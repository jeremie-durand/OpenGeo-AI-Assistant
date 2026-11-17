"""/api/health reports vector search without ever failing the health check."""

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from services.vector_search import index as vector_index
from services.vector_search.index import STATUS_DEGRADED, STATUS_READY


def _aiohttp_ok() -> MagicMock:
    resp = MagicMock(status=200)
    get_ctx = MagicMock(
        __aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False)
    )
    session = MagicMock(get=MagicMock(return_value=get_ctx))
    return MagicMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    )


def health() -> Any:
    from fastapi.testclient import TestClient
    from fastapi_app import app

    with patch("aiohttp.ClientSession", return_value=_aiohttp_ok()):
        return TestClient(app).get("/api/health")


def _set_index(monkeypatch: pytest.MonkeyPatch, **state: Any) -> None:
    index = vector_index.get_index()
    for key, value in state.items():
        setattr(index, key, value)

    async def no_run() -> None:
        return None

    monkeypatch.setattr(index, "run", no_run)


def test_degraded_vector_search_keeps_health_200(monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_SEARCH_ENABLED", "true")
    _set_index(monkeypatch, status=STATUS_DEGRADED, reason="PostgreSQL is unreachable")
    response = health()
    body: Dict[str, Any] = response.json()
    assert response.status_code == 200 and body["status"] == "healthy"
    assert body["checks"]["vector_search"]["status"] == "degraded"
    assert body["checks"]["vector_search"]["reason"] == "PostgreSQL is unreachable"


def test_database_password_never_in_health(monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_SEARCH_ENABLED", "true")
    monkeypatch.setenv("VECTOR_SEARCH_URL", "postgresql://user:secret@db:5432/geo")
    _set_index(monkeypatch, status=STATUS_DEGRADED, reason="unreachable")
    assert "secret" not in health().text


def test_refresh_error_shown_while_ready(monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_SEARCH_ENABLED", "true")
    _set_index(
        monkeypatch,
        status=STATUS_READY,
        count=80,
        last_error="Private STAC API http://localhost:8081 is unavailable",
    )
    check = health().json()["checks"]["vector_search"]
    assert check["status"] == "ready" and check["document_count"] == 80
    assert "unavailable" in check["last_error"] and "reason" not in check


def test_disabled_loads_no_model(monkeypatch) -> None:
    import fastembed

    loaded = []
    monkeypatch.setattr(fastembed, "TextEmbedding", lambda *a, **k: loaded.append(1))
    check = health().json()["checks"]["vector_search"]
    assert check["status"] == "disabled"
    assert loaded == [] and vector_index.get_index()._embedder is None
