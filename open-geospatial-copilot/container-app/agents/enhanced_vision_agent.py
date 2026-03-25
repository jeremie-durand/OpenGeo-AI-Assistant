"""
Enhanced Vision Agent - Local-first LLM Client

Refactored to use a configurable local LLM client (OpenAI/Anthropic) for vision analysis and tool orchestration.
Removes Azure dependencies and Agent Service logic.

This agent:
1. Maintains conversation memory via VisionSession (persistent threads)
2. Has access to 13 vision analysis tools via vision_tools
3. Uses LLM-driven tool selection (replaces forced keyword routing)
4. Sets module-level session context for standalone tool functions
"""

import logging
import os
import re
import json
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ============================================================================
# COLLECTION -> RASTER DATA_TYPE MAPPING (all 72 PC collections)
# ============================================================================
# Maps collection ID patterns to the correct `sample_raster_value(data_type=...)`
# parameter. Used by the deterministic pre-sampling check to bypass LLM tool
# selection when the user asks a measurable-value question.
#
# Order matters: more specific patterns MUST come before generic ones.
# Each entry is (substring_to_match, data_type, human_label).
# ============================================================================

COLLECTION_RASTER_MAP: List[Tuple[str, str, str]] = [
    # -- SST / Ocean Temperature --
    ("noaa-cdr-sea-surface-temperature", "sst", "Sea Surface Temperature"),
    ("sst", "sst", "Sea Surface Temperature"),

    # -- Land Surface Temperature (MODIS 11, 21) --
    ("modis-11a1", "sst", "Land Surface Temperature"),
    ("modis-11a2", "sst", "Land Surface Temperature"),
    ("modis-21a2", "sst", "Land Surface Temperature"),

    # -- Elevation / DEM --
    ("cop-dem-glo-30", "elevation", "Elevation"),
    ("cop-dem-glo-90", "elevation", "Elevation"),
    ("alos-dem", "elevation", "Elevation"),
    ("nasadem", "elevation", "Elevation"),
    ("3dep-seamless", "elevation", "Elevation"),
    ("3dep-lidar-dsm", "elevation", "Digital Surface Model"),
    ("3dep-lidar-dtm", "elevation", "Digital Terrain Model"),
    ("3dep-lidar-dtm-native", "elevation", "Digital Terrain Model"),
    ("3dep-lidar-hag", "lidar", "Height Above Ground"),
    ("3dep-lidar-intensity", "lidar", "LiDAR Intensity"),
    ("3dep-lidar-classification", "lidar", "LiDAR Classification"),
    ("3dep-lidar-pointsourceid", "lidar", "LiDAR Point Source"),
    ("3dep-lidar-returns", "lidar", "LiDAR Returns"),

    # -- Fire / Burn --
    ("modis-14a1", "fire", "Active Fire"),
    ("modis-14a2", "fire", "Active Fire"),
    ("modis-64a1", "fire", "Burned Area"),
    ("mtbs", "fire", "Burn Severity"),

    # -- Snow / Ice --
    ("modis-10a1", "snow", "Snow Cover"),
    ("modis-10a2", "snow", "Snow Cover"),

    # -- Vegetation / NDVI / LAI / GPP / NPP / ET --
    ("modis-13a1", "vegetation", "NDVI (500m)"),
    ("modis-13q1", "vegetation", "NDVI (250m)"),
    ("modis-15a2h", "vegetation", "LAI/FPAR"),
    ("modis-15a3h", "vegetation", "LAI/FPAR (8-day)"),
    ("modis-16a3gf", "vegetation", "Evapotranspiration"),
    ("modis-17a2h", "vegetation", "GPP"),
    ("modis-17a2hgf", "vegetation", "GPP (gap-filled)"),
    ("modis-17a3hgf", "vegetation", "NPP"),

    # -- Optical / Multispectral (NDVI-capable) --
    ("sentinel-2", "ndvi", "Optical Imagery"),
    ("hls2-l30", "ndvi", "HLS Landsat"),
    ("hls2-s30", "ndvi", "HLS Sentinel"),
    ("landsat-c2-l2", "ndvi", "Landsat Collection 2 L2"),
    ("landsat-c2-l1", "ndvi", "Landsat Collection 2 L1"),
    ("naip", "ndvi", "NAIP Aerial"),
    ("aster", "ndvi", "ASTER Multispectral"),

    # -- Surface Reflectance / BRDF --
    ("modis-43a4", "reflectance", "BRDF/NBAR"),
    ("modis-09a1", "reflectance", "Surface Reflectance (8-day)"),
    ("modis-09q1", "reflectance", "Surface Reflectance (250m)"),

    # -- SAR / Radar --
    ("sentinel-1-grd", "sar", "SAR GRD"),
    ("sentinel-1-rtc", "sar", "SAR RTC"),
    ("alos-palsar", "sar", "ALOS PALSAR"),

    # -- Water --
    ("jrc-gsw", "water", "Water Occurrence"),

    # -- Biomass --
    ("chloris-biomass", "biomass", "Above-ground Biomass"),

    # -- Land Cover (classification — no numeric sampling, use domain tool) --
    ("esa-worldcover", "landcover", "Land Cover"),
    ("esa-cci-lc", "landcover", "Land Cover (ESA CCI)"),
    ("io-lulc-annual-v02", "landcover", "Land Use/Land Cover"),
    ("io-lulc-9-class", "landcover", "Land Use/Land Cover"),
    ("io-lulc", "landcover", "Land Use/Land Cover"),
    ("usda-cdl", "landcover", "Cropland Data Layer"),
    ("drcog-lulc", "landcover", "Land Use/Land Cover"),
    ("nrcan-landcover", "landcover", "Land Cover (Canada)"),
    ("noaa-c-cap", "landcover", "Coastal Land Cover"),
    ("chesapeake-lc-13", "landcover", "Land Cover (Chesapeake)"),
    ("chesapeake-lc-7", "landcover", "Land Cover (Chesapeake)"),
    ("chesapeake-lu", "landcover", "Land Use (Chesapeake)"),
    ("usgs-gap", "landcover", "GAP Land Cover"),
    ("usgs-lcmap-conus", "landcover", "Land Cover (LCMAP)"),
    ("usgs-lcmap-hawaii", "landcover", "Land Cover (Hawaii)"),
    ("alos-fnf-mosaic", "landcover", "Forest/Non-Forest"),

    # -- Climate Projections (NetCDF — not COG, limited raster sampling) --
    ("nasa-nex-gddp-cmip6", "climate", "Climate Projection (CMIP6)"),
    ("nex-gddp", "climate", "Climate Projection (NEX-GDDP)"),

    # -- Precipitation --
    ("noaa-mrms-qpe", "auto", "Precipitation"),

    # -- Climate Normals --
    ("noaa-climate-normals", "auto", "Climate Normals"),
    ("noaa-nclimgrid", "auto", "Climate Grid"),

    # -- Thematic (non-raster or specialized) --
    ("hrea", "auto", "Electricity Access"),
    ("hgb", "auto", "Gap Habitat"),
    ("mobi", "auto", "Biodiversity Importance"),
    ("io-biodiversity", "auto", "Biodiversity"),
    ("ms-buildings", "auto", "Building Footprints"),
]

# ============================================================================
# VALUE-QUESTION KEYWORDS  (used for TONE CONTROL, not gating)
# ============================================================================
# When these patterns match, the pre-sampled raster value is injected with
# forceful wording ("use this data, do NOT guess from colors").  When they
# don't match, the value is still injected but with softer wording so the
# agent can decide whether to surface it.  Pre-sampling itself is ALWAYS
# triggered when raster-capable data is loaded — no regex gate.
# ============================================================================

VALUE_QUESTION_PATTERNS = [
    # Direct value requests — "what is the X"
    r"what is the (?:sea surface |surface |land surface )?temperature",
    r"what(?:'s| is) the (?:elevation|altitude|height)",
    r"what(?:'s| is) the (?:ndvi|evi|vegetation index|greenness)",
    r"what(?:'s| is) the (?:value|reading|measurement)",
    r"what(?:'s| is) the (?:snow|ice) (?:cover|extent)",
    r"what(?:'s| is) the (?:fire|thermal|burn|frp|maxfrp)",
    r"what(?:'s| is) the (?:water|flood|inundation)",
    r"what(?:'s| is) the (?:biomass|carbon)",
    r"what(?:'s| is) the (?:backscatter|radar|sar)",
    r"what(?:'s| is) the (?:reflectance|albedo)",
    r"what(?:'s| is) the (?:precipitation|rainfall|rain)",
    r"what(?:'s| is) the (?:projected|climate|cmip6)",
    r"(?:projected|climate projection|cmip6) (?:temperature|precipitation|wind|humidity)",
    r"what(?:'s| is) the (?:land cover|land use)",
    r"what(?:'s| is) the (?:slope|aspect|steepness)",
    r"what(?:'s| is) the (?:evapotranspiration|et\b)",
    r"what(?:'s| is) the (?:lai|leaf area|fpar)",
    r"what(?:'s| is) the (?:gpp|npp|productivity)",
    # Plural form — "what are the X values"
    r"what are the (?:ndvi|evi|temperature|elevation|fire|snow|water|biomass|sar|backscatter|reflectance|lai|gpp|npp|frp|maxfrp|vv|vh|hh|hv|polarization|raster|projected|climate)",
    # Temperature/elevation/NDVI/FRP at location
    r"(?:temperature|elevation|ndvi|evi|sst|lst|frp|maxfrp|fire radiative) (?:at|of|for|near|in|here)",
    r"(?:at|of|for|near|in) (?:this|that|the|my) (?:location|point|spot|pin|coordinate|field|area|site)",
    # How hot/cold/high/deep
    r"how (?:hot|cold|warm|cool) is",
    r"how (?:high|tall|deep|low) is",
    # Measure/sample/extract/read/get — broad verb patterns
    r"(?:measure|sample|extract|read|get|tell me|give me|show me|report) .*(?:value|temperature|elevation|ndvi|evi|data|frp|maxfrp|fire radiative|raster|pixel|measurement|reading|reflectance|brdf|band)",
    r"sample (?:the )?(?:fire|frp|maxfrp|ndvi|evi|temperature|elevation|raster|pixel|value|reflectance|brdf|band)",
    # "in celsius/fahrenheit/meters/feet"
    r"in (?:celsius|fahrenheit|kelvin|meters|feet|degrees)",
    # Numeric expectation
    r"(?:exact|actual|precise|numeric|quantitative) (?:value|temperature|elevation|measurement|reading)",
    # Domain-specific asks that imply numeric sampling
    r"fire radiative power",
    r"radiative power",
    r"maxfrp",
    r"pixel value",
    r"raster value",
    r"data value",
    r"(?:ndvi|evi|ndwi|lai|fpar|gpp|npp) (?:value|index|at|for|here)",
    r"(?:tasmax|tasmin|\btas\b|sfcwind|\bpr\b|hurs|huss|rlds|rsds)",
    r"(?:projected|projection) .*(?:value|temperature|precipitation|wind|humidity|radiation)",
]

_VALUE_QUESTION_RE = re.compile("|".join(VALUE_QUESTION_PATTERNS), re.IGNORECASE)


def _detect_raster_data_type(collections: List[str]) -> Optional[Tuple[str, str]]:
    """
    Given loaded collection IDs, return the best (data_type, human_label) for
    sample_raster_value, or None if no mapping exists.
    """
    if not collections:
        return None
    for coll in collections:
        coll_lower = coll.lower()
        for pattern, data_type, label in COLLECTION_RASTER_MAP:
            if pattern in coll_lower:
                return (data_type, label)
    return None


def _is_value_question(query: str) -> bool:
    """Return True if the user query is asking for a measurable numeric value."""
    return bool(_VALUE_QUESTION_RE.search(query))


# ============================================================================
# VISION AGENT SYSTEM PROMPT
# ============================================================================

VISION_AGENT_INSTRUCTIONS = """You are a Geospatial Intelligence (GEOINT) Vision Analysis Agent specializing in satellite imagery analysis, environmental monitoring, and quantitative data extraction from Earth observation data.

## Available Tools (13 total):

### Visual Analysis
1. **analyze_screenshot(question)** - Analyze the map screenshot with GPT-5 Vision. Use for: visual features, patterns, colors, land cover identification, "what do you see", general map questions.
2. **identify_features(feature_type)** - Identify specific geographic features visible on the map. Use for: "what is that", rivers, mountains, cities, landmarks, roads.

### Quantitative Raster Analysis
3. **analyze_raster(metric_type)** - Get statistics from loaded raster data (elevation, NDVI, SST). Use for: quantitative analysis, statistics, overall metrics.
4. **sample_raster_value(data_type)** - Extract the ACTUAL pixel value at the pin/center location. Use for: "what is the value", "temperature at", "elevation at", point-specific measurements. **THIS IS THE MOST IMPORTANT TOOL FOR NUMERIC DATA.**

### Domain-Specific Analysis
5. **analyze_vegetation(analysis_type)** - MODIS vegetation products + optical NDVI. Use when: MODIS-13, HLS, Sentinel-2, Landsat data is loaded AND question is about vegetation/NDVI/EVI/LAI.
6. **analyze_fire(analysis_type)** - Fire detection and burn severity. Use when: MODIS-14, MTBS data is loaded AND question is about fires/burns.
7. **analyze_land_cover(analysis_type)** - Land cover classification. Use when: ESA WorldCover, CDL, IO-LULC data is loaded.
8. **analyze_snow(analysis_type)** - Snow/ice analysis. Use when: MODIS-10 data is loaded AND question is about snow/ice.
9. **analyze_sar(analysis_type)** - Radar/SAR analysis. Use when: Sentinel-1, ALOS PALSAR data is loaded.
10. **analyze_water(analysis_type)** - Water occurrence and flood detection. Use when: JRC-GSW, Sentinel-1 data is loaded.
11. **analyze_biomass(analysis_type)** - Above-ground biomass. Use when: CHLORIS data is loaded.

### Knowledge & Temporal
12. **query_knowledge(question)** - Answer educational/factual questions using LLM knowledge. Use for: "why", "explain", "how does", general knowledge questions.
13. **compare_temporal(location, time_period_1, time_period_2, analysis_focus)** - Compare satellite data between two time periods. Use for: change detection, before/after comparisons.

## CRITICAL TOOL SELECTION RULES:

### Data-to-Tool Matching (HIGHEST PRIORITY):
- **SST / sea surface temperature / ocean temperature data loaded** -> ALWAYS use `sample_raster_value(data_type='sst')`
- **DEM / elevation / cop-dem data loaded** -> Use `sample_raster_value(data_type='elevation')` for point values, `analyze_raster(metric_type='elevation')` for statistics
- **Sentinel-2 / HLS / Landsat loaded + NDVI question** -> Use `sample_raster_value(data_type='ndvi')` for point, `analyze_vegetation` for area
- **MODIS-13 loaded** -> Use `analyze_vegetation` or `sample_raster_value(data_type='vegetation')`
- **MODIS-14 / MTBS loaded** -> Use `analyze_fire` or `sample_raster_value(data_type='fire')`
- **JRC-GSW loaded** -> Use `analyze_water` or `sample_raster_value(data_type='water')`
- **MODIS-10 loaded** -> Use `analyze_snow` or `sample_raster_value(data_type='snow')`
- **Sentinel-1 / SAR loaded** -> Use `analyze_sar` or `sample_raster_value(data_type='sar')`

### When to use sample_raster_value:
- User asks for a specific VALUE at a location -> sample_raster_value
- User asks "what is the temperature/elevation/NDVI here" -> sample_raster_value
- Any loaded STAC data + question about its value -> sample_raster_value
- **DEFAULT when any raster data is loaded and the user asks about it**

### When to use analyze_screenshot:
- No specific data loaded but screenshot available
- User asks about visual appearance, colors, patterns
- General "what does this area look like" questions

### When to use query_knowledge:
- No data loaded AND question is educational/factual
- "Why is the ocean warm here?"
- Historical or scientific context questions

## Guidelines:
1. ALWAYS prefer tools that match the loaded data type
2. When STAC data is loaded, ALWAYS try sample_raster_value or the domain-specific tool
3. Include actual numeric values with units in your response
4. Interpret values (e.g., NDVI 0.7 = dense vegetation, SST 28°C = warm tropical water)
5. Be concise but informative — focus on answering the user's specific question
6. If a tool fails, explain what happened and suggest alternatives
7. **Summary**: ALWAYS conclude with a **Summary** section that gives a clear, direct answer to the user's specific question, grounded in the data returned by your tools. For example:
   - If asked "what is the temperature here?", end with the exact value and units from sample_raster_value (e.g., "The sea surface temperature at this location is 27.3°C")
   - If asked "what crop is growing here?", end with the identified land cover class from tool output
   - If asked about vegetation health, end with a clear healthy/stressed/sparse assessment citing the NDVI value
   - Never end with generic descriptions — always tie your conclusion to actual tool data
"""


# ============================================================================
# SESSION DATACLASS
# ============================================================================

@dataclass
class VisionSession:
    """Represents a conversation session with the vision agent."""
    session_id: str
    thread_id: Optional[str] = None  # Agent Service thread ID
    screenshot_base64: Optional[str] = None
    map_bounds: Optional[Dict[str, float]] = None
    loaded_collections: List[str] = field(default_factory=list)
    tile_urls: List[str] = field(default_factory=list)
    stac_items: List[Dict[str, Any]] = field(default_factory=list)
    last_analysis: Optional[str] = None
    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def add_turn(self, role: str, content: str):
        """Add a conversation turn and trim to last 10."""
        self.conversation_history.append({"role": role, "content": content})
        self.updated_at = datetime.utcnow()
        if len(self.conversation_history) > 10:
            self.conversation_history = self.conversation_history[-10:]


# ============================================================================
# ENHANCED VISION AGENT (Agent Service)
# ============================================================================

class EnhancedVisionAgent:
    """
    Local-first vision analysis agent using a configurable LLM client (OpenAI/Anthropic).

    Replaces Azure Agent Service with:
    - Custom LLM client for agent creation and management
    - vision_tools for 13 standalone tool functions
    - Session threads for persistent conversation
    """

    def __init__(self):
        """Initialize the vision agent (lazy — actual setup on first use)."""
        self.sessions: Dict[str, VisionSession] = {}
        self.memory_ttl = timedelta(minutes=30)
        self._llm = None
        self._initialized = False
        logger.info("EnhancedVisionAgent created (will initialize on first use)")

    def _ensure_initialized(self):
        """Lazy initialization of LLM client."""
        if self._initialized:
            return
        from llm_client import get_llm_client
        compat = get_llm_client()
        self._llm = compat._llm  # raw LLMClient (has .provider, .model, .chat())
        self._initialized = True
        logger.info(f"EnhancedVisionAgent initialised (provider={self._llm.provider}, model={self._llm.model})")

    def _get_or_create_session(self, session_id: str) -> VisionSession:
        """Get existing session or create a new one."""
        if session_id in self.sessions:
            return self.sessions[session_id]
        session = VisionSession(session_id=session_id)
        self.sessions[session_id] = session
        logger.info(f"Created vision session: {session_id}")
        return session

    def update_session(self, session_id: str, **kwargs):
        """Update session context (screenshot, STAC items, map bounds, etc.)."""
        session = self.sessions.get(session_id)
        if session:
            for key, value in kwargs.items():
                if hasattr(session, key) and value is not None:
                    setattr(session, key, value)
            session.updated_at = datetime.utcnow()

    def get_or_create_session(self, session_id: str) -> VisionSession:
        if session_id not in self.sessions:
            self.sessions[session_id] = VisionSession(session_id=session_id)
        return self.sessions[session_id]

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    async def _llm_text(self, messages: List[Dict], max_tokens: int = 1200, temperature: float = 0.4, **kwargs) -> str:
        response = await self._llm.chat(messages, max_tokens=max_tokens, temperature=temperature, **kwargs)
        if isinstance(response, dict):
            blocks = response.get("content", [])
            if blocks and isinstance(blocks, list):
                item = blocks[0]
                return item.get("text", "") if isinstance(item, dict) else str(item)
            choices = response.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
        return str(response)

    async def _call_llm(
        self,
        user_query: str,
        session: VisionSession,
        screenshot_base64: Optional[str],
        raster_result: Optional[str],
    ) -> str:
        """
        Single LLM call with screenshot + context + question.
        Including the image directly ensures the model actually sees the map.
        """
        bounds = session.map_bounds or {}
        lat = bounds.get("center_lat") or bounds.get("pin_lat")
        lng = bounds.get("center_lng") or bounds.get("pin_lng")

        context_lines = []
        if lat is not None and lng is not None:
            context_lines.append(f"Map center: ({lat:.4f}, {lng:.4f})")
        if session.loaded_collections:
            context_lines.append(f"Data layers loaded: {', '.join(session.loaded_collections)}")
        if raster_result:
            context_lines.append(f"\n[Raster/Point Data]\n{raster_result}")
        context_str = "\n".join(context_lines) if context_lines else ""

        system_prompt = (
            "You are a Geospatial Intelligence (GEOINT) Vision Analysis Agent.\n"
            "Analyze the satellite/map image provided and answer the user's question.\n\n"
            "Guidelines:\n"
            "- Look at the image carefully to identify cities, water bodies, terrain, vegetation, infrastructure\n"
            "- Use raster data when available for quantitative answers\n"
            "- Be specific — name visible cities, rivers, landmarks\n"
            "- End with a concise **Summary** answering the user's question directly\n"
        )
        if context_str:
            system_prompt += f"\nContext:\n{context_str}"

        # Build conversation history prefix (last 6 turns)
        history = list(session.conversation_history[-6:])

        if screenshot_base64:
            clean = screenshot_base64
            if screenshot_base64.startswith("data:image"):
                clean = screenshot_base64.split(",", 1)[1]

            if self._llm.provider == "anthropic":
                # Anthropic: system as kwarg, image in user content
                user_msg = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_query},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/jpeg", "data": clean
                        }},
                    ],
                }
                messages = history + [user_msg]
                return await self._llm_text(messages, max_tokens=1200, temperature=0.3)
            else:
                # OpenAI-compatible: system message + image_url in user content
                sys_msg = {"role": "system", "content": system_prompt}
                user_msg = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_query},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{clean}", "detail": "high"
                        }},
                    ],
                }
                messages = [sys_msg] + history + [user_msg]
                return await self._llm_text(messages, max_tokens=1200, temperature=0.3)
        else:
            # No screenshot — text only
            if self._llm.provider == "anthropic":
                user_msg = {"role": "user", "content": user_query}
                messages = history + [user_msg]
                return await self._llm_text(
                    messages, max_tokens=1200, temperature=0.4,
                    system=system_prompt,
                )
            else:
                sys_msg = {"role": "system", "content": system_prompt}
                user_msg = {"role": "user", "content": user_query}
                messages = [sys_msg] + history + [user_msg]
                return await self._llm_text(messages, max_tokens=1200, temperature=0.4)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def analyze(
        self,
        user_query: str,
        session_id: str = "default",
        imagery_base64: Optional[str] = None,
        map_bounds: Optional[Dict[str, float]] = None,
        collections: Optional[List[str]] = None,
        tile_urls: Optional[List[str]] = None,
        stac_items: Optional[List[Dict[str, Any]]] = None,
        conversation_history: Optional[List[Dict]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Analyze a user query using vision + raster tools."""
        try:
            self._ensure_initialized()
            session = self._get_or_create_session(session_id)

            # Update session context
            if imagery_base64:
                session.screenshot_base64 = imagery_base64
            if map_bounds:
                session.map_bounds = map_bounds
            if collections:
                session.loaded_collections = collections
            if tile_urls:
                session.tile_urls = tile_urls
            if stac_items:
                session.stac_items = stac_items

            # Set module-level context for sync raster tools
            from agents.vision_tools import (
                set_session_context, clear_tool_calls, get_tool_calls,
                sample_raster_value, analyze_raster,
            )
            set_session_context(
                screenshot_base64=session.screenshot_base64,
                map_bounds=session.map_bounds,
                stac_items=session.stac_items,
                loaded_collections=session.loaded_collections,
                tile_urls=session.tile_urls,
            )
            clear_tool_calls()

            import asyncio

            # === Step 1: Raster point sampling (when STAC data is loaded) ===
            raster_result: Optional[str] = None
            if session.stac_items or session.tile_urls:
                data_type = "auto"
                if session.loaded_collections:
                    det = _detect_raster_data_type(session.loaded_collections)
                    if det:
                        data_type = det[0]
                try:
                    loop = asyncio.get_event_loop()
                    raster_result = await loop.run_in_executor(None, sample_raster_value, data_type)
                    if raster_result and len(raster_result) < 30:
                        raster_result = None  # too short = likely "no data" message
                except Exception as e:
                    logger.warning(f"sample_raster_value failed: {e}")

            # === Step 2: Single LLM call with screenshot + context + question ===
            response_text = await self._call_llm(
                user_query=user_query,
                session=session,
                screenshot_base64=session.screenshot_base64,
                raster_result=raster_result,
            )

            session.last_analysis = response_text
            session.add_turn("user", user_query)
            session.add_turn("assistant", response_text)

            return {
                "response": response_text,
                "analysis": response_text,
                "tools_used": get_tool_calls(),
                "confidence": 0.9,
                "session_id": session_id,
            }

        except Exception as e:
            logger.error(f"[FAIL] EnhancedVisionAgent.analyze error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {
                "response": "Vision analysis failed due to an internal error.",
                "analysis": "",
                "tools_used": [],
                "error": str(e),
                "confidence": 0.0,
                "session_id": session_id,
            }

    # ====================================================================
    # FALLBACK: Direct Azure OpenAI Vision API (when Agent Service fails)
    # ====================================================================

    async def _fallback_direct_openai(
        self,
        user_query: str,
        session: VisionSession,
        session_id: str,
        pre_sampled_value: Optional[str] = None,
        pre_sample_failure: Optional[str] = None,
        is_value_q: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Fallback to direct Azure OpenAI Chat Completions API with Vision.

        Called when the Agent Service is unavailable (404, network disabled, etc.).
        Sends the screenshot + user query directly to GPT-5 Vision without tools.
        If pre-sampled raster data is available, it is injected into the prompt.
        """
        import aiohttp

        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
        # Fallback: try AZURE_AI_PROJECT_ENDPOINT if AZURE_OPENAI_ENDPOINT is not set
        # Azure AI Foundry project endpoints support the OpenAI chat completions API
        if not endpoint:
            endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "")
            if endpoint:
                logger.info("[SYNC] Using AZURE_AI_PROJECT_ENDPOINT for fallback (AZURE_OPENAI_ENDPOINT not set)")
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5")
        api_key = os.getenv("AZURE_OPENAI_API_KEY", "")
        use_managed_identity = os.getenv("AZURE_OPENAI_USE_MANAGED_IDENTITY", "").lower() == "true"

        if not endpoint:
            logger.error("No AZURE_OPENAI_ENDPOINT for fallback")
            return None

        # Build auth headers
        headers = {"Content-Type": "application/json"}
        headers["api-key"] = api_key

        # Build context
        context_parts = []
        if session.loaded_collections:
            context_parts.append(f"Loaded satellite data: {', '.join(session.loaded_collections)}")
        if session.map_bounds:
            b = session.map_bounds
            pin_lat = b.get("pin_lat") or b.get("center_lat")
            pin_lng = b.get("pin_lng") or b.get("center_lng")
            if pin_lat and pin_lng:
                context_parts.append(f"Map center: ({pin_lat:.4f}, {pin_lng:.4f})")
                context_parts.append(
                    f"Bounds: W={b.get('west', 'N/A')}, S={b.get('south', 'N/A')}, "
                    f"E={b.get('east', 'N/A')}, N={b.get('north', 'N/A')}"
                )

        # Inject pre-sampled raster data into fallback context
        if pre_sampled_value:
            if is_value_q:
                context_parts.append(
                    f"\n[RASTER DATA — ACTUAL SAMPLED VALUE]\n{pre_sampled_value}\n"
                    "The above is a REAL measurement extracted from the underlying Cloud Optimized GeoTIFF. "
                    "Use this data to answer the user's question. Do NOT guess from colors. "
                    "Interpret and explain the value in context (units, what it means for this location)."
                )
            else:
                context_parts.append(
                    f"\n[RASTER DATA — SAMPLED AT PIN]\n{pre_sampled_value}\n"
                    "The above measurement was automatically read from the COG at the pin location. "
                    "Use this real data to answer instead of guessing from colors."
                )
        elif pre_sample_failure:
            context_parts.append(
                f"\n[RASTER SAMPLING ISSUE]\n{pre_sample_failure}\n"
                "Explain the issue to the user succinctly. "
                "Do NOT suggest using external GIS software or Python — this platform can sample data directly."
            )

        context_str = "\n".join(context_parts) if context_parts else "No additional context."

        if pre_sampled_value and is_value_q:
            system_prompt = (
                "You are a geospatial intelligence analyst. Real raster data has been sampled from the "
                "underlying Cloud Optimized GeoTIFF and is provided in the context. Use ONLY this real "
                "data to answer the user's question — do NOT make up values or give generic instructions. "
                "Interpret the values with proper units and explain what they mean for this location. "
                "Be concise but informative."
            )
        else:
            system_prompt = (
                "You are a geospatial intelligence analyst. Analyze the satellite/map imagery and "
                "answer the user's question. If a map screenshot is provided, describe what you see "
                "including terrain, land cover, water bodies, urban areas, and notable features. "
                "Be concise but informative."
            )

        user_content: list = [
            {"type": "text", "text": f"{context_str}\n\nUser question: {user_query}"}
        ]

        # Attach screenshot if available
        if session.screenshot_base64:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{session.screenshot_base64}",
                    "detail": "high",
                },
            })

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_completion_tokens": 1500,
        }

        # Determine the correct URL (handle both Azure OpenAI and AI Foundry endpoints)
        url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version=2024-12-01-preview"

        logger.info(f"[SYNC] Fallback: calling {deployment} directly at {endpoint}")

        try:
            async with aiohttp.ClientSession() as http_session:
                async with http_session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"Fallback API error {resp.status}: {error_text[:300]}")
                        return None

                    result = await resp.json()
                    analysis_text = result["choices"][0]["message"]["content"]

                    logger.info(f"[OK] Fallback vision analysis succeeded ({len(analysis_text)} chars)")

                    # Update session
                    session.last_analysis = analysis_text
                    session.add_turn("user", user_query)
                    session.add_turn("assistant", analysis_text)

                    return {
                        "response": analysis_text,
                        "analysis": analysis_text,
                        "tools_used": ["direct_openai_vision_fallback"],
                        "tool_calls": [],
                        "confidence": 0.8,
                        "session_id": session_id,
                        "agent_mode": "direct_openai_fallback",
                        "context": {
                            "has_screenshot": bool(session.screenshot_base64),
                            "collections": session.loaded_collections,
                            "map_bounds": session.map_bounds,
                        },
                    }
        except Exception as e:
            logger.error(f"[FAIL] Fallback direct OpenAI call failed: {e}")
            return None

    def cleanup_old_sessions(self, max_age_minutes: int = 30):
        """Remove sessions older than max_age_minutes."""
        now = datetime.utcnow()
        expired = [
            sid for sid, session in self.sessions.items()
            if (now - session.updated_at).total_seconds() > max_age_minutes * 60
        ]
        for sid in expired:
            del self.sessions[sid]
            logger.info(f"Cleaned up expired vision session: {sid}")

    async def cleanup(self):
        """Cleanup agent resources on shutdown."""
        if self._agents_client and self._agent_id:
            try:
                await self._agents_client.delete_agent(self._agent_id)
                logger.info(f"Deleted vision agent: {self._agent_id}")
            except Exception as e:
                logger.debug(f"Agent cleanup: {e}")


# ============================================================================
# SINGLETON AND ALIASES
# ============================================================================

_enhanced_vision_agent: Optional[EnhancedVisionAgent] = None


def get_enhanced_vision_agent() -> EnhancedVisionAgent:
    """Get the singleton EnhancedVisionAgent instance."""
    global _enhanced_vision_agent
    if _enhanced_vision_agent is None:
        _enhanced_vision_agent = EnhancedVisionAgent()
    return _enhanced_vision_agent


def get_vision_agent() -> EnhancedVisionAgent:
    """Alias for get_enhanced_vision_agent (backwards compatibility)."""
    return get_enhanced_vision_agent()


# Backwards compatibility alias
VisionAgent = EnhancedVisionAgent
