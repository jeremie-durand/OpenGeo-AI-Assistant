"""Route and middleware parity checks guarding the fastapi_app split (#3)."""

from fastapi.routing import APIRoute

EXPECTED_ROUTES = {
    ("GET", "/"),
    ("GET", "/pc_collections_metadata.json"),
    ("GET", "/stac_collections.json"),
    ("GET", "/pc_rendering_config.json"),
    ("GET", "/api/health"),
    ("GET", "/api/config"),
    ("GET", "/api/stac/collections"),
    ("POST", "/api/query"),
    ("POST", "/api/sign-mosaic-url"),
    ("POST", "/api/stac-search"),
    ("POST", "/api/veda-search"),
    ("POST", "/api/structured-search"),
    ("POST", "/api/session-reset"),
    ("POST", "/api/process-comparison-query"),
    ("POST", "/api/geoint/mobility"),
    ("POST", "/api/geoint/terrain"),
    ("POST", "/api/geoint/terrain/chat"),
    ("GET", "/api/geoint/terrain/chat/{session_id}/history"),
    ("DELETE", "/api/geoint/terrain/chat/{session_id}"),
    ("POST", "/api/geoint/vision"),
    ("POST", "/api/geoint/vision/chat"),
    ("DELETE", "/api/geoint/vision/chat/{session_id}"),
    ("POST", "/api/geoint/building-damage"),
    ("POST", "/api/geoint/extreme-weather"),
    ("GET", "/api/geoint/cmip6-test"),
    ("POST", "/api/geoint/comparison"),
    ("POST", "/api/geoint/animation"),
    ("POST", "/api/geoint/orchestrate"),
    ("GET", "/api/debug/location/{location}"),
}


def _flatten(routes: list) -> list[APIRoute]:
    """Expand included routers (lazy in recent FastAPI) into APIRoutes."""
    flat: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            flat.append(route)
        elif hasattr(route, "original_router"):
            flat.extend(_flatten(route.original_router.routes))
    return flat


def _route_keys() -> list[tuple[str, str]]:
    """Return (method, path) for every API route, duplicates included."""
    from fastapi_app import app

    return [
        (method, route.path)
        for route in _flatten(app.routes)
        for method in route.methods
    ]


def test_every_route_still_registered() -> None:
    assert set(_route_keys()) == EXPECTED_ROUTES


def test_no_duplicate_routes() -> None:
    keys = _route_keys()
    assert len(keys) == len(set(keys))


def test_middleware_order_unchanged() -> None:
    from fastapi_app import app

    names = [middleware.cls.__name__ for middleware in app.user_middleware]
    assert names == ["CORSMiddleware", "RateLimitMiddleware"]


def test_global_exception_handler_registered() -> None:
    from fastapi_app import app

    assert Exception in app.exception_handlers


def test_rendering_config_served_from_moved_router() -> None:
    from fastapi.testclient import TestClient
    from fastapi_app import app

    response = TestClient(app).get("/pc_rendering_config.json")
    assert response.status_code == 200


def test_all_endpoints_live_in_routers() -> None:
    from fastapi_app import app

    modules = {route.endpoint.__module__ for route in _flatten(app.routes)}
    assert modules
    assert all(module.startswith("routers.") for module in modules), modules
