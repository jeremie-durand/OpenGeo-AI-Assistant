"""HTTP retriever backend against an in-process stub server."""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from services.vector_search.backends.base import (
    BackendConfigError,
    BackendUnavailableError,
    SearchQuery,
)
from services.vector_search.backends.http import HttpRetrieverBackend
from services.vector_search.embedders import create_embedder
from services.vector_search.settings import VectorSearchSettings

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def serve(handler: Handler, scenario: Callable[[str], Awaitable[Any]]) -> Any:
    """Run *scenario* with the URL of a stub server answering with *handler*."""

    async def main() -> Any:
        app = web.Application()
        app.router.add_post("/search", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            return await scenario(str(server.make_url("/search")))
        finally:
            await server.close()

    return asyncio.run(main())


def recording(seen: List[Dict[str, Any]], payload: Any, status: int = 200) -> Handler:
    async def handler(request: web.Request) -> web.Response:
        seen.append(
            {"body": await request.json(), "auth": request.headers.get("Authorization")}
        )
        return web.json_response(payload, status=status)

    return handler


RESULTS = {
    "results": [{"id": "mtbs", "score": 12.5, "title": "MTBS"}, {"id": "b", "score": 3}]
}


def test_exact_body_bearer_and_passthrough_scores() -> None:
    seen: List[Dict[str, Any]] = []

    async def scenario(url: str) -> Any:
        backend = HttpRetrieverBackend(url, "token-123", 5.0)
        return await backend.search(SearchQuery("burn severity", 10))

    hits = serve(recording(seen, RESULTS), scenario)
    assert seen[0]["body"] == {"query": "burn severity", "k": 10, "kind": "collections"}
    assert seen[0]["auth"] == "Bearer token-123"
    assert [(h.id, h.score, h.title) for h in hits] == [
        ("mtbs", 12.5, "MTBS"),
        ("b", 3.0, ""),
    ]


def test_filters_forwarded_unchanged() -> None:
    seen: List[Dict[str, Any]] = []

    async def scenario(url: str) -> Any:
        query = SearchQuery(
            "lidar", 3, bbox=(-79.8, 45.0, -57.1, 62.6), datetime_text="2001-01-01/.."
        )
        return await HttpRetrieverBackend(url, None, 5.0).search(query)

    serve(recording(seen, {"results": []}), scenario)
    assert seen[0]["body"]["bbox"] == [-79.8, 45.0, -57.1, 62.6]
    assert seen[0]["body"]["datetime"] == "2001-01-01/.."
    assert seen[0]["auth"] is None


async def _search(url: str, timeout: float = 5.0) -> Any:
    return await HttpRetrieverBackend(url, None, timeout).search(SearchQuery("q", 5))


def test_http_500_is_unavailable() -> None:
    with pytest.raises(BackendUnavailableError, match="HTTP 500"):
        serve(recording([], {"error": "x"}, status=500), _search)


def test_timeout_is_unavailable() -> None:
    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(1)
        return web.json_response(RESULTS)

    with pytest.raises(BackendUnavailableError):
        serve(slow, lambda url: _search(url, timeout=0.1))


def test_invalid_json_is_unavailable() -> None:
    async def broken(request: web.Request) -> web.Response:
        return web.Response(text="not json", content_type="application/json")

    with pytest.raises(BackendUnavailableError):
        serve(broken, _search)


@pytest.mark.parametrize(
    "payload",
    [{}, {"results": "x"}, {"results": [{"score": 1}]}, {"results": [{"id": "a"}]}],
)
def test_invalid_body_is_unavailable(payload: Dict[str, Any]) -> None:
    with pytest.raises(BackendUnavailableError):
        serve(recording([], payload), _search)


def test_check_probes_with_one_result() -> None:
    seen: List[Dict[str, Any]] = []

    async def scenario(url: str) -> None:
        await HttpRetrieverBackend(url, None, 5.0).check(None)

    serve(recording(seen, {"results": []}), scenario)
    assert seen[0]["body"]["k"] == 1


def test_read_only() -> None:
    backend = HttpRetrieverBackend("https://h/search", None, 5.0)
    for call in (backend.stored(), backend.upsert([]), backend.delete_missing(set())):
        with pytest.raises(BackendConfigError):
            asyncio.run(call)


def test_no_embedding_model_with_default_provider() -> None:
    settings = VectorSearchSettings.from_env(
        {"VECTOR_SEARCH_URL": "https://search.example.com/v1/search"}
    )
    assert settings.provider == "local"
    assert create_embedder(settings) is None


def test_redirect_loop_reason_never_contains_the_token() -> None:
    async def loop(request: web.Request) -> web.Response:
        raise web.HTTPTemporaryRedirect(location=str(request.url))

    async def scenario(url: str) -> Any:
        return await HttpRetrieverBackend(url, "secret-token", 5.0).search(
            SearchQuery("q", 5)
        )

    with pytest.raises(BackendUnavailableError) as error:
        serve(loop, scenario)
    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None
