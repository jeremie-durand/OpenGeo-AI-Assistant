"""
GenericQueryTranslator
======================
A drop-in replacement for SemanticQueryTranslator that works with any LLM
provider configured via LLM_PROVIDER / LLM_API_KEY / LLM_MODEL environment
variables (openai or anthropic).

Exposes the same public interface used by fastapi_app.py:
  - translate_query(query, pin_location, session_bbox)
  - collection_mapping_agent(query)
  - build_stac_query_agent(query, collections)
  - datetime_translation_agent(query, collections, mode)
  - generate_contextual_earth_science_response(query, classification, stac_response, geoint_results)
  - generate_empty_result_response(query, stac_query, collections, diagnostics)
  - generate_alternative_result_response(...)
  - get_conversation_context(conversation_id)
  - reset_conversation_context(conversation_id)
  - set_model(model_name)
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> Any:
    """Extract and parse the first JSON object/array found in *text*."""
    text = text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    # Try the whole string first; fall back to a greedy object search.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


async def _llm_text(client, messages: List[Dict], max_tokens: int = 512, temperature: float = 0.2) -> str:
    """Call *client* (LLMClient) and return the assistant text."""
    response = await client.chat(messages, max_tokens=max_tokens, temperature=temperature)
    if isinstance(response, dict):
        content_blocks = response.get("content", [])
        if content_blocks and isinstance(content_blocks, list):
            item = content_blocks[0]
            return item.get("text", "") if isinstance(item, dict) else str(item)
        choices = response.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
    return str(response)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class GenericQueryTranslator:
    """Provider-agnostic query translator used when Azure/SK is not configured."""

    def __init__(self):
        from llm_client import get_llm_client as _get
        self._compat = _get()          # OpenAICompatClient
        self._llm = self._compat._llm  # raw LLMClient
        self.conversation_contexts: Dict[str, Any] = {}
        self._model_override: Optional[str] = None

    # ------------------------------------------------------------------
    # Public interface — model switching
    # ------------------------------------------------------------------

    def set_model(self, model_name: str) -> None:
        if model_name and model_name != self._model_override:
            logger.info(f"[GQT] Model override: {self._model_override} -> {model_name}")
            self._model_override = model_name

    def get_active_model(self) -> str:
        return self._model_override or self._llm.model

    # ------------------------------------------------------------------
    # Conversation context (mirrors SemanticQueryTranslator)
    # ------------------------------------------------------------------

    def get_conversation_context(self, conversation_id: str) -> Dict[str, Any]:
        if conversation_id not in self.conversation_contexts:
            self.conversation_contexts[conversation_id] = {
                "session_id": conversation_id,
                "query_count": 0,
                "queries": [],
                "responses": [],
                "last_map_data": None,
                "last_location": None,
                "last_collections": [],
                "last_bbox": None,
                "context_topics": [],
                "session_start": datetime.now(),
                "has_rendered_map": False,
                "chat_history": [],
            }
        return self.conversation_contexts[conversation_id]

    def reset_conversation_context(self, conversation_id: str) -> None:
        self.conversation_contexts.pop(conversation_id, None)

    # ------------------------------------------------------------------
    # Agent 1 — collection mapping
    # ------------------------------------------------------------------

    async def collection_mapping_agent(self, query: str) -> List[str]:
        """Return a list of STAC collection IDs relevant to *query*."""
        prompt = f"""You are a geospatial data assistant. Given a user query, return the most relevant STAC satellite/geospatial collection IDs from the list below.

AVAILABLE COLLECTIONS (ID : description):
sentinel-2-l2a : Sentinel-2 optical imagery (10 m)
landsat-c2-l2 : Landsat Collection 2 Level-2 (30 m)
hls2-s30 : Harmonized Landsat-Sentinel (Sentinel) 30 m
hls2-l30 : Harmonized Landsat-Sentinel (Landsat) 30 m
naip : NAIP aerial imagery (US, <1 m)
modis-09A1-061 : MODIS surface reflectance 8-day 500 m
modis-09Q1-061 : MODIS surface reflectance 8-day 250 m
cop-dem-glo-30 : Copernicus DEM 30 m
cop-dem-glo-90 : Copernicus DEM 90 m
alos-dem : ALOS DEM 30 m
3dep-lidar-hag : USGS 3DEP LiDAR height above ground
sentinel-1-rtc : Sentinel-1 SAR RTC
sentinel-1-grd : Sentinel-1 SAR GRD
modis-14A1-061 : MODIS thermal anomalies / fire daily
modis-14A2-061 : MODIS fire 8-day
modis-64A1-061 : MODIS burned area monthly
modis-10A1-061 : MODIS snow cover daily
modis-10A2-061 : MODIS snow cover 8-day
modis-13Q1-061 : MODIS NDVI/EVI 250 m 16-day
modis-13A1-061 : MODIS NDVI/EVI 500 m 16-day
modis-11A1-061 : MODIS land surface temperature daily
modis-11A2-061 : MODIS land surface temperature 8-day
esa-worldcover : ESA WorldCover land cover 10 m
io-lulc-9-class : IO/Esri land use land cover
usda-cdl : USDA cropland data layer
jrc-gsw-occurrence : JRC global surface water
noaa-cdr-sea-surface-temp-whoi : NOAA sea surface temperature
noaa-mrms-qpe-1h-pass1 : NOAA MRMS hourly precipitation
nasa-nex-gddp-cmip6 : NASA NEX GDDP CMIP6 climate projections
chloris-biomass : Chloris above-ground biomass

USER QUERY: "{query}"

Return ONLY a JSON array of collection IDs (max 3), most relevant first.
Example: ["sentinel-2-l2a", "landsat-c2-l2"]"""

        try:
            raw = await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=128,
                temperature=0.0,
            )
            result = _parse_json(raw)
            if isinstance(result, list):
                return [str(c) for c in result]
            logger.warning(f"[GQT] collection_mapping_agent unexpected shape: {result}")
        except Exception as exc:
            logger.error(f"[GQT] collection_mapping_agent failed: {exc}")
        # Deterministic keyword fallback
        from fastapi_app import detect_collections
        return detect_collections(query)

    # ------------------------------------------------------------------
    # Agent 2 — STAC query builder (location + datetime extraction)
    # ------------------------------------------------------------------

    async def build_stac_query_agent(
        self, query: str, collections: List[str]
    ) -> Dict[str, Any]:
        """Extract bbox, datetime, and other STAC params from *query*."""
        prompt = f"""You are a geospatial query parser. Extract the geographic location and time range from the user query below.

USER QUERY: "{query}"
COLLECTIONS: {json.dumps(collections)}

Return ONLY valid JSON with these fields:
{{
  "location_name": "<place name or null>",
  "bbox": [west, south, east, north] or null,
  "datetime": "<ISO 8601 range like 2024-01-01/2024-12-31, or null>",
  "cloud_cover": <integer 0-100 or null>
}}

For bbox: use approximate WGS-84 coordinates. If you know the location return the bbox.
Examples:
- "Paris" -> {{"location_name": "Paris", "bbox": [2.25, 48.81, 2.42, 48.91], "datetime": null, "cloud_cover": null}}
- "California fires 2020" -> {{"location_name": "California", "bbox": [-124.4, 32.5, -114.1, 42.0], "datetime": "2020-01-01/2020-12-31", "cloud_cover": null}}
- "Sentinel-2 of Tokyo under 10% clouds" -> {{"location_name": "Tokyo", "bbox": [139.5, 35.5, 140.0, 35.9], "datetime": null, "cloud_cover": 10}}"""

        try:
            raw = await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=256,
                temperature=0.0,
            )
            result = _parse_json(raw)
            if isinstance(result, dict):
                return result
        except Exception as exc:
            logger.error(f"[GQT] build_stac_query_agent failed: {exc}")
        return {"location_name": None, "bbox": None, "datetime": None, "cloud_cover": None}

    # ------------------------------------------------------------------
    # Agent 3 — datetime extraction
    # ------------------------------------------------------------------

    async def datetime_translation_agent(
        self,
        query: str,
        collections: List[str],
        mode: str = "single",
    ) -> Dict[str, Any]:
        """Extract temporal information from *query*."""
        if mode == "comparison":
            prompt = f"""Extract a BEFORE and AFTER time range from this change-detection query.

QUERY: "{query}"

Return ONLY JSON:
{{"before": "YYYY-MM-DD/YYYY-MM-DD", "after": "YYYY-MM-DD/YYYY-MM-DD"}}

If no explicit dates exist, infer reasonable ranges from context (e.g. "before the fire" = several months prior)."""
        else:
            prompt = f"""Extract the time range from this geospatial query.

QUERY: "{query}"

Return ONLY JSON:
{{"datetime": "YYYY-MM-DD/YYYY-MM-DD or null"}}

Use ISO 8601 range format. If no date mentioned return null."""

        try:
            raw = await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=128,
                temperature=0.0,
            )
            return _parse_json(raw)
        except Exception as exc:
            logger.error(f"[GQT] datetime_translation_agent failed: {exc}")
            return {"datetime": None}

    # ------------------------------------------------------------------
    # Master translate_query — orchestrates agents 1-3
    # ------------------------------------------------------------------

    async def translate_query(
        self,
        natural_query: str,
        pin_location: Optional[Dict[str, float]] = None,
        session_bbox: Optional[List[float]] = None,
        skip_intent_classification: bool = False,
        pre_classified_intent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Translate *natural_query* to STAC parameters."""
        logger.info(f"[GQT] translate_query: '{natural_query}'")

        collections = await self.collection_mapping_agent(natural_query)
        if not collections:
            return {
                "error": "LOCATION_REQUIRED",
                "message": "Could not determine relevant data collections. Please be more specific.",
                "suggestions": [],
            }

        stac_info = await self.build_stac_query_agent(natural_query, collections)

        bbox = stac_info.get("bbox")
        location_name = stac_info.get("location_name")
        datetime_range = stac_info.get("datetime")
        cloud_cover = stac_info.get("cloud_cover")

        # Pin overrides location if provided
        if pin_location and not bbox:
            lat, lng = pin_location.get("lat", 0), pin_location.get("lng", 0)
            delta = 0.5
            bbox = [lng - delta, lat - delta, lng + delta, lat + delta]
            location_name = location_name or f"({lat:.4f}, {lng:.4f})"

        # Session fallback when no bbox extracted
        if not bbox and session_bbox:
            bbox = session_bbox

        if not bbox and not location_name:
            return {
                "error": "LOCATION_REQUIRED",
                "message": "Please specify a location for your search.",
                "suggestions": [],
            }

        # If we have a name but no bbox, resolve via geocoder
        if not bbox and location_name:
            try:
                from location_resolver import EnhancedLocationResolver
                resolver = EnhancedLocationResolver()
                bbox = await resolver.resolve_location_to_bbox(location_name)
            except Exception as exc:
                logger.warning(f"[GQT] location resolution failed: {exc}")

        params: Dict[str, Any] = {
            "collections": collections,
            "bbox": bbox,
            "datetime": datetime_range,
            "location_name": location_name,
            "limit": 50,
        }
        if cloud_cover is not None:
            params["cloud_cover"] = cloud_cover

        logger.info(f"[GQT] translate_query result: collections={collections}, bbox={bbox}, datetime={datetime_range}")
        return params

    # ------------------------------------------------------------------
    # Response generation
    # ------------------------------------------------------------------

    async def generate_contextual_earth_science_response(
        self,
        natural_query: str,
        classification: Dict[str, Any],
        stac_response: Optional[Dict[str, Any]] = None,
        geoint_results: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate a natural language response describing the STAC results."""
        features = (stac_response or {}).get("results", {}).get("features", []) if stac_response else []
        count = len(features)

        # Build a brief data summary
        data_lines: List[str] = []
        for f in features[:5]:
            props = f.get("properties", {})
            dt = props.get("datetime") or props.get("start_datetime", "")
            col = f.get("collection", "")
            bbox = f.get("bbox", [])
            data_lines.append(f"  - {col} | {dt[:10] if dt else 'N/A'} | bbox={bbox}")
        data_summary = "\n".join(data_lines) if data_lines else "No results."

        prompt = f"""You are an Earth observation assistant. Provide a concise, helpful response (2-4 sentences) for the user's geospatial query.

USER QUERY: "{natural_query}"
RESULTS: {count} satellite data item(s) found.
SAMPLE DATA:
{data_summary}

Guidelines:
- If results were found, briefly describe what data was loaded and its coverage/dates.
- If no results, explain likely reasons and suggest what to try.
- Be factual and concise. Do not repeat the query verbatim."""

        try:
            message = await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.4,
            )
        except Exception as exc:
            logger.error(f"[GQT] generate_contextual_earth_science_response failed: {exc}")
            message = (
                f"Found {count} result(s) for your query."
                if count > 0
                else "No results found. Try adjusting the location or time range."
            )

        return {
            "message": message,
            "query_type": "contextual_earth_science",
            "has_satellite_data": count > 0,
            "has_contextual_analysis": True,
        }

    async def generate_empty_result_response(
        self,
        natural_query: str,
        stac_query: Dict[str, Any],
        collections: List[str],
        diagnostics: Dict[str, Any],
    ) -> str:
        stage = diagnostics.get("failure_stage", "unknown")
        prompt = f"""You are an Earth observation assistant. Explain why no satellite data was found and suggest next steps.

USER QUERY: "{natural_query}"
COLLECTIONS SEARCHED: {json.dumps(collections)}
FAILURE STAGE: {stage}
DIAGNOSTICS: {json.dumps(diagnostics)}

Provide a 2-3 sentence helpful response. Be specific about what might have gone wrong."""

        try:
            return await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.4,
            )
        except Exception as exc:
            logger.error(f"[GQT] generate_empty_result_response failed: {exc}")
            return f"No results found for '{natural_query}'. Try adjusting the location name, time range, or collection type."

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
        features = stac_response.get("results", {}).get("features", [])
        count = len(features)
        prompt = f"""You are an Earth observation assistant. Explain that the original search had no results but alternatives were found.

USER QUERY: "{natural_query}"
ALTERNATIVE RESULTS: {count} item(s) found.
CHANGE: {explanation}

Write 2-3 sentences explaining what was changed and what was found."""

        try:
            message = await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.4,
            )
        except Exception as exc:
            logger.error(f"[GQT] generate_alternative_result_response failed: {exc}")
            message = f"Showing {count} alternative result(s): {explanation}"

        return {"message": message, "query_type": "alternative_results"}
