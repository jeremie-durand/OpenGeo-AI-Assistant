"""
Tests for GenericQueryTranslator JSON-mode enforcement and bbox anchoring.

Covers:
  - _llm_text forwards response_format only when the caller sets it
  - _KNOWN_BBOXES resolves Canadian locations in the keyword fallback
  - _anchor_bbox overrides an inaccurate LLM bbox with the vetted one
  - build_stac_query_agent requests JSON mode and anchors the result
"""

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
