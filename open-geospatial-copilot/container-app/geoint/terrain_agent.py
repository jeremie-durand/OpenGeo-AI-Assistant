"""
GEOINT Terrain Agent — Provider-agnostic rewrite

Uses LLM_PROVIDER / LLM_API_KEY / LLM_MODEL (same as the rest of the app).
No Azure AI Agent Service dependency.

Flow per query:
  1. LLM selects which terrain tools to call
  2. Tools are called directly (sync, in executor)
  3. Optional screenshot vision analysis
  4. LLM synthesises a natural-language response from all tool results
  5. Conversation history is kept in-process per session
"""

import logging
import json
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)

TERRAIN_AGENT_INSTRUCTIONS = """You are a Geospatial Intelligence (GEOINT) Terrain Analysis Agent specializing in site permitting and environmental suitability analysis.

Your role is to analyze terrain and answer questions about geographic locations using DEM (Digital Elevation Model) data, water occurrence data, land cover data, and visual analysis from the user's current map view.

## Available Tools:

### Terrain Analysis (DEM-based):
- **get_elevation_analysis**: Get elevation data (min, max, mean in meters) and terrain classification (flat/hilly/mountainous)
- **get_slope_analysis**: Analyze terrain steepness, traversability, and percentage of flat/moderate/steep areas
- **get_aspect_analysis**: Determine slope direction (N, S, E, W, etc.) and sun exposure
- **find_flat_areas**: Locate flat areas suitable for landing zones, construction, or camps

### Environmental & Permitting Analysis:
- **analyze_flood_risk**: Check historical flood occurrence (0-100%) using JRC Global Surface Water. Returns flood risk level (LOW/MODERATE/HIGH) and permitting recommendation.
- **analyze_water_proximity**: Calculate distance to nearest water body for setback requirements (e.g., 500m from wetlands). Returns whether setback is satisfied.
- **analyze_environmental_sensitivity**: Identify wetlands, forests, mangroves, and protected habitats using ESA WorldCover. Returns environmental constraints and permitting status.

## Permitting Use Case Workflows:

**Mining Site Permit**: Call get_slope_analysis -> analyze_flood_risk -> analyze_water_proximity -> analyze_environmental_sensitivity
**Nuclear Facility Siting**: Call get_elevation_analysis -> analyze_flood_risk -> get_slope_analysis
**Construction Permit**: Call get_slope_analysis -> find_flat_areas -> analyze_flood_risk
**Solar/Wind Farm**: Call get_aspect_analysis -> get_slope_analysis -> find_flat_areas

## Visual Context
If a [Visual Analysis of Current Map View] section is provided, use it to enrich your response:
- Reference visible terrain features, land use patterns, and infrastructure
- Relate elevation/slope data to what is visible (e.g., visible cliff faces, flat farmland, forested hills)
- Combine visual observations with quantitative tool data for richer answers

## CRITICAL: Tool Parameters
Each message includes [Location Context] with:
- Coordinates: (latitude, longitude) - USE THESE VALUES when calling any terrain tool
- Analysis radius: X km - USE THIS as the radius_km parameter

**ALWAYS extract the latitude, longitude, and radius from the context and pass them to tools.**

## Guidelines:
1. **For permitting questions** - Call ALL relevant tools (slope, flood, water proximity, environmental)
2. **Always call DEM tools** for elevation, slope, and aspect - these provide accurate quantitative data
3. **Be specific** - Include actual numbers (elevations in meters, slope percentages, etc.)
4. **Summarize permitting status** - End with clear SUITABLE/CONDITIONAL/NOT SUITABLE recommendation

## Response Format:
1. **Terrain Overview**: ALWAYS start with the location name followed by terrain character summary
2. **Relevant Data Sections**: Include only the sections relevant to the user's question:
   - Elevation & Topography: min/max/mean elevation, terrain type from tools
   - Slope & Traversability: Steepness data, percentage flat/steep, traversability
   - Aspect & Sun Exposure: Direction distribution, sun exposure rating WITH note
   - Environmental Assessment (for permitting): Flood risk, water proximity, wetlands/forests
3. **Summary**: ALWAYS conclude with a **Summary** section that gives a clear, direct answer to the user's specific question, grounded in the data returned by tools.

## CRITICAL RULES:
- **Always use the Location name from [Location Context].** Never respond with just coordinates.
- **Answer the question asked.** Do not provide unrelated sections.
- **Use the sun_exposure_note** from get_aspect_analysis results — it accounts for flat terrain correctly.
- **Never contradict tool data.** If the tool says sun exposure is "good" due to flat terrain, do NOT downgrade it.

**Keep responses factual and concise.**
"""

# ---------------------------------------------------------------------------
# Tool catalogue (name → callable)
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: Dict[str, Any] = {}


def _get_tool_registry() -> Dict[str, Any]:
    global _TOOL_REGISTRY
    if not _TOOL_REGISTRY:
        from geoint.terrain_tools import (
            get_elevation_analysis,
            get_slope_analysis,
            get_aspect_analysis,
            find_flat_areas,
            analyze_flood_risk,
            analyze_water_proximity,
            analyze_environmental_sensitivity,
        )
        _TOOL_REGISTRY = {
            "get_elevation_analysis": get_elevation_analysis,
            "get_slope_analysis": get_slope_analysis,
            "get_aspect_analysis": get_aspect_analysis,
            "find_flat_areas": find_flat_areas,
            "analyze_flood_risk": analyze_flood_risk,
            "analyze_water_proximity": analyze_water_proximity,
            "analyze_environmental_sensitivity": analyze_environmental_sensitivity,
        }
    return _TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_safe(text: str) -> Any:
    text = text.strip()
    for marker in ("```json", "```"):
        if marker in text:
            text = text.split(marker, 1)[1].split("```")[0].strip()
            break
    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
        m2 = re.search(r"\[.*\]", text, re.DOTALL)
        if m2:
            try:
                return json.loads(m2.group())
            except Exception:
                pass
    return None


async def _llm_text(llm, messages, max_tokens=512, temperature=0.2):
    response = await llm.chat(messages, max_tokens=max_tokens, temperature=temperature)
    if isinstance(response, dict):
        blocks = response.get("content", [])
        if blocks and isinstance(blocks, list):
            item = blocks[0]
            return item.get("text", "") if isinstance(item, dict) else str(item)
        choices = response.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
    return str(response)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class TerrainAgentSession:
    def __init__(self, session_id: str, latitude: float, longitude: float):
        self.session_id = session_id
        self.latitude = latitude
        self.longitude = longitude
        self.created_at = datetime.utcnow()
        self.last_activity = datetime.utcnow()
        self.message_count = 0
        # Conversation history as OpenAI-style messages
        self.history: List[Dict[str, Any]] = []

    def update_location(self, latitude: float, longitude: float):
        self.latitude = latitude
        self.longitude = longitude
        self.last_activity = datetime.utcnow()


# ---------------------------------------------------------------------------
# TerrainAgent
# ---------------------------------------------------------------------------

class TerrainAgent:
    """Provider-agnostic terrain analysis agent with persistent conversation."""

    def __init__(self):
        self._llm = None
        self._initialized = False
        self.sessions: Dict[str, TerrainAgentSession] = {}
        logger.info("TerrainAgent created (provider-agnostic, lazy init)")

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def _ensure_initialized(self):
        if self._initialized:
            return
        import asyncio
        for attempt in range(3):
            try:
                await self._do_initialize()
                return
            except Exception as e:
                if attempt < 2:
                    wait = 2 ** attempt
                    logger.warning(f"TerrainAgent init attempt {attempt + 1} failed: {e} — retrying in {wait}s")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"TerrainAgent init failed after 3 attempts: {e}")
                    raise

    async def _do_initialize(self):
        from llm_client import get_llm_client
        compat = get_llm_client()
        self._llm = compat._llm
        self._initialized = True
        logger.info(f"TerrainAgent initialised (provider={self._llm.provider}, model={self._llm.model})")

    # ------------------------------------------------------------------
    # Step 1 — select which tools to call
    # ------------------------------------------------------------------

    async def _select_tools(self, user_query: str) -> List[str]:
        all_tools = list(_get_tool_registry().keys())

        prompt = f"""Given this terrain analysis question, select the relevant tools to call.

USER QUESTION: "{user_query}"

Available tools:
- get_elevation_analysis: elevation, altitude, height, topography
- get_slope_analysis: slope, steepness, gradient, traversability
- get_aspect_analysis: aspect, direction, sun exposure, solar, orientation
- find_flat_areas: flat land, landing zones, construction, buildable
- analyze_flood_risk: flood risk, water occurrence, flooding history
- analyze_water_proximity: water setback, distance to water, wetland buffer
- analyze_environmental_sensitivity: environmental, wetlands, forest, habitat, permitting

Rules:
- For permitting questions: include flood, water proximity, and environmental sensitivity
- For solar/wind farm: include aspect and slope
- For general terrain overview: include elevation and slope
- Return ONLY a JSON array of tool names from the list above

Return ONLY valid JSON array, e.g.: ["get_elevation_analysis", "get_slope_analysis"]"""

        try:
            raw = await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=128,
                temperature=0.0,
            )
            parsed = _parse_json_safe(raw)
            if isinstance(parsed, list):
                valid = [t for t in parsed if t in all_tools]
                if valid:
                    return valid
        except Exception as e:
            logger.error(f"[TerrainAgent] Tool selection failed: {e}")

        # Fallback: elevation + slope
        return ["get_elevation_analysis", "get_slope_analysis"]

    # ------------------------------------------------------------------
    # Step 2 — call tools directly (sync in executor)
    # ------------------------------------------------------------------

    @staticmethod
    def _call_tool(tool_name: str, latitude: float, longitude: float, radius_km: float) -> Dict[str, Any]:
        registry = _get_tool_registry()
        fn = registry.get(tool_name)
        if not fn:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            raw = fn(latitude=latitude, longitude=longitude, radius_km=radius_km)
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            return parsed if isinstance(parsed, dict) else {"result": raw}
        except Exception as e:
            logger.error(f"[TerrainAgent] Tool {tool_name} failed: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Step 3 — screenshot vision analysis (optional)
    # ------------------------------------------------------------------

    async def _analyze_screenshot(
        self,
        screenshot_base64: str,
        latitude: float,
        longitude: float,
    ) -> Optional[str]:
        try:
            clean = screenshot_base64
            if screenshot_base64.startswith("data:image"):
                clean = screenshot_base64.split(",", 1)[1]

            vision_prompt = (
                f"Analyze this satellite/map image for terrain and geospatial intelligence.\n"
                f"Location: approximately ({latitude:.4f}, {longitude:.4f})\n\n"
                "Provide a brief analysis covering:\n"
                "1. Land use & urban development\n"
                "2. Vegetation & land cover\n"
                "3. Water features\n"
                "4. Terrain features (hills, valleys, flat areas)\n"
                "Be specific and concise."
            )

            if self._llm.provider == "anthropic":
                messages = [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": clean}},
                    ],
                }]
            else:
                messages = [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{clean}", "detail": "high"}},
                    ],
                }]

            result = await _llm_text(self._llm, messages, max_tokens=600, temperature=0.3)
            logger.info(f"Terrain vision analysis: {len(result)} chars")
            return result
        except Exception as e:
            logger.warning(f"Terrain vision analysis failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Step 4 — synthesise the final response
    # ------------------------------------------------------------------

    async def _synthesise_response(
        self,
        session: TerrainAgentSession,
        context_message: str,
        tool_results: Dict[str, Any],
    ) -> str:
        tool_summary = json.dumps(tool_results, indent=2)[:3000]

        system_prompt = TERRAIN_AGENT_INSTRUCTIONS

        # Build message list: system + history + current context+tools
        messages = []
        if self._llm.provider != "anthropic":
            messages.append({"role": "system", "content": system_prompt})

        # Include recent conversation history (last 10 turns)
        messages.extend(session.history[-10:])

        # Current turn: context + tool results
        user_content = f"{context_message}\n\n[Tool Results]\n{tool_summary}"
        messages.append({"role": "user", "content": user_content})

        try:
            kwargs: Dict[str, Any] = {"max_tokens": 1200, "temperature": 0.4}
            if self._llm.provider == "anthropic":
                kwargs["system"] = system_prompt
            return await _llm_text(self._llm, messages, **kwargs)
        except Exception as e:
            logger.error(f"[TerrainAgent] Response synthesis failed: {e}")
            return f"Terrain analysis could not be completed: {str(e)}"

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_or_create_session(
        self,
        session_id: str,
        latitude: float,
        longitude: float,
    ) -> TerrainAgentSession:
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.update_location(latitude, longitude)
            return session
        session = TerrainAgentSession(session_id, latitude, longitude)
        self.sessions[session_id] = session
        logger.info(f"TerrainAgent: created session {session_id}")
        return session

    def cleanup_old_sessions(self, max_age_minutes: int = 60):
        now = datetime.utcnow()
        expired = [
            sid for sid, s in self.sessions.items()
            if (now - s.last_activity).total_seconds() > max_age_minutes * 60
        ]
        for sid in expired:
            del self.sessions[sid]
            logger.info(f"TerrainAgent: expired session {sid}")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def chat(
        self,
        session_id: str,
        user_message: str,
        latitude: float,
        longitude: float,
        screenshot_base64: Optional[str] = None,
        radius_km: float = 5.0,
    ) -> Dict[str, Any]:
        await self._ensure_initialized()

        session = self._get_or_create_session(session_id, latitude, longitude)

        # ---- Reverse geocode + vision in parallel ----
        import asyncio

        async def _reverse_geocode() -> str:
            fallback = f"Location ({latitude:.4f}, {longitude:.4f})"
            try:
                from semantic_translator import geocoding_plugin
                rg = await geocoding_plugin.azure_maps_reverse_geocode(latitude, longitude)
                data = json.loads(rg)
                if not data.get("error"):
                    n = data.get("name", "")
                    r = data.get("region", "")
                    c = data.get("country", "")
                    parts = [p for p in [r, c] if p and p != n]
                    return f"{n}, {', '.join(parts)}" if n and parts else n or fallback
            except Exception as e:
                logger.warning(f"Reverse geocode exception: {e}")
            return fallback

        async def _vision() -> Optional[str]:
            if not screenshot_base64 or len(screenshot_base64) < 5000:
                return None
            try:
                return await asyncio.wait_for(
                    self._analyze_screenshot(screenshot_base64, latitude, longitude),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Terrain vision analysis timed out — skipping")
            except Exception as e:
                logger.warning(f"Terrain vision error: {e}")
            return None

        location_name, visual_analysis = await asyncio.gather(_reverse_geocode(), _vision())
        logger.info(f"TerrainAgent: location={location_name}, vision={len(visual_analysis) if visual_analysis else 0} chars")

        # ---- Build context message ----
        context_message = (
            f"[Location Context]\n"
            f"- Location: {location_name}\n"
            f"- Coordinates: ({latitude:.6f}, {longitude:.6f})\n"
            f"- Analysis radius: {radius_km} km\n"
        )
        if visual_analysis:
            context_message += f"\n[Visual Analysis of Current Map View]\n{visual_analysis}\n"
        context_message += f"\n[User Question]\n{user_message}"

        # ---- Select and run tools ----
        tool_names = await self._select_tools(user_message)
        logger.info(f"TerrainAgent: selected tools {tool_names}")

        loop = asyncio.get_event_loop()
        tool_results: Dict[str, Any] = {}
        tool_calls_meta: List[Dict[str, Any]] = []

        for tool_name in tool_names:
            try:
                result = await loop.run_in_executor(
                    None,
                    self._call_tool,
                    tool_name,
                    latitude,
                    longitude,
                    radius_km,
                )
                tool_results[tool_name] = result
                tool_calls_meta.append({"tool": tool_name, "result": result})
                logger.info(f"TerrainAgent: {tool_name} → {str(result)[:120]}")
            except Exception as e:
                logger.error(f"TerrainAgent: {tool_name} executor failed: {e}")
                tool_results[tool_name] = {"error": str(e)}

        # ---- Synthesise response ----
        response_text = await self._synthesise_response(session, context_message, tool_results)

        # Update conversation history
        session.history.append({"role": "user", "content": context_message})
        session.history.append({"role": "assistant", "content": response_text})
        session.message_count += 2
        session.last_activity = datetime.utcnow()

        return {
            "response": response_text,
            "tool_calls": tool_calls_meta,
            "session_id": session_id,
            "message_count": session.message_count,
            "location": {"latitude": latitude, "longitude": longitude},
        }

    async def get_session_history(self, session_id: str) -> List[Dict[str, str]]:
        if session_id not in self.sessions:
            return []
        return list(self.sessions[session_id].history)

    async def clear_session(self, session_id: str) -> bool:
        if session_id in self.sessions:
            del self.sessions[session_id]
            logger.info(f"TerrainAgent: cleared session {session_id}")
            return True
        return False

    async def cleanup(self):
        pass  # Nothing to clean up


# Singleton
_terrain_agent: Optional[TerrainAgent] = None


def get_terrain_agent() -> TerrainAgent:
    global _terrain_agent
    if _terrain_agent is None:
        _terrain_agent = TerrainAgent()
    return _terrain_agent
