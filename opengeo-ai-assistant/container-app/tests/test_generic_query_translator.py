"""
Tests for GenericQueryTranslator JSON-mode enforcement and bbox anchoring.

Covers:
  - _llm_text forwards response_format only when the caller sets it
  - _KNOWN_BBOXES resolves Canadian locations in the keyword fallback
  - _anchor_bbox overrides an inaccurate LLM bbox with the vetted one
  - build_stac_query_agent requests JSON mode and anchors the result
"""

import sys
from unittest.mock import AsyncMock

import pytest
from generic_query_translator import (
    _KNOWN_BBOXES,
    _anchor_bbox,
    _keyword_extract_stac_params,
    _llm_text,
)


def _fake_client(content: str = "{}") -> AsyncMock:
    """Return a client whose chat() records kwargs and returns *content*."""
    client = AsyncMock()
    client.chat.return_value = {"choices": [{"message": {"content": content}}]}
    return client


# ===========================================================================
# _llm_text — response_format passthrough
# ===========================================================================


class TestLlmTextResponseFormat:
    @pytest.mark.asyncio
    async def test_omitted_when_not_requested(self) -> None:
        """Callers that don't opt in must not send response_format."""
        client = _fake_client("hello")
        await _llm_text(client, [{"role": "user", "content": "hi"}])
        assert "response_format" not in client.chat.call_args.kwargs

    @pytest.mark.asyncio
    async def test_forwarded_when_requested(self) -> None:
        """An explicit response_format reaches the provider unchanged."""
        client = _fake_client("{}")
        await _llm_text(
            client,
            [{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
        )
        kwargs = client.chat.call_args.kwargs
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["max_tokens"] == 512
        assert kwargs["temperature"] == 0.2


# ===========================================================================
# _KNOWN_BBOXES — Canadian coverage
# ===========================================================================


class TestKnownBboxes:
    @pytest.mark.parametrize(
        "name",
        ["canada", "quebec", "québec", "montreal", "sherbrooke", "toronto"],
    )
    def test_canadian_entries_present(self, name: str) -> None:
        west, south, east, north = _KNOWN_BBOXES[name]
        assert -141.0 <= west < east <= -52.0
        assert 41.0 <= south < north <= 84.0

    def test_bare_quebec_is_the_province_not_the_city(self) -> None:
        """'quebec' must span the province; 'quebec city' the city."""
        province = _KNOWN_BBOXES["quebec"]
        city = _KNOWN_BBOXES["quebec city"]
        assert province[2] - province[0] > city[2] - city[0]

    def test_keyword_fallback_resolves_quebec_city(self) -> None:
        """Longest-match must prefer 'quebec city' over the bare province."""
        params = _keyword_extract_stac_params("Sentinel-2 over Quebec City 2023")
        assert params["bbox"] == _KNOWN_BBOXES["quebec city"]
        assert params["datetime"] == "2023-01-01/2023-12-31"


# ===========================================================================
# _anchor_bbox — vetted coordinates win over the LLM's
# ===========================================================================


class TestAnchorBbox:
    def test_known_location_overrides_llm_bbox(self) -> None:
        """An inaccurate bbox for a known location is replaced."""
        result = _anchor_bbox(
            {"location_name": "Sherbrooke", "bbox": [2.25, 48.81, 2.42, 48.91]}
        )
        assert result["bbox"] == _KNOWN_BBOXES["sherbrooke"]

    def test_unknown_location_keeps_llm_bbox(self) -> None:
        """Locations we have no vetted bbox for pass through untouched."""
        llm_bbox = [10.0, 20.0, 11.0, 21.0]
        result = _anchor_bbox({"location_name": "Timbuktu", "bbox": llm_bbox})
        assert result["bbox"] == llm_bbox

    def test_null_location_name_is_safe(self) -> None:
        """A null location_name must not raise."""
        assert _anchor_bbox({"location_name": None, "bbox": None})["bbox"] is None


# ===========================================================================
# build_stac_query_agent — JSON mode + anchoring end to end
# ===========================================================================


class TestBuildStacQueryAgent:
    def _translator(self, content: str):
        from generic_query_translator import GenericQueryTranslator

        translator = GenericQueryTranslator()
        translator._llm = _fake_client(content)
        return translator

    @pytest.mark.asyncio
    async def test_requests_json_mode(self) -> None:
        """Small local models ignore prompt-level JSON instructions."""
        translator = self._translator('{"location_name": "Paris", "bbox": null}')
        await translator.build_stac_query_agent("imagery of Paris", ["sentinel-2"])
        kwargs = translator._llm.chat.call_args.kwargs
        assert kwargs["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_anchors_inaccurate_llm_bbox(self) -> None:
        """A hallucinated bbox for a known location is corrected."""
        translator = self._translator(
            '{"location_name": "Sherbrooke", "bbox": [0.0, 0.0, 1.0, 1.0]}'
        )
        result = await translator.build_stac_query_agent("Sherbrooke", ["sentinel-2"])
        assert result["bbox"] == _KNOWN_BBOXES["sherbrooke"]


# ===========================================================================
# collection_mapping_agent — keyword fallback must not import fastapi_app
# ===========================================================================


class TestCollectionMappingFallback:
    @pytest.mark.asyncio
    async def test_fallback_works_without_fastapi_app(self, monkeypatch) -> None:
        """The keyword fallback must not depend on the web app module."""
        from generic_query_translator import GenericQueryTranslator

        monkeypatch.setitem(sys.modules, "fastapi_app", None)
        translator = GenericQueryTranslator()
        translator._llm = _fake_client("not json")
        translator._discover_stac_collections = AsyncMock(return_value=[])
        result = await translator.collection_mapping_agent("wildfire near Sherbrooke")
        assert result == ["landsat-c2-l2", "sentinel-2-l2a", "modis-14A1-061"]


# ===========================================================================
# generate_contextual_earth_science_response — dashboard figures section
# ===========================================================================


class TestContextualDashboardPrompt:
    HINT = "[Dashboard shown to the user: modis-10A2-061; scenes per month: ...]"

    async def _prompt(self, **kwargs) -> str:
        from generic_query_translator import GenericQueryTranslator

        translator = GenericQueryTranslator()
        translator._llm = _fake_client("answer")
        await translator.generate_contextual_earth_science_response(
            "why so few scenes in March?", {}, **kwargs
        )
        return translator._llm.chat.call_args.args[0][0]["content"]

    @pytest.mark.asyncio
    async def test_dashboard_figures_get_their_own_section(self) -> None:
        """The figures must not be read as the user's words or as 0 results."""
        prompt = await self._prompt(dashboard_hint=self.HINT)
        assert 'USER QUERY: "why so few scenes in March?"' in prompt
        assert f"DASHBOARD SHOWN TO THE USER: {self.HINT}" in prompt
        assert "quote the relevant numbers" in prompt
        assert "RESULTS:" not in prompt

    @pytest.mark.asyncio
    async def test_prompt_without_dashboard_is_unchanged(self) -> None:
        prompt = await self._prompt()
        assert "RESULTS: 0 satellite data item(s) found." in prompt
        assert "DASHBOARD" not in prompt


# ===========================================================================
# collection_mapping_agent — candidates from vector search (add-vector-search)
# ===========================================================================


class _HitsBackend:
    """Vector backend returning fixed hits, optionally slow."""

    read_only = True

    def __init__(self, ids, delay: float = 0.0) -> None:
        self.ids = ids
        self.delay = delay

    async def search(self, query):
        import asyncio

        from services.vector_search.backends.base import BackendHit

        await asyncio.sleep(self.delay)
        return [
            BackendHit(id=cid, score=1.0 - i / 100, title=cid.upper())
            for i, cid in enumerate(self.ids)
        ]


def _ready_index(ids, delay: float = 0.0, private=None, timeout: str = "5"):
    """Install a ready vector index (HTTP retriever) returning *ids*."""
    from services.vector_search import index as vector_index
    from services.vector_search.index import STATUS_READY, VectorIndex
    from services.vector_search.settings import VectorSearchSettings

    settings = VectorSearchSettings.from_env(
        {
            "VECTOR_SEARCH_URL": "https://retriever.example/search",
            "VECTOR_SEARCH_TIMEOUT_SECONDS": timeout,
        }
    )
    index = VectorIndex(settings, backend=_HitsBackend(ids, delay), stac_api_url="")
    index.status = STATUS_READY
    index.private_renderable = set(private or ())
    vector_index._index = index
    return index


def _translator(llm_content: str, local_ids=None):
    from generic_query_translator import GenericQueryTranslator

    translator = GenericQueryTranslator()
    translator._llm = _fake_client(llm_content)
    discovered = [{"id": cid, "title": cid} for cid in (local_ids or [])]
    translator._discover_stac_collections = AsyncMock(return_value=discovered)
    return translator


def _prompt(translator) -> str:
    return translator._llm.chat.call_args.args[0][0]["content"]


SPREAD = ["ms-buildings", "mtbs", "naip", "esa-worldcover"]


class TestCollectionSelectionWithVectorSearch:
    @pytest.mark.asyncio
    async def test_retrieved_collection_is_offered(self) -> None:
        _ready_index(SPREAD)
        translator = _translator('["ms-buildings"]')
        result = await translator.collection_mapping_agent(
            "show me building footprints in Denver"
        )
        prompt = _prompt(translator)
        assert result == ["ms-buildings"]
        assert "ms-buildings : MS-BUILDINGS" in prompt
        assert "hls2-s30" not in prompt

    @pytest.mark.asyncio
    async def test_defaults_always_offered(self) -> None:
        _ready_index(["esa-worldcover"])
        translator = _translator('["sentinel-2-l2a"]')
        assert await translator.collection_mapping_agent("show me Paris") == [
            "sentinel-2-l2a"
        ]
        prompt = _prompt(translator)
        assert "sentinel-2-l2a : " in prompt and "landsat-c2-l2 : " in prompt

    @pytest.mark.asyncio
    async def test_only_renderable_hits_offered_and_max_twelve(self) -> None:
        from services.dashboard_catalog import renderable_collection_ids

        ids = ["private-unrenderable"] + renderable_collection_ids()[:20]
        _ready_index(ids)
        translator = _translator('["private-unrenderable"]')
        result = await translator.collection_mapping_agent("anything")
        offered = [
            line
            for line in _prompt(translator).splitlines()
            if " : " in line and not line.startswith("AVAILABLE")
        ]
        assert "private-unrenderable" not in _prompt(translator)
        assert len(offered) == 14  # 12 hits plus the two defaults
        assert "private-unrenderable" not in result

    @pytest.mark.asyncio
    async def test_unoffered_ids_discarded_and_max_three(self) -> None:
        _ready_index(SPREAD)
        translator = _translator(
            '["jrc-gsw-occurrence", "mtbs", "naip", "esa-worldcover", "ms-buildings"]'
        )
        result = await translator.collection_mapping_agent("fires")
        assert result == ["mtbs", "naip", "esa-worldcover"]

    @pytest.mark.asyncio
    async def test_nothing_usable_falls_back_to_best_hits(self) -> None:
        _ready_index(SPREAD)
        translator = _translator('["jrc-gsw-occurrence"]')
        result = await translator.collection_mapping_agent("water")
        assert result == ["ms-buildings", "mtbs", "naip"]

    @pytest.mark.asyncio
    async def test_llm_error_uses_top_three_hits(self) -> None:
        _ready_index(SPREAD)
        translator = _translator("")
        translator._llm.chat.side_effect = RuntimeError("LLM down")
        result = await translator.collection_mapping_agent("burn severity")
        assert result == ["ms-buildings", "mtbs", "naip"]

    @pytest.mark.asyncio
    async def test_no_hits_and_llm_error_uses_keyword_rules(self) -> None:
        _ready_index([])
        translator = _translator("")
        translator._llm.chat.side_effect = RuntimeError("LLM down")
        result = await translator.collection_mapping_agent("wildfire near Sherbrooke")
        assert result == ["landsat-c2-l2", "sentinel-2-l2a", "modis-14A1-061"]

    @pytest.mark.asyncio
    async def test_disabled_uses_fixed_list(self) -> None:
        from generic_query_translator import FALLBACK_COLLECTION_LIST

        translator = _translator('["hls2-s30"]')
        assert await translator.collection_mapping_agent("q") == ["hls2-s30"]
        assert FALLBACK_COLLECTION_LIST in _prompt(translator)

    @pytest.mark.asyncio
    async def test_building_and_llm_failure_uses_keyword_rules(self) -> None:
        index = _ready_index(SPREAD)
        index.status = "building"
        translator = _translator("not json")
        result = await translator.collection_mapping_agent("wildfire near Sherbrooke")
        assert result == ["landsat-c2-l2", "sentinel-2-l2a", "modis-14A1-061"]

    @pytest.mark.asyncio
    async def test_retrieval_timeout_uses_fixed_list_without_status_change(
        self,
    ) -> None:
        from generic_query_translator import FALLBACK_COLLECTION_LIST

        index = _ready_index(SPREAD, delay=1.0, timeout="0.05")
        translator = _translator('["naip"]')
        assert await translator.collection_mapping_agent("aerial") == ["naip"]
        assert FALLBACK_COLLECTION_LIST in _prompt(translator)
        assert index.status == "ready"

    @pytest.mark.asyncio
    async def test_private_collection_routed_to_private_stac(self) -> None:
        _ready_index(
            ["orthophotos-sherbrooke-2024", "naip"],
            private={"orthophotos-sherbrooke-2024"},
        )
        translator = _translator(
            '["orthophotos-sherbrooke-2024"]', local_ids=["orthophotos-sherbrooke-2024"]
        )
        result = await translator.collection_mapping_agent(
            "aerial photos of Sherbrooke"
        )
        assert result == ["orthophotos-sherbrooke-2024"]
        assert translator.determine_stac_source("q", {"collections": result}) == (
            "local_stac"
        )

    @pytest.mark.asyncio
    async def test_mixed_sources_keep_first_api_only(self) -> None:
        _ready_index(
            ["orthophotos-sherbrooke-2024"], private={"orthophotos-sherbrooke-2024"}
        )
        translator = _translator(
            '["orthophotos-sherbrooke-2024", "sentinel-2-l2a"]',
            local_ids=["orthophotos-sherbrooke-2024"],
        )
        result = await translator.collection_mapping_agent("aerial photos")
        assert result == ["orthophotos-sherbrooke-2024"]


FIXED_LIST_UNRENDERABLE = {"nasa-nex-gddp-cmip6"}


def test_fixed_list_ids_are_renderable_collections() -> None:
    from generic_query_translator import FALLBACK_COLLECTION_LIST
    from services.dashboard_catalog import renderable_collection_ids

    ids = {line.split(" : ")[0] for line in FALLBACK_COLLECTION_LIST.splitlines()}
    unknown = ids - set(renderable_collection_ids()) - FIXED_LIST_UNRENDERABLE
    assert unknown == set()
    assert {"jrc-gsw", "noaa-cdr-sea-surface-temperature-whoi"} <= ids
