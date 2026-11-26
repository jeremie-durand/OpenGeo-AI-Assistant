"""POST /api/datasets/search: results, validation, 503s, rate limit and auth."""

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.vector_search import index as vector_index
from services.vector_search.backends.base import (
    BackendHit,
    BackendUnavailableError,
    SearchQuery,
)
from services.vector_search.backends.parquet import ParquetBackend
from services.vector_search.index import STATUS_DEGRADED, VectorIndex
from services.vector_search.settings import VectorSearchSettings
from vector_search_fakes import FakeEmbedder, Sources, doc

URL = "/api/datasets/search"
QUEBEC = [-79.8, 45.0, -57.1, 62.6]


def _sources() -> Sources:
    src = Sources()
    src.pc = [
        doc("ms-buildings", "building footprints"),
        doc("mtbs", "burn severity fire"),
        doc("3dep-lidar-dtm", "lidar elevation", bbox=(-125.0, 24.0, -66.0, 36.0)),
        doc("lidar-global", "lidar elevation world"),
    ] + [doc(f"filler-{i}", f"other data {i}") for i in range(12)]
    src.private = []
    return src


@pytest.fixture
def ready(tmp_path: Path) -> VectorIndex:
    src = _sources()
    index = VectorIndex(
        VectorSearchSettings.from_env({}),
        backend=ParquetBackend(str(tmp_path / "i.parquet"), False, str(tmp_path)),
        embedder=FakeEmbedder(),
        pc_source=src.pc_source,
        private_source=src.private_source,
        stac_api_url="",
    )
    asyncio.run(index.start())
    vector_index._index = index
    return index


def client() -> TestClient:
    from fastapi_app import app

    return TestClient(app)


def post(body: Dict[str, Any]) -> Any:
    return client().post(URL, json=body)


def test_results_shape_and_order(ready) -> None:
    results = post({"query": "building footprints"}).json()["results"]
    assert results[0]["id"] == "ms-buildings"
    assert set(results[0]) == {"id", "title", "score", "renderable", "source"}
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_default_returns_at_most_ten(ready) -> None:
    assert len(post({"query": "data"}).json()["results"]) == 10


def test_bbox_filter(ready) -> None:
    ids = [r["id"] for r in post({"query": "lidar", "bbox": QUEBEC}).json()["results"]]
    assert "lidar-global" in ids and "3dep-lidar-dtm" not in ids


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 501},
        {"query": "fires", "k": 500},
        {"query": "fires", "k": 0},
        {"query": "fires", "bbox": [-80, 45, -57, 120]},
        {"query": "fires", "bbox": [-57, 45, -80, 62]},
        {"query": "fires", "datetime": "last summer"},
        {"query": "fires", "unexpected": 1},
    ],
)
def test_validation_errors(body: Dict[str, Any], ready) -> None:
    assert post(body).status_code == 422


def test_open_ended_interval_accepted(ready) -> None:
    response = post({"query": "fires", "datetime": "../2020-12-31T23:59:59Z"})
    assert response.status_code == 200


def test_disabled_is_503() -> None:
    response = post({"query": "fires"})
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_building_is_503() -> None:
    index = VectorIndex(VectorSearchSettings.from_env({}))
    vector_index._index = index
    response = post({"query": "fires"})
    assert response.status_code == 503 and "building" in response.json()["detail"]


def test_degraded_is_503(ready) -> None:
    ready.status, ready.reason = STATUS_DEGRADED, "PostgreSQL is unreachable"
    response = post({"query": "fires"})
    assert response.status_code == 503
    assert "PostgreSQL is unreachable" in response.json()["detail"]


def test_backend_failure_during_request_is_503(ready, monkeypatch) -> None:
    async def fail(query: SearchQuery) -> List[BackendHit]:
        raise BackendUnavailableError("The HTTP retriever answered HTTP 500.")

    monkeypatch.setattr(ready._backend, "search", fail)
    response = post({"query": "fires"})
    detail = response.json()["detail"]
    assert response.status_code == 503 and "unavailable" in detail
    assert "HTTP 500" not in detail


def test_works_with_misconfigured_llm(ready, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER", "nonsense")
    assert post({"query": "burn severity"}).status_code == 200


def _mini_app(*middlewares: Any) -> FastAPI:
    from routers import datasets

    app = FastAPI()
    for middleware in middlewares:
        app.add_middleware(middleware)
    app.include_router(datasets.router)
    return app


def test_rate_limit_429(ready, monkeypatch) -> None:
    import rate_limit_middleware as rl
    from limits import parse
    from limits.storage import MemoryStorage
    from limits.strategies import FixedWindowRateLimiter

    monkeypatch.setattr(rl, "_search_limit", parse("2/minute"))
    monkeypatch.setattr(rl, "_strategy", FixedWindowRateLimiter(MemoryStorage()))
    test_client = TestClient(_mini_app(rl.RateLimitMiddleware))
    codes = [test_client.post(URL, json={"query": "q"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_auth_401_without_key(ready, monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "a" * 64)
    from auth_middleware import ApiKeyAuthMiddleware

    test_client = TestClient(_mini_app(ApiKeyAuthMiddleware))
    assert test_client.post(URL, json={"query": "q"}).status_code == 401
    ok = test_client.post(URL, json={"query": "q"}, headers={"X-Api-Key": "a" * 64})
    assert ok.status_code == 200


def test_private_and_unrenderable_flags(ready) -> None:
    ready.private_ids = {"lidar-global"}
    results = post({"query": "lidar elevation world"}).json()["results"]
    top = results[0]
    assert top["id"] == "lidar-global" and top["renderable"] is False
    assert top["source"] == "private_stac"
