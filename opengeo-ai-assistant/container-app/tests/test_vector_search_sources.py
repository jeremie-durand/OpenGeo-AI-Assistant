"""Collection sources: offline Planetary Computer text, extents, private STAC."""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from services import dashboard_catalog
from services.vector_search import sources

US_BBOX = [-125.0, 24.0, -66.0, 50.0]


@pytest.fixture(autouse=True)
def fresh_catalog() -> None:
    dashboard_catalog.reset_cache()
    yield
    dashboard_catalog.reset_cache()


def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail() -> List[Dict[str, Any]]:
        raise aiohttp.ClientConnectionError("no network")

    monkeypatch.setattr(dashboard_catalog, "fetch_upstream_collections", fail)


def _online(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch() -> List[Dict[str, Any]]:
        return [
            {
                "id": "3dep-lidar-dtm",
                "extent": {
                    "spatial": {"bbox": [US_BBOX]},
                    "temporal": {"interval": [["2012-01-01T00:00:00Z", None]]},
                },
            }
        ]

    monkeypatch.setattr(dashboard_catalog, "fetch_upstream_collections", fetch)


def test_offline_build_has_every_renderable_collection(monkeypatch) -> None:
    _offline(monkeypatch)
    documents, reason = asyncio.run(sources.planetary_computer_documents())
    by_id = {doc.id: doc for doc in documents}
    assert len(documents) == len(dashboard_catalog.renderable_collection_ids())
    for collection_id in ("ms-buildings", "mtbs", "modis-17A2H-061"):
        assert by_id[collection_id].title and by_id[collection_id].description
        assert by_id[collection_id].bbox is None and by_id[collection_id].start is None
    assert "extents unavailable" in reason


def test_catalog_extents_are_used(monkeypatch) -> None:
    _online(monkeypatch)
    documents, reason = asyncio.run(sources.planetary_computer_documents())
    lidar = next(doc for doc in documents if doc.id == "3dep-lidar-dtm")
    assert reason == ""
    assert lidar.bbox == tuple(US_BBOX)
    assert lidar.start is not None and lidar.start.year == 2012 and lidar.end is None


def test_stac_base_url() -> None:
    assert sources.stac_base_url("http://h:8081/search") == "http://h:8081"
    assert sources.stac_base_url("http://h:8081/") == "http://h:8081"


COLLECTIONS = [
    {
        "id": "orthophotos-sherbrooke-2024",
        "title": "Aerial photos Sherbrooke 2024",
        "description": "Orthophotos",
        "keywords": ["aerial"],
        "extent": {
            "spatial": {"bbox": [[-72.1, 45.3, -71.7, 45.5]]},
            "temporal": {
                "interval": [["2024-05-01T00:00:00Z", "2024-06-01T00:00:00Z"]]
            },
        },
    },
    {"id": "lidar-raw", "title": "Raw lidar"},
    {"id": "sentinel-2-l2a", "title": "Local Sentinel-2 mirror"},
]
ITEMS = {
    "orthophotos-sherbrooke-2024": [{"assets": {"tilejson": {"href": "http://t/x"}}}],
    "lidar-raw": [{"assets": {"data": {"href": "http://d/x.laz"}}}],
}


def stub_stac(scenario: Callable[[str], Awaitable[Any]], paginate: bool = False) -> Any:
    async def collections(request: web.Request) -> web.Response:
        if not paginate:
            return web.json_response({"collections": COLLECTIONS})
        if request.query.get("page") == "2":
            return web.json_response({"collections": COLLECTIONS[1:]})
        next_url = str(request.url.with_query(page="2"))
        return web.json_response(
            {
                "collections": COLLECTIONS[:1],
                "links": [{"rel": "next", "href": next_url}],
            }
        )

    async def items(request: web.Request) -> web.Response:
        features = ITEMS.get(request.match_info["cid"], [])
        return web.json_response({"features": features})

    async def main() -> Any:
        app = web.Application()
        app.router.add_get("/collections", collections)
        app.router.add_get("/collections/{cid}/items", items)
        server = TestServer(app)
        await server.start_server()
        try:
            return await scenario(str(server.make_url("/search")))
        finally:
            await server.close()

    return asyncio.run(main())


@pytest.mark.parametrize("paginate", [False, True])
def test_private_collections_and_renderable_probe(paginate: bool) -> None:
    catalog = stub_stac(
        lambda url: sources.private_stac_catalog(url, {"sentinel-2-l2a"}), paginate
    )
    ids = [doc.id for doc in catalog.documents]
    assert ids == ["orthophotos-sherbrooke-2024", "lidar-raw"]
    assert catalog.renderable_ids == {"orthophotos-sherbrooke-2024"}
    ortho = catalog.documents[0]
    assert ortho.bbox == (-72.1, 45.3, -71.7, 45.5) and ortho.source == "private_stac"
    assert ortho.keywords == ("aerial",) and ortho.end is not None


def test_private_stac_down_raises_with_masked_reason() -> None:
    url = "http://user:secret@127.0.0.1:9/search"
    with pytest.raises(sources.SourceUnavailableError, match="unavailable") as error:
        asyncio.run(sources.private_stac_catalog(url, set()))
    assert "secret" not in str(error.value)
    assert "ClientConnectorError" in str(error.value)


def test_dashboard_api_unchanged_by_intervals(monkeypatch) -> None:
    _online(monkeypatch)
    catalog = asyncio.run(dashboard_catalog.get_catalog())
    assert set(catalog["collections"][0]) == {
        "id",
        "title",
        "category",
        "bbox",
        "is_global",
    }
    assert dashboard_catalog.collection_intervals()["3dep-lidar-dtm"][1] is None


def test_off_host_next_link_and_redirects_are_not_followed() -> None:
    fetched: List[str] = []

    async def scenario(url: str) -> Any:
        return await sources.private_stac_catalog(url, set())

    async def collections(request: web.Request) -> web.Response:
        fetched.append(str(request.url))
        return web.json_response(
            {
                "collections": [{"id": "a b/../c?x"}],
                "links": [{"rel": "next", "href": "http://169.254.169.254/latest"}],
            }
        )

    async def items(request: web.Request) -> web.Response:
        fetched.append(request.raw_path)
        raise web.HTTPFound(location="http://169.254.169.254/latest")

    async def main() -> Any:
        app = web.Application()
        app.router.add_get("/collections", collections)
        app.router.add_route("GET", "/collections/{cid:.*}", items)
        server = TestServer(app)
        await server.start_server()
        try:
            return await scenario(str(server.make_url("/search")))
        finally:
            await server.close()

    catalog = asyncio.run(main())
    assert [d.id for d in catalog.documents] == ["a b/../c?x"]
    assert catalog.renderable_ids == set()
    assert not any("169.254" in url for url in fetched)
    assert "/collections/a%20b%2F..%2Fc%3Fx/items?limit=1" in fetched


def test_same_origin_url() -> None:
    base = "http://stac:8081"
    assert sources.same_origin_url(base, "/collections?page=2") == (
        "http://stac:8081/collections?page=2"
    )
    assert sources.same_origin_url(base, "https://stac:8081/x") is None
    assert sources.same_origin_url(base, "http://evil:8081/x") is None
    assert sources.same_origin_url(base, "file:///etc/passwd") is None


def test_placeholder_descriptions_are_replaced(monkeypatch) -> None:
    _offline(monkeypatch)
    documents, _ = asyncio.run(sources.planetary_computer_documents())
    by_id = {doc.id: doc for doc in documents}
    overrides = sources._description_overrides()
    assert set(overrides) <= set(by_id)
    for collection_id in overrides:
        assert "geospatial dataset from Microsoft Planetary Computer" not in (
            by_id[collection_id].description
        )
    assert by_id["jrc-gsw"].title == "JRC Global Surface Water"
    assert "Lakes" in by_id["jrc-gsw"].keywords
