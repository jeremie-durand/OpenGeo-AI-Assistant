"""FastAPI app startup and /api/health smoke tests."""

from unittest.mock import AsyncMock, MagicMock, patch


def _aiohttp_mock(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    get_ctx = MagicMock(
        __aenter__=AsyncMock(return_value=resp),
        __aexit__=AsyncMock(return_value=False),
    )
    session = MagicMock(get=MagicMock(return_value=get_ctx))
    return MagicMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    )


def test_app_importable() -> None:
    from fastapi import FastAPI
    from fastapi_app import app

    assert isinstance(app, FastAPI)


def test_health_returns_200_with_valid_llm_config() -> None:
    from fastapi.testclient import TestClient

    with patch("aiohttp.ClientSession", return_value=_aiohttp_mock()):
        from fastapi_app import app

        response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["checks"]["llm_client"]["status"] == "configured"


def test_health_checks_structure() -> None:
    from fastapi.testclient import TestClient

    with patch("aiohttp.ClientSession", return_value=_aiohttp_mock()):
        from fastapi_app import app

        body = TestClient(app).get("/api/health").json()
    # private_stac_api is only present when STAC_API_URL is configured
    assert {"llm_client", "planetary_computer"} <= body["checks"].keys()
