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
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_json_text(text: str) -> str:
    """Replace Python literals with their JSON equivalents."""
    # Must replace whole-word occurrences only to avoid mangling strings
    text = re.sub(r'\bNone\b', 'null', text)
    text = re.sub(r'\bTrue\b', 'true', text)
    text = re.sub(r'\bFalse\b', 'false', text)
    return text


def _parse_json(text: str) -> Any:
    """Extract and parse the first JSON object/array found in *text*.

    Handles:
    - Markdown code fences (```json ... ```)
    - Python literals: None/True/False → null/true/false
    - Single-quoted Python dicts: {'key': 'val'} via ast.literal_eval
    """
    import ast

    text = text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    text = _normalize_json_text(text)

    # 1. Try standard JSON parse on the whole string.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Try extracting the first {...} or [...] block and re-parsing.
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            candidate = _normalize_json_text(match.group())
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                # 3. Fall back to ast.literal_eval for Python-style dicts/lists.
                try:
                    return ast.literal_eval(candidate)
                except (ValueError, SyntaxError):
                    pass

    # 4. Last resort: ast.literal_eval on the full text.
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        pass

    raise json.JSONDecodeError("Could not parse LLM output as JSON", text, 0)


async def _llm_text(client, messages: List[Dict], max_tokens: int = 512, temperature: float = 0.2) -> str:
    """Call *client* (LLMClient) and return the assistant text."""
    try:
        response = await client.chat(messages, max_tokens=max_tokens, temperature=temperature)
    except Exception as exc:
        logger.error(f"[GQT] _llm_text chat() raised: {type(exc).__name__}: {exc}")
        return ""
    if isinstance(response, dict):
        content_blocks = response.get("content", [])
        if content_blocks and isinstance(content_blocks, list):
            item = content_blocks[0]
            return item.get("text", "") if isinstance(item, dict) else str(item)
        choices = response.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content") if isinstance(msg, dict) else None
            if not content:
                logger.warning(f"[GQT] _llm_text got null/empty content, finish_reason={choices[0].get('finish_reason')!r}")
            return content or ""
    logger.warning(f"[GQT] _llm_text unexpected response shape: {str(response)[:200]}")
    return ""

# ---------------------------------------------------------------------------
# Keyword-based STAC parameter extractor (fallback when LLM fails)
# ---------------------------------------------------------------------------

# Well-known location bboxes [west, south, east, north]
_KNOWN_BBOXES: Dict[str, List[float]] = {
    "france": [-5.14, 41.33, 9.56, 51.09],
    "germany": [5.87, 47.27, 15.04, 55.06],
    "spain": [-9.39, 35.95, 4.32, 43.75],
    "italy": [6.63, 36.62, 18.52, 47.09],
    "uk": [-8.62, 49.86, 1.77, 60.86],
    "united kingdom": [-8.62, 49.86, 1.77, 60.86],
    "usa": [-125.0, 24.5, -66.9, 49.4],
    "united states": [-125.0, 24.5, -66.9, 49.4],
    "brazil": [-73.99, -33.75, -28.85, 5.27],
    "india": [68.18, 7.97, 97.40, 35.67],
    "china": [73.50, 18.20, 134.77, 53.55],
    "australia": [113.34, -43.64, 153.57, -10.67],
    "africa": [-17.63, -34.83, 51.28, 37.35],
    "europe": [-10.66, 34.58, 34.60, 71.19],
    "california": [-124.41, 32.53, -114.13, 42.01],
    "paris": [2.25, 48.81, 2.42, 48.91],
    "london": [-0.51, 51.28, 0.33, 51.69],
    "new york": [-74.26, 40.50, -73.70, 40.92],
    "tokyo": [139.50, 35.50, 140.00, 35.90],
    "amazon": [-73.99, -9.00, -44.00, 2.00],
    "sahara": [-17.00, 15.00, 37.00, 35.00],
}


def _keyword_extract_stac_params(query: str) -> Dict[str, Any]:
    """Extract bbox, datetime, and location_name from query text without LLM."""
    q_lower = query.lower()

    # Extract year(s) — pick the first 4-digit year in [1970, 2030]
    years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", query) if 1970 <= int(y) <= 2030]
    datetime_range: Optional[str] = None
    if len(years) >= 2:
        years_sorted = sorted(years)
        datetime_range = f"{years_sorted[0]}-01-01/{years_sorted[-1]}-12-31"
    elif len(years) == 1:
        datetime_range = f"{years[0]}-01-01/{years[0]}-12-31"

    # Match against known locations (longest match wins)
    bbox: Optional[List[float]] = None
    location_name: Optional[str] = None
    best_len = 0
    for name, coords in _KNOWN_BBOXES.items():
        if name in q_lower and len(name) > best_len:
            bbox = coords
            location_name = name.title()
            best_len = len(name)

    # Cloud cover
    cc_match = re.search(r"(\d+)\s*%?\s*cloud", q_lower)
    cloud_cover: Optional[int] = int(cc_match.group(1)) if cc_match else None

    logger.info(f"[GQT] keyword fallback: location={location_name}, bbox={bbox}, datetime={datetime_range}")
    return {
        "location_name": location_name,
        "bbox": bbox,
        "datetime": datetime_range,
        "cloud_cover": cloud_cover,
    }


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class GenericQueryTranslator:
    """Provider-agnostic query translator used when LLM_PROVIDER is set to "generic"."""

    def __init__(self):
        from llm_client import get_llm_client as _get
        self._compat = _get()          # OpenAICompatClient
        self._llm = self._compat._llm  # raw LLMClient
        self.conversation_contexts: Dict[str, Any] = {}
        self._model_override: Optional[str] = None
        self._local_collection_ids: Optional[set] = None  # cached after first discovery

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

    async def _discover_stac_collections(self) -> Optional[List[Dict[str, str]]]:
        """Fetch available collections from the configured local STAC API.

        Returns a list of {"id": ..., "title": ...} dicts, or None if not configured.
        """
        import os
        stac_url = os.getenv("STAC_API_URL", "").strip()
        if not stac_url:
            return None
        # Derive the /collections endpoint from the search URL
        base = stac_url.rsplit("/search", 1)[0].rstrip("/")
        collections_url = f"{base}/collections"
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.get(collections_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        cols = data.get("collections", [])
                        return [{"id": c["id"], "title": c.get("title", c["id"])} for c in cols if "id" in c]
        except Exception as exc:
            logger.warning(f"[GQT] Could not discover STAC collections from {collections_url}: {exc}")
        return None

    async def collection_mapping_agent(self, query: str) -> List[str]:
        """Return a list of PC-compatible STAC collection IDs relevant to *query*.

        The query pipeline always uses Planetary Computer collection IDs (e.g.
        "sentinel-2-l2a").  Local STAC discovery is stored separately in
        _local_collection_ids and used ONLY by determine_stac_source to decide
        whether to route to the local endpoint — and only when the local STAC
        uses the exact same collection ID as PC (e.g. "sentinel-2-l2a").
        Collections with non-PC IDs (e.g. "sentinel2_eo_products") never match
        and the query goes to Planetary Computer automatically.
        """
        # Discover local collections for routing purposes only — not for prompting.
        discovered = await self._discover_stac_collections()
        self._local_collection_ids = {c["id"] for c in discovered} if discovered else None

        # Always prompt the LLM with PC collection IDs so the returned IDs are
        # always valid for Planetary Computer regardless of local STAC state.
        collection_list = (
            "sentinel-2-l2a : Sentinel-2 optical imagery (10 m)\n"
            "landsat-c2-l2 : Landsat Collection 2 Level-2 (30 m)\n"
            "hls2-s30 : Harmonized Landsat-Sentinel (Sentinel) 30 m\n"
            "hls2-l30 : Harmonized Landsat-Sentinel (Landsat) 30 m\n"
            "naip : NAIP aerial imagery (US, <1 m)\n"
            "modis-09A1-061 : MODIS surface reflectance 8-day 500 m\n"
            "modis-09Q1-061 : MODIS surface reflectance 8-day 250 m\n"
            "cop-dem-glo-30 : Copernicus DEM 30 m\n"
            "cop-dem-glo-90 : Copernicus DEM 90 m\n"
            "alos-dem : ALOS DEM 30 m\n"
            "3dep-lidar-hag : USGS 3DEP LiDAR height above ground\n"
            "sentinel-1-rtc : Sentinel-1 SAR RTC\n"
            "sentinel-1-grd : Sentinel-1 SAR GRD\n"
            "modis-14A1-061 : MODIS thermal anomalies / fire daily\n"
            "modis-14A2-061 : MODIS fire 8-day\n"
            "modis-64A1-061 : MODIS burned area monthly\n"
            "modis-10A1-061 : MODIS snow cover daily\n"
            "modis-10A2-061 : MODIS snow cover 8-day\n"
            "modis-13Q1-061 : MODIS NDVI/EVI 250 m 16-day\n"
            "modis-13A1-061 : MODIS NDVI/EVI 500 m 16-day\n"
            "modis-11A1-061 : MODIS land surface temperature daily\n"
            "modis-11A2-061 : MODIS land surface temperature 8-day\n"
            "esa-worldcover : ESA WorldCover land cover 10 m\n"
            "io-lulc-9-class : IO/Esri land use land cover\n"
            "usda-cdl : USDA cropland data layer\n"
            "jrc-gsw-occurrence : JRC global surface water\n"
            "noaa-cdr-sea-surface-temp-whoi : NOAA sea surface temperature\n"
            "noaa-mrms-qpe-1h-pass1 : NOAA MRMS hourly precipitation\n"
            "nasa-nex-gddp-cmip6 : NASA NEX GDDP CMIP6 climate projections\n"
            "chloris-biomass : Chloris above-ground biomass"
        )

        prompt = f"""You are a geospatial data assistant. Given a user query, return the most relevant STAC satellite/geospatial collection IDs from the list below.

AVAILABLE COLLECTIONS (ID : description):
{collection_list}

These are the collections available on Planetary Computer.

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

        # Keyword fallback — always returns PC collection IDs.
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

        # Keyword fallback — extract year and location name from query text
        logger.info("[GQT] build_stac_query_agent using keyword fallback")
        return _keyword_extract_stac_params(query)

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

    def determine_stac_source(self, query: str, entities: Dict[str, Any]) -> str:
        """Route to the appropriate STAC endpoint.

        Uses the local/private STAC API only when STAC_API_URL is configured AND
        the requested collections are actually present there (based on the cache
        populated by collection_mapping_agent).  Falls back to Planetary Computer
        when the collection is not available locally.
        """
        if not os.getenv("STAC_API_URL", "").strip():
            return "planetary_computer"

        requested = entities.get("collections", [])
        local_ids = self._local_collection_ids

        # No cache yet (collection_mapping_agent hasn't run or failed) — use PC
        # as a safe default since common collections (Landsat, Sentinel) live there.
        if local_ids is None:
            return "planetary_computer"

        # Empty local catalog — nothing to search locally.
        if not local_ids:
            return "planetary_computer"

        if requested and any(c in local_ids for c in requested):
            return "local_stac"
        return "planetary_computer"

    # ------------------------------------------------------------------
    # Spatial / cloud-cover filtering (mirrors SemanticQueryTranslator)
    # ------------------------------------------------------------------

    def _calculate_spatial_overlap(
        self, bbox1: List[float], bbox2: List[float]
    ) -> float:
        """Return the fractional overlap of bbox2 within bbox1 (0-1)."""
        try:
            x_overlap = max(0, min(bbox1[2], bbox2[2]) - max(bbox1[0], bbox2[0]))
            y_overlap = max(0, min(bbox1[3], bbox2[3]) - max(bbox1[1], bbox2[1]))
            intersection = x_overlap * y_overlap
            area1 = (bbox1[2] - bbox1[0]) * (bbox1[3] - bbox1[1])
            if area1 <= 0:
                return 0.0
            return intersection / area1
        except Exception:
            return 0.0

    def _filter_stac_results_by_spatial_overlap(
        self,
        stac_results: Dict[str, Any],
        requested_bbox: Optional[List[float]],
        min_overlap: float = 0.1,
    ) -> Dict[str, Any]:
        """Keep only features with meaningful overlap with *requested_bbox*."""
        if not requested_bbox or not stac_results.get("features"):
            return stac_results

        kept = [
            f for f in stac_results["features"]
            if f.get("bbox") and self._calculate_spatial_overlap(requested_bbox, f["bbox"]) >= min_overlap
        ]
        logger.info(f"[TARGET] Spatial filter: {len(kept)}/{len(stac_results['features'])} features kept")
        return {**stac_results, "features": kept}

    def _filter_stac_results_by_cloud_cover(
        self,
        stac_results: Dict[str, Any],
        max_cloud_cover: Optional[int] = None,
        collection_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Keep only features whose cloud cover is ≤ *max_cloud_cover*."""
        if max_cloud_cover is None or not stac_results.get("features"):
            return stac_results

        cloud_props = ["eo:cloud_cover", "cloud_cover", "cloudCover", "CLOUD_COVER"]
        kept = []
        for feature in stac_results["features"]:
            props = feature.get("properties", {})
            cloud = next((props[p] for p in cloud_props if p in props), None)
            if cloud is None or cloud <= max_cloud_cover:
                kept.append(feature)

        logger.info(f"[CLOUD] Cloud filter ≤{max_cloud_cover}%: {len(kept)}/{len(stac_results['features'])} features kept")
        return {**stac_results, "features": kept}

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
