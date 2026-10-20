"""Characterization tests for POST /api/query (issue #3, phase 2).

Each scenario runs ``unified_query_processor`` against fake collaborators and
compares the normalized response, plus the ordered collaborator calls, with
``snapshots/query_endpoint.json``. Re-record with ``.refactor/record-snapshots``
and review the JSON diff before committing.
"""

import asyncio
import copy
import importlib
import inspect
import json
import logging
import os
import sys
from pathlib import Path
from types import FunctionType, MethodType, ModuleType, SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import pytest
from generic_query_translator import GenericQueryTranslator

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "query_endpoint.json"
UPDATE = os.environ.get("UPDATE_QUERY_SNAPSHOTS") == "1"
VOLATILE_KEYS = frozenset(
    {
        "timestamp",
        "search_timestamp",
        "translation_timestamp",
        "processing_timestamp",
        "fast_path_elapsed_ms",
        "elapsed_ms",
    }
)
VOLATILE = "<volatile>"
PATCH_TARGETS = [
    ("services.stac_search", "execute_direct_stac_search"),
    ("services.stac_search", "try_alternative_queries"),
    ("hybrid_rendering_system", "get_mosaic_tilejson_url"),
    ("location_resolver", "EnhancedLocationResolver"),
    ("agents", "get_vision_agent"),
    ("geoint.chat_vision_analyzer", "get_chat_vision_analyzer"),
    ("geoint.extreme_weather_agent", "get_extreme_weather_agent"),
]
for _module_name in [name for name, _ in PATCH_TARGETS] + [
    "routers.query",
    "geoint.router_agent",
]:
    importlib.import_module(_module_name)
ORIGINALS: Dict[Tuple[str, str], Any] = {
    (name, attr): getattr(sys.modules[name], attr) for name, attr in PATCH_TARGETS
}
SESSION = "session-1"
PARIS_BBOX = [2.22, 48.81, 2.47, 48.9]
PARIS_BOUNDS = {
    "west": 2.22,
    "south": 48.81,
    "east": 2.47,
    "north": 48.9,
    "center_lat": 48.855,
    "center_lng": 2.345,
}
SENTINEL_PARAMS = {
    "collections": ["sentinel-2-l2a"],
    "bbox": PARIS_BBOX,
    "datetime": "2024-06-01/2024-06-30",
    "location_name": "Paris",
}
STAC_ACTION = {"action_type": "stac_search"}
NAVIGATE_ACTION = {"action_type": "navigate_to", "location": "paris", "zoom_level": 11}
WEATHER_ACTION = {
    "action_type": "extreme_weather",
    "location": "miami",
    "original_query": "How hot will Miami get by 2050?",
}
SERVER_ERROR = {"http_error": 500, "detail": "Query processing failed"}


def feature(
    item_id: str,
    collection: str = "sentinel-2-l2a",
    bbox: Optional[List[Any]] = None,
    cloud: Optional[float] = 5.0,
    tilejson: bool = True,
    expression: bool = False,
) -> Dict[str, Any]:
    """Build a STAC item with an optional Planetary Computer tilejson asset."""
    href = (
        "https://planetarycomputer.microsoft.com/api/data/v1/item/tilejson.json"
        f"?collection={collection}&item={item_id}&assets=visual"
        "&asset_bidx=visual%7C1%2C2%2C3&nodata=0"
    )
    if expression:
        href += "&expression=HH%3BHV&rescale=0%2C0.5&rescale=0%2C0.1"
    assets = {
        "visual": {
            "href": f"https://data.test/{item_id}.tif",
            "type": "image/tiff; application=geotiff",
        }
    }
    if tilejson:
        assets["tilejson"] = {"href": href, "type": "application/json"}
    return {
        "type": "Feature",
        "id": item_id,
        "collection": collection,
        "bbox": list(bbox or [2.2, 48.8, 2.5, 48.95]),
        "properties": {"datetime": "2024-06-01T10:00:00Z", "eo:cloud_cover": cloud},
        "assets": assets,
    }


def results(*features: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap features in a successful execute_direct_stac_search result."""
    return {
        "success": True,
        "results": {"type": "FeatureCollection", "features": list(features)},
    }


def feature_ids(payload: Any) -> List[Any]:
    """Ids of the features in a STAC response or FeatureCollection."""
    if not isinstance(payload, dict):
        return []
    collection = payload.get("results", payload)
    if not isinstance(collection, dict):
        return []
    return [f.get("id") for f in collection.get("features", []) if isinstance(f, dict)]


def json_default(value: Any) -> Any:
    """Strict JSON conversion: sets become sorted lists, anything else fails."""
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class ErrorCollector(logging.Handler):
    """Collects the first line of every ERROR record."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage().splitlines()[0])


def normalize(value: Any) -> Any:
    """Drop wall-clock values and the random pipeline id so snapshots are stable."""
    if isinstance(value, dict):
        out = {
            k: VOLATILE if k in VOLATILE_KEYS else normalize(v)
            for k, v in value.items()
        }
        if isinstance(out.get("pipeline_trace"), dict):
            out["pipeline_trace"]["session_id"] = "<pipeline>"
        return out
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


class FakeTranslator:
    """Stands in for GenericQueryTranslator and records every call."""

    _filter_stac_results_by_cloud_cover = (
        GenericQueryTranslator._filter_stac_results_by_cloud_cover
    )

    def __init__(
        self,
        calls: List[Any],
        stac_params: Optional[Dict[str, Any]] = None,
        translate_error: Optional[Exception] = None,
        endpoint: str = "planetary_computer",
        contextual_error: Optional[Exception] = None,
    ) -> None:
        self.calls = calls
        self.stac_params = stac_params
        self.translate_error = translate_error
        self.endpoint = endpoint
        self.contextual_error = contextual_error

    def _record(self, name: str, payload: Any = None) -> None:
        self.calls.append([f"translator.{name}", copy.deepcopy(payload)])

    def set_model(self, model_name: str) -> None:
        self._record("set_model", model_name)

    def get_conversation_context(self, conversation_id: str) -> Dict[str, Any]:
        self._record("get_conversation_context", conversation_id)
        return {}

    def determine_stac_source(self, query: str, entities: Dict[str, Any]) -> str:
        self._record("determine_stac_source", {"query": query, "entities": entities})
        return self.endpoint

    async def translate_query(
        self,
        query: str,
        pin_location: Optional[Dict[str, Any]] = None,
        session_bbox: Optional[List[float]] = None,
    ) -> Optional[Dict[str, Any]]:
        self._record(
            "translate_query",
            {"query": query, "pin": pin_location, "session_bbox": session_bbox},
        )
        if self.translate_error:
            raise self.translate_error
        return copy.deepcopy(self.stac_params)

    async def generate_contextual_earth_science_response(
        self,
        natural_query: str,
        classification: Dict[str, Any],
        stac_response: Optional[Dict[str, Any]] = None,
        geoint_results: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        count = len((stac_response or {}).get("results", {}).get("features", []))
        self._record(
            "generate_contextual",
            {
                "query": natural_query,
                "intent": classification.get("intent_type"),
                "features": count,
                "classification": classification,
                "geoint_results": geoint_results,
                "feature_ids": feature_ids(stac_response),
            },
        )
        if self.contextual_error:
            raise self.contextual_error
        return {"message": f"contextual answer for {count} features"}

    async def generate_empty_result_response(
        self,
        natural_query: str,
        stac_query: Dict[str, Any],
        collections: List[str],
        diagnostics: Dict[str, Any],
    ) -> str:
        stage = diagnostics.get("failure_stage")
        self._record(
            "generate_empty",
            {
                "collections": collections,
                "stage": stage,
                "query": natural_query,
                "stac_query": stac_query,
                "diagnostics": diagnostics,
            },
        )
        return f"no results ({stage})"

    async def generate_alternative_result_response(
        self,
        natural_query: str,
        classification: Dict[str, Any],
        stac_response: Dict[str, Any],
        original_filters: Dict[str, Any],
        alternative_filters: Dict[str, Any],
        explanation: str,
        geoint_results: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._record(
            "generate_alternative",
            {
                "original": original_filters,
                "alternative": alternative_filters,
                "explanation": explanation,
                "classification": classification,
                "feature_ids": feature_ids(stac_response),
            },
        )
        return {"message": f"alternative: {explanation}"}


class FakeRouterAgent:
    """Stands in for RouterAgent: canned action, real session-context merging."""

    def __init__(
        self,
        calls: List[Any],
        action: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
        session: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.calls = calls
        self.action = action
        self.error = error
        contexts = {SESSION: copy.deepcopy(session)} if session else {}
        self.tools = SimpleNamespace(session_contexts=contexts)

    def update_session_context(self, session_id: str, context: Dict[str, Any]) -> None:
        self.calls.append(
            [
                "router.update_session_context",
                {"session_id": session_id, "context": copy.deepcopy(context)},
            ]
        )
        self.tools.session_contexts.setdefault(session_id, {}).update(
            copy.deepcopy(context)
        )

    async def route_query(
        self, query: str, session_id: str, has_screenshot: bool = False
    ) -> Optional[Dict[str, Any]]:
        self.calls.append(
            ["router.route_query", {"query": query, "has_screenshot": has_screenshot}]
        )
        if self.error:
            raise self.error
        return copy.deepcopy(self.action)


class FakeHttpResponse:
    """Async context manager returning a canned JSON payload."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload

    async def __aenter__(self) -> "FakeHttpResponse":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    async def json(self) -> Dict[str, Any]:
        return copy.deepcopy(self.payload)


class FakeHttpSession:
    """aiohttp.ClientSession stand-in for the GIS feature lookup."""

    def __init__(self, calls: List[Any], payloads: Dict[str, Any]) -> None:
        self.calls = calls
        self.payloads = payloads

    async def __aenter__(self) -> "FakeHttpSession":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> Any:
        self.calls.append(["http.get", url])
        key = "feature" if "/items/" in url else "collections"
        return FakeHttpResponse(self.payloads[key])


class Harness:
    """Installs fakes for every /api/query collaborator and runs the handler."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app_state
        import routers.query  # noqa: F401  loads every pipeline module first

        self.monkeypatch = monkeypatch
        self.app_state = app_state
        self.calls: List[Any] = []
        self.stac_responses: List[Dict[str, Any]] = []
        self.alternative_result: Dict[str, Any] = {"success": False, "features": []}
        self.mosaic_result: Optional[Dict[str, Any]] = None
        self.location_bbox: Any = None
        self.weather_result: Any = {"response": "Climate summary for the location."}
        self.vision_result: Any = {"response": "Vision answer."}
        self.keyword_vision = False
        self._install_fakes()
        self.configure()

    def configure(
        self,
        translator: Optional[FakeTranslator] = None,
        router: Optional[FakeRouterAgent] = None,
    ) -> None:
        """Set app_state the way lifespan does; AI mode iff there is a translator."""
        state = {
            "semantic_translator": translator,
            "global_translator": translator,
            "router_agent": router,
            "SEMANTIC_KERNEL_AVAILABLE": translator is not None,
        }
        for name, value in state.items():
            self.monkeypatch.setattr(self.app_state, name, value)

    def ai_mode(
        self,
        stac_params: Optional[Dict[str, Any]] = None,
        action: Optional[Dict[str, Any]] = None,
        session: Optional[Dict[str, Any]] = None,
        router_error: Optional[Exception] = None,
        **translator_options: Any,
    ) -> None:
        """Configure a fake translator plus a fake RouterAgent."""
        self.configure(
            translator=FakeTranslator(
                self.calls, stac_params=stac_params, **translator_options
            ),
            router=FakeRouterAgent(
                self.calls, action=action, error=router_error, session=session
            ),
        )

    def run(self, **body: Any) -> Dict[str, Any]:
        """Call the handler; return normalized response and collaborator calls."""
        from fastapi import HTTPException
        from request_models import QueryRequest
        from routers.query import unified_query_processor

        collector = ErrorCollector()
        logging.getLogger().addHandler(collector)
        try:
            response = asyncio.run(unified_query_processor(QueryRequest(**body)))
        except HTTPException as exc:
            response = {"http_error": exc.status_code, "detail": exc.detail}
        finally:
            logging.getLogger().removeHandler(collector)
        router = self.app_state.router_agent
        snapshot = normalize(
            {
                "response": response,
                "calls": self.calls,
                "errors": collector.lines,
                "session_contexts": router.tools.session_contexts if router else None,
            }
        )
        return json.loads(json.dumps(snapshot, default=json_default))

    def _install_fakes(self) -> None:
        patch = self._patch_everywhere
        patch("services.stac_search", "execute_direct_stac_search", self._stac)
        patch("services.stac_search", "try_alternative_queries", self._alternatives)
        patch("hybrid_rendering_system", "get_mosaic_tilejson_url", self._mosaic)
        patch(
            "location_resolver",
            "EnhancedLocationResolver",
            lambda: SimpleNamespace(resolve_location_to_bbox=self._resolve),
        )
        patch(
            "agents", "get_vision_agent", lambda: SimpleNamespace(analyze=self._vision)
        )
        patch(
            "geoint.chat_vision_analyzer",
            "get_chat_vision_analyzer",
            lambda: SimpleNamespace(should_use_vision=self._should_use_vision),
        )
        patch(
            "geoint.extreme_weather_agent",
            "get_extreme_weather_agent",
            lambda: SimpleNamespace(chat=self._weather),
        )

    def _patch_everywhere(self, module_name: str, attr: str, fake: Any) -> None:
        """Replace module.attr and every module-level alias of the same object."""
        original = ORIGINALS[(module_name, attr)]
        for module in list(sys.modules.values()):
            if not isinstance(module, ModuleType):
                continue
            for name, value in list(vars(module).items()):
                if value is original:
                    self.monkeypatch.setattr(module, name, fake)

    async def _stac(
        self,
        stac_query: Dict[str, Any],
        stac_endpoint: str = "local_stac",
        original_query: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.calls.append(
            [
                "stac_search",
                {
                    "query": copy.deepcopy(stac_query),
                    "endpoint": stac_endpoint,
                    "original_query": original_query,
                },
            ]
        )
        if self.stac_responses:
            return copy.deepcopy(self.stac_responses.pop(0))
        return results()

    async def _alternatives(
        self,
        original_query: str,
        original_stac_query: Dict[str, Any],
        original_stac_params: Dict[str, Any],
        translator: Any,
        stac_endpoint: str,
        requested_bbox: Optional[List[float]],
    ) -> Dict[str, Any]:
        self.calls.append(
            [
                "try_alternative_queries",
                {
                    "endpoint": stac_endpoint,
                    "bbox": requested_bbox,
                    "original_query": original_query,
                    "original_stac_query": copy.deepcopy(original_stac_query),
                    "original_stac_params": copy.deepcopy(original_stac_params),
                },
            ]
        )
        return copy.deepcopy(self.alternative_result)

    async def _mosaic(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        self.calls.append(["get_mosaic_tilejson_url", copy.deepcopy(kwargs)])
        return copy.deepcopy(self.mosaic_result)

    async def _resolve(self, location: str) -> Any:
        self.calls.append(["resolve_location_to_bbox", location])
        if isinstance(self.location_bbox, Exception):
            raise self.location_bbox
        return copy.deepcopy(self.location_bbox)

    async def _weather(self, **kwargs: Any) -> Any:
        self.calls.append(["extreme_weather.chat", copy.deepcopy(kwargs)])
        if isinstance(self.weather_result, BaseException):
            raise self.weather_result
        return copy.deepcopy(self.weather_result)

    async def _vision(self, **kwargs: Any) -> Any:
        self.calls.append(["vision.analyze", copy.deepcopy(kwargs)])
        if isinstance(self.vision_result, Exception):
            raise self.vision_result
        return copy.deepcopy(self.vision_result)

    def _should_use_vision(self, query: str, conversation_history: List[Any]) -> bool:
        self.calls.append(
            [
                "should_use_vision",
                {
                    "query": query,
                    "conversation_history": copy.deepcopy(conversation_history),
                },
            ]
        )
        return self.keyword_vision


class SnapshotBook:
    """Recorded scenario outputs; rebuilt from scratch in update mode."""

    def __init__(self) -> None:
        self.recorded: Dict[str, Any] = {}
        if not UPDATE and SNAPSHOT_PATH.exists():
            self.recorded = json.loads(SNAPSHOT_PATH.read_text())

    def check(self, name: str, actual: Dict[str, Any]) -> None:
        if UPDATE:
            self.recorded[name] = actual
            return
        assert name in self.recorded, f"no snapshot for {name}; record it first"
        recorded = self.recorded[name]
        if json.dumps(actual, sort_keys=True) != json.dumps(recorded, sort_keys=True):
            assert actual == recorded
            pytest.fail(f"{name}: snapshot differs in value types")

    def save(self) -> None:
        SNAPSHOT_PATH.parent.mkdir(exist_ok=True)
        text = json.dumps(self.recorded, indent=2, sort_keys=True)
        SNAPSHOT_PATH.write_text(text + "\n")


@pytest.fixture(scope="module")
def snapshots() -> Iterator[SnapshotBook]:
    book = SnapshotBook()
    yield book
    if UPDATE:
        book.save()


def defined_here(value: Any) -> bool:
    """True for a function, lambda or instance defined by this test module."""
    if isinstance(value, MethodType):
        value = value.__func__
    if isinstance(value, FunctionType):
        return value.__module__ == __name__
    return type(value).__module__ == __name__


def leaked_fakes() -> Set[str]:
    """Names of module attributes that still hold an object made by this module."""
    leaks = set()
    for module in list(sys.modules.values()):
        if not isinstance(module, ModuleType) or module.__name__ == __name__:
            continue
        for name, value in list(vars(module).items()):
            if defined_here(value):
                leaks.add(f"{module.__name__}.{name}")
    return leaks


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    yield Harness(monkeypatch)
    monkeypatch.undo()
    assert leaked_fakes() == set()


@pytest.fixture
def check(
    snapshots: SnapshotBook, request: pytest.FixtureRequest
) -> Callable[[Dict[str, Any]], None]:
    return lambda result: snapshots.check(request.node.name, result)


def calls_named(result: Dict[str, Any], name: str) -> List[Any]:
    """Payloads of every recorded call with the given name, in order."""
    return [payload for call, payload in result["calls"] if call == name]


# --- intake and keyword mode ---------------------------------------------


def test_keyword_mode_without_translator(harness: Harness, check: Callable) -> None:
    """Current bug: search is skipped because determine_stac_source hits None."""
    result = harness.run(query="Show Sentinel-2 imagery of Paris")
    assert result["response"]["success"] is True
    assert calls_named(result, "stac_search") == []
    check(result)


def test_keyword_mode_quickstart_query_returns_500(
    harness: Harness, check: Callable
) -> None:
    """Current bug: the fast path calls determine_stac_source on None."""
    result = harness.run(query="show sentinel 1 rtc for baltimore", session_id=SESSION)
    assert result["response"] == SERVER_ERROR
    check(result)


def test_ai_mode_selects_model(harness: Harness, check: Callable) -> None:
    harness.ai_mode(stac_params=None, action=STAC_ACTION)
    result = harness.run(
        query="Show Sentinel-2 imagery of Paris", session_id=SESSION, model="gpt-4o"
    )
    assert calls_named(result, "translator.set_model") == ["gpt-4o"]
    check(result)


def test_request_without_session_skips_router(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(stac_params=SENTINEL_PARAMS, action=STAC_ACTION)
    harness.stac_responses = [results(feature("S2A_1"))]
    result = harness.run(query="Show Sentinel-2 imagery of Paris")
    assert calls_named(result, "router.route_query") == []
    check(result)


# --- quickstart fast path ---------------------------------------------------


def test_quickstart_fast_path(harness: Harness, check: Callable) -> None:
    harness.ai_mode()
    harness.stac_responses = [
        results(
            feature(
                "S1_1", "sentinel-1-rtc", [-76.7, 39.2, -76.5, 39.4], expression=True
            ),
            feature("S1_2", "sentinel-1-rtc", [None, 39.2, -76.5, 39.4]),
        )
    ]
    result = harness.run(query="show sentinel 1 rtc for baltimore", session_id=SESSION)
    assert result["response"]["debug"]["fast_path"] is True
    check(result)


def test_quickstart_fast_path_registers_optical_mosaic(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode()
    harness.mosaic_result = {"tilejson_url": "https://mosaic.test/tilejson.json"}
    harness.stac_responses = [
        results(feature("HLS_1", "hls2-l30", [23.3, 37.9, 24.5, 38.8]))
    ]
    result = harness.run(
        query="show harmonized landsat sentinel-2 imagery of athens",
        session_id=SESSION,
    )
    metadata = result["response"]["translation_metadata"]
    assert metadata["mosaic_tilejson"] == harness.mosaic_result
    check(result)


def test_quickstart_fast_path_without_results(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode()
    result = harness.run(
        query="show modis snow cover daily for quebec for january 2025",
        session_id=SESSION,
    )
    assert "no tiles were found" in result["response"]["response"]
    check(result)


# --- RouterAgent: navigate_to and extreme_weather -----------------------------


def test_navigate_to_resolved_location(harness: Harness, check: Callable) -> None:
    harness.ai_mode(action=NAVIGATE_ACTION)
    harness.location_bbox = PARIS_BBOX
    result = harness.run(query="Go to Paris", session_id=SESSION)
    assert result["response"]["processing_type"] == "map_navigation"
    check(result)


def test_navigate_to_unresolved_location(harness: Harness, check: Callable) -> None:
    harness.ai_mode(action=NAVIGATE_ACTION)
    result = harness.run(query="Go to Paris", session_id=SESSION)
    assert result["response"]["processing_type"] == "contextual"
    check(result)


def test_navigate_to_resolver_error(harness: Harness, check: Callable) -> None:
    harness.ai_mode(action=NAVIGATE_ACTION)
    harness.location_bbox = RuntimeError("geocoder down")
    result = harness.run(query="Go to Paris", session_id=SESSION)
    assert "trouble resolving" in result["response"]["response"]
    check(result)


def test_extreme_weather_answer(harness: Harness, check: Callable) -> None:
    harness.ai_mode(action=WEATHER_ACTION)
    harness.location_bbox = [-80.3, 25.7, -80.1, 25.9]
    result = harness.run(query="How hot will Miami get by 2050?", session_id=SESSION)
    assert result["response"]["processing_type"] == "climate_projection"
    assert result["response"]["success"] is True
    check(result)


def test_extreme_weather_timeout(harness: Harness, check: Callable) -> None:
    harness.ai_mode(action=WEATHER_ACTION)
    harness.location_bbox = [-80.3, 25.7, -80.1, 25.9]
    harness.weather_result = asyncio.TimeoutError()
    result = harness.run(query="How hot will Miami get by 2050?", session_id=SESSION)
    assert result["response"]["success"] is False
    check(result)


def test_extreme_weather_uses_session_bbox(harness: Harness, check: Callable) -> None:
    harness.ai_mode(
        action={"action_type": "extreme_weather"},
        session={"last_bbox": PARIS_BBOX, "last_location": "paris"},
    )
    result = harness.run(query="Climate outlook here?", session_id=SESSION)
    assert calls_named(result, "resolve_location_to_bbox") == []
    assert result["response"]["navigate_to"]["location_name"] == "Paris"
    check(result)


def test_extreme_weather_failure_falls_through(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=WEATHER_ACTION)
    harness.location_bbox = [-80.3, 25.7, -80.1, 25.9]
    harness.weather_result = RuntimeError("model down")
    result = harness.run(query="How hot will Miami get by 2050?", session_id=SESSION)
    assert "processing_type" not in result["response"]
    check(result)


def test_router_error_defaults_to_stac(harness: Harness, check: Callable) -> None:
    harness.ai_mode(
        stac_params=SENTINEL_PARAMS, router_error=RuntimeError("router down")
    )
    harness.stac_responses = [results(feature("S2A_1"))]
    result = harness.run(query="Show Sentinel-2 imagery of Paris", session_id=SESSION)
    assert result["response"]["data"]["query_classification"]["confidence"] == 0.5
    check(result)


def test_snapshot_has_no_orphans() -> None:
    """Every recorded snapshot belongs to a scenario that still exists."""
    if UPDATE:
        pytest.skip("recording snapshots")
    scenarios = {
        name
        for name, obj in globals().items()
        if name.startswith("test_")
        and callable(obj)
        and "check" in inspect.signature(obj).parameters
    }
    assert set(json.loads(SNAPSHOT_PATH.read_text())) == scenarios


# --- vision -----------------------------------------------------------------

VISION_ACTION = {"action_type": "vision_analysis"}
CONTEXTUAL_ACTION = {"action_type": "contextual"}
VISION_RESULT = {
    "response": "I see the Seine.",
    "tools_used": ["analyze_screenshot"],
    "tool_calls": [{"tool": "analyze_screenshot"}],
    "context": {"has_screenshot": True, "collections": ["sentinel-2-l2a"]},
}
RENDERED_SESSION = {
    "has_rendered_map": True,
    "last_bbox": PARIS_BBOX,
    "last_location": "Paris",
    "last_collections": ["sentinel-2-l2a"],
    "last_stac_items": [{"id": "S2A_1"}],
}


def test_vision_with_map_context(harness: Harness, check: Callable) -> None:
    harness.ai_mode(action=VISION_ACTION, session=RENDERED_SESSION)
    harness.vision_result = VISION_RESULT
    result = harness.run(
        query="What river is this?",
        session_id=SESSION,
        map_bounds=PARIS_BOUNDS,
        imagery_base64="aGVsbG8=",
        current_collection="landsat-c2-l2",
        tile_urls=["https://tiles.test/1"],
        has_satellite_data=True,
        conversation_history=[{"role": "user", "content": "Show Paris"}],
    )
    assert result["response"]["processing_type"] == "vision_agent"
    check(result)


def test_vision_uses_session_bbox_after_navigation(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(
        action=VISION_ACTION,
        session={
            "last_bbox": PARIS_BBOX,
            "last_location": "Paris",
            "has_rendered_map": False,
        },
    )
    harness.vision_result = VISION_RESULT
    stale = {"west": -100, "south": 30, "east": -90, "north": 40}
    result = harness.run(query="What is here?", session_id=SESSION, map_bounds=stale)
    (vision_call,) = calls_named(result, "vision.analyze")
    assert vision_call["map_bounds"]["west"] == PARIS_BBOX[0]
    check(result)


def test_vision_empty_response_returns_error(harness: Harness, check: Callable) -> None:
    harness.ai_mode(action=VISION_ACTION, session=RENDERED_SESSION)
    harness.vision_result = {"response": ""}
    result = harness.run(
        query="What river is this?", session_id=SESSION, map_bounds=PARIS_BOUNDS
    )
    assert result["response"]["processing_type"] == "vision_agent_error"
    assert result["response"]["success"] is False
    check(result)


def test_vision_agent_exception_returns_error(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=VISION_ACTION, session=RENDERED_SESSION)
    harness.vision_result = RuntimeError("vision down")
    result = harness.run(
        query="What river is this?", session_id=SESSION, map_bounds=PARIS_BOUNDS
    )
    assert result["response"]["error"] == "internal error"
    check(result)


def test_vision_without_map_context_uses_llm_mode(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=VISION_ACTION)
    harness.vision_result = VISION_RESULT
    result = harness.run(query="What is on the map?", session_id=SESSION)
    assert result["response"]["processing_type"] == "vision_agent_llm"
    check(result)


def test_vision_without_map_context_empty_response(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=VISION_ACTION)
    harness.vision_result = None
    result = harness.run(query="What is on the map?", session_id=SESSION)
    assert result["response"]["processing_type"] == "vision_agent_no_context"
    check(result)


def test_vision_without_map_context_exception(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=VISION_ACTION)
    harness.vision_result = RuntimeError("vision down")
    result = harness.run(query="What is on the map?", session_id=SESSION)
    assert result["response"]["processing_type"] == "vision_agent_error"
    assert result["response"]["success"] is True
    check(result)


def test_keyword_detector_routes_contextual_query_to_vision(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=CONTEXTUAL_ACTION, session=RENDERED_SESSION)
    harness.keyword_vision = True
    harness.vision_result = VISION_RESULT
    result = harness.run(
        query="Describe what you see", session_id=SESSION, map_bounds=PARIS_BOUNDS
    )
    detections = calls_named(result, "should_use_vision")
    assert [d["query"] for d in detections] == ["Describe what you see"]
    assert result["response"]["processing_type"] == "vision_agent"
    check(result)


# --- contextual and GIS -------------------------------------------------------


def test_contextual_answer_with_session_location(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=CONTEXTUAL_ACTION, session={"last_location": "Tokyo"})
    result = harness.run(query="How do typhoons form?", session_id=SESSION)
    assert result["response"]["processing_type"] == "contextual_only"
    (prompt,) = calls_named(result, "translator.generate_contextual")
    assert "Tokyo" in prompt["query"]
    check(result)


def test_contextual_answer_with_map_bounds_hint(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=CONTEXTUAL_ACTION, session={"has_rendered_map": True})
    result = harness.run(
        query="What is this area known for?",
        session_id=SESSION,
        map_bounds=PARIS_BOUNDS,
    )
    (prompt,) = calls_named(result, "translator.generate_contextual")
    assert "48.855" in prompt["query"]
    check(result)


def test_contextual_failure_gives_generic_message(
    harness: Harness, check: Callable
) -> None:
    harness.ai_mode(action=CONTEXTUAL_ACTION, contextual_error=RuntimeError("llm down"))
    result = harness.run(query="How do typhoons form?", session_id=SESSION)
    assert calls_named(result, "stac_search") == []
    check(result)


def test_gis_feature_query_answers_contextually(
    harness: Harness, check: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiohttp

    harness.ai_mode(
        action={
            "action_type": "gis_feature_query",
            "collection": "parcels",
            "feature_id": "lot-7",
        }
    )
    payloads = {
        "feature": {"properties": {"owner": "City"}},
        "collections": {"collections": [{"id": "parcels"}]},
    }
    monkeypatch.setattr(
        aiohttp, "ClientSession", lambda: FakeHttpSession(harness.calls, payloads)
    )
    result = harness.run(query="Tell me about lot 7", session_id=SESSION)
    assert result["response"]["processing_type"] == "contextual_only"
    assert len(calls_named(result, "http.get")) == 2
    check(result)


def test_gis_feature_query_lookup_failure(
    harness: Harness, check: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiohttp

    def refuse() -> None:
        raise aiohttp.ClientError("vector api down")

    harness.ai_mode(
        action={
            "action_type": "gis_feature_query",
            "collection": "parcels",
            "feature_id": "lot-7",
        }
    )
    monkeypatch.setattr(aiohttp, "ClientSession", refuse)
    result = harness.run(query="Tell me about lot 7", session_id=SESSION)
    assert result["response"]["processing_type"] == "contextual_only"
    check(result)


# --- hybrid and semantic-kernel-off ---------------------------------------------


def test_hybrid_combines_vision_and_stac(harness: Harness, check: Callable) -> None:
    harness.ai_mode(
        stac_params=SENTINEL_PARAMS,
        action={"action_type": "hybrid"},
        session=RENDERED_SESSION,
    )
    harness.vision_result = {"analysis": "Clouds cover the north.", "response": "x"}
    harness.stac_responses = [results(feature("S2A_1"))]
    result = harness.run(
        query="Load Sentinel-2 and describe it",
        session_id=SESSION,
        map_bounds=PARIS_BOUNDS,
    )
    assert result["response"]["response"].startswith("**Visual Analysis:**")
    check(result)


def test_router_classification_without_semantic_kernel_returns_500(
    harness: Harness, check: Callable
) -> None:
    """Current bug: intent_type is only bound inside the semantic-kernel block."""
    harness.configure(router=FakeRouterAgent(harness.calls, action=STAC_ACTION))
    result = harness.run(query="Show Sentinel-2 imagery of Paris", session_id=SESSION)
    assert result["response"] == SERVER_ERROR
    check(result)
