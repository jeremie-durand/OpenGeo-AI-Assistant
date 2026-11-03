"""Startup populates the shared app_state module that routers read from."""

from unittest.mock import MagicMock, patch

import pytest


def test_lifespan_populates_app_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan fills app_state; monkeypatch restores the prior state after."""
    import app_state
    from fastapi.testclient import TestClient

    for name in (
        "semantic_translator",
        "global_translator",
        "router_agent",
        "SEMANTIC_KERNEL_AVAILABLE",
    ):
        monkeypatch.setattr(app_state, name, getattr(app_state, name))

    translator, agent = MagicMock(), MagicMock()
    with (
        patch(
            "generic_query_translator.GenericQueryTranslator",
            return_value=translator,
        ),
        patch("geoint.router_agent.get_router_agent", return_value=agent),
    ):
        from fastapi_app import app

        with TestClient(app):
            assert app_state.semantic_translator is translator
            assert app_state.global_translator is translator
            assert app_state.router_agent is agent
            assert app_state.SEMANTIC_KERNEL_AVAILABLE is True
