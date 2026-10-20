"""Startup populates the shared app_state module that routers read from."""

from unittest.mock import MagicMock, patch


def test_lifespan_populates_app_state() -> None:
    from fastapi.testclient import TestClient

    translator, agent = MagicMock(), MagicMock()
    with (
        patch(
            "generic_query_translator.GenericQueryTranslator",
            return_value=translator,
        ),
        patch("geoint.router_agent.get_router_agent", return_value=agent),
    ):
        import app_state
        from fastapi_app import app

        with TestClient(app):
            assert app_state.semantic_translator is translator
            assert app_state.global_translator is translator
            assert app_state.router_agent is agent
            assert app_state.SEMANTIC_KERNEL_AVAILABLE is True
