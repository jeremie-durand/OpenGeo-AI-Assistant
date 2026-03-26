"""
Extreme Weather Agent — Provider-agnostic rewrite

Uses LLM_PROVIDER / LLM_API_KEY / LLM_MODEL (same as the rest of the app).
No Azure AI Agent Service dependency.

Flow per query:
  1. LLM selects which climate tools to call
  2. Tools are called directly (sync, in executor)
  3. Optional screenshot vision analysis
  4. LLM synthesises a natural-language response
  5. Conversation history kept in-process per session
"""

import logging
import json
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)

# Reverse-geocode cache: same coordinates always return the same place name.
_reverse_geocode_cache: Dict[str, str] = {}

EXTREME_WEATHER_AGENT_INSTRUCTIONS = """You are a Climate & Extreme Weather Analysis Agent specializing in future climate projections using NASA NEX-GDDP-CMIP6 data.

Your role is to analyze projected climate conditions for any location on Earth using downscaled CMIP6 global climate model outputs.

## Data Source
All data comes from **NASA NEX-GDDP-CMIP6** on Microsoft Planetary Computer:
- Resolution: 0.25° × 0.25° global grid (~25 km)
- Time range: 2015–2100
- These are climate **projections**, not real-time observations

## SSP Scenarios
- **SSP2-4.5** ("Middle of the Road"): Moderate emissions
- **SSP5-8.5** ("Fossil-fuel Development"): Worst-case, high emissions

## Available Tools
- **get_temperature_projection**: Max, min, mean daily temperature (°C)
- **get_precipitation_projection**: Daily precipitation (mm/day), ensemble range
- **get_wind_projection**: Near-surface wind speed (m/s) with Beaufort classification
- **get_humidity_projection**: Relative humidity (%) and specific humidity (g/kg)
- **get_radiation_projection**: Shortwave/longwave radiation (W/m²)
- **get_climate_overview**: All key variables in one call
- **compare_climate_scenarios**: SSP2-4.5 vs SSP5-8.5 comparison

## Response Guidelines
1. Start with the location name — never just coordinates
2. Explain the numbers in human-understandable context
3. Note that these are projections, not forecasts
4. Mention "NASA NEX-GDDP-CMIP6 (0.25° resolution)" at least once
5. **Summary**: ALWAYS conclude with a clear, direct answer grounded in tool data

## EFFICIENCY
- For general climate: call **get_climate_overview** once — do NOT call individual tools separately
- For trends: call at most 2 key years (e.g. 2030 and 2070)
- NEVER call more than 3 tools per message
"""

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: Dict[str, Any] = {}


def _get_tool_registry() -> Dict[str, Any]:
    global _TOOL_REGISTRY
    if not _TOOL_REGISTRY:
        from geoint.extreme_weather_tools import (
            get_temperature_projection,
            get_precipitation_projection,
            get_wind_projection,
            get_humidity_projection,
            get_radiation_projection,
            get_climate_overview,
            compare_climate_scenarios,
        )
        _TOOL_REGISTRY = {
            "get_temperature_projection": get_temperature_projection,
            "get_precipitation_projection": get_precipitation_projection,
            "get_wind_projection": get_wind_projection,
            "get_humidity_projection": get_humidity_projection,
            "get_radiation_projection": get_radiation_projection,
            "get_climate_overview": get_climate_overview,
            "compare_climate_scenarios": compare_climate_scenarios,
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
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


async def _llm_text(llm, messages, max_tokens=512, temperature=0.2, **kwargs):
    response = await llm.chat(messages, max_tokens=max_tokens, temperature=temperature, **kwargs)
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

class ExtremeWeatherAgentSession:
    def __init__(self, session_id: str, latitude: float, longitude: float):
        self.session_id = session_id
        self.latitude = latitude
        self.longitude = longitude
        self.created_at = datetime.utcnow()
        self.last_activity = datetime.utcnow()
        self.message_count = 0
        self.history: List[Dict[str, Any]] = []

    def update_location(self, latitude: float, longitude: float):
        self.latitude = latitude
        self.longitude = longitude
        self.last_activity = datetime.utcnow()


# ---------------------------------------------------------------------------
# ExtremeWeatherAgent
# ---------------------------------------------------------------------------

class ExtremeWeatherAgent:
    """Provider-agnostic extreme weather / climate projection agent."""

    def __init__(self):
        self._llm = None
        self._initialized = False
        self.sessions: Dict[str, ExtremeWeatherAgentSession] = {}
        logger.info("ExtremeWeatherAgent created (provider-agnostic, lazy init)")

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
                    logger.warning(f"[RETRY] ExtremeWeatherAgent init attempt {attempt + 1} failed: {e} — retrying in {wait}s")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"ExtremeWeatherAgent init failed after 3 attempts: {e}")
                    raise

    async def _do_initialize(self):
        from llm_client import get_llm_client
        compat = get_llm_client()
        self._llm = compat._llm
        self._initialized = True
        logger.info(f"ExtremeWeatherAgent initialised (provider={self._llm.provider}, model={self._llm.model})")

    # ------------------------------------------------------------------
    # Step 1 — select which tools to call
    # ------------------------------------------------------------------

    async def _select_tools(self, user_query: str) -> List[Dict[str, Any]]:
        """Ask LLM to pick tools and parameters from the user query."""
        prompt = f"""Given this climate projection question, select the tools to call and their parameters.

USER QUESTION: "{user_query}"

Available tools:
- get_temperature_projection(latitude, longitude, scenario, year) — temperature questions
- get_precipitation_projection(latitude, longitude, scenario, year) — rain/precipitation questions
- get_wind_projection(latitude, longitude, scenario, year) — wind questions
- get_humidity_projection(latitude, longitude, scenario, year) — humidity questions
- get_radiation_projection(latitude, longitude, scenario, year) — solar/radiation questions
- get_climate_overview(latitude, longitude, scenario, year) — general climate, mixed questions
- compare_climate_scenarios(latitude, longitude, year) — compare SSP2-4.5 vs SSP5-8.5

Rules:
- For general/overview questions: use ONLY get_climate_overview
- For compare/contrast scenario questions: use ONLY compare_climate_scenarios
- For trend questions: call the relevant tool for 2 years max (e.g. year=2030 and year=2070)
- Default scenario: "ssp585", default year: 2030
- NEVER select more than 3 tool calls total
- The latitude/longitude will be substituted at runtime — use 0.0 as placeholders

Return ONLY a valid JSON array of calls, e.g.:
[{{"tool": "get_temperature_projection", "scenario": "ssp585", "year": 2030}}]"""

        try:
            raw = await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=256,
                temperature=0.0,
            )
            parsed = _parse_json_safe(raw)
            if isinstance(parsed, list) and parsed:
                return parsed[:3]
        except Exception as e:
            logger.error(f"[ExtremeWeatherAgent] Tool selection failed: {e}")

        return [{"tool": "get_climate_overview", "scenario": "ssp585", "year": 2030}]

    # ------------------------------------------------------------------
    # Step 2 — call tools
    # ------------------------------------------------------------------

    @staticmethod
    def _call_tool(tool_name: str, latitude: float, longitude: float,
                   scenario: str = "ssp585", year: int = 2030) -> str:
        registry = _get_tool_registry()
        fn = registry.get(tool_name)
        if not fn:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        try:
            if tool_name == "compare_climate_scenarios":
                return fn(latitude=latitude, longitude=longitude, year=year)
            return fn(latitude=latitude, longitude=longitude, scenario=scenario, year=year)
        except Exception as e:
            logger.error(f"[ExtremeWeatherAgent] Tool {tool_name} failed: {e}")
            return json.dumps({"error": str(e)})

    # ------------------------------------------------------------------
    # Step 3 — screenshot vision (optional)
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
                f"Analyze this satellite/map image for climate-relevant geographic context.\n"
                f"Location: approximately ({latitude:.4f}, {longitude:.4f})\n\n"
                "Identify: land use, water features, terrain, coastal/urban areas relevant to climate impacts. "
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

            result = await _llm_text(self._llm, messages, max_tokens=500, temperature=0.3)
            logger.info(f"Climate vision analysis: {len(result)} chars")
            return result
        except Exception as e:
            logger.warning(f"Climate vision analysis failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Step 4 — synthesise response
    # ------------------------------------------------------------------

    async def _synthesise_response(
        self,
        user_query: str,
        location_name: str,
        latitude: float,
        longitude: float,
        tool_results: Dict[str, str],
        visual_analysis: Optional[str],
        session: ExtremeWeatherAgentSession,
    ) -> str:
        tool_summary = "\n\n".join(
            f"### {name}\n{result}" for name, result in tool_results.items()
        )[:3000]

        context = (
            f"[Location Context]\n"
            f"- Location: {location_name}\n"
            f"- Coordinates: ({latitude:.6f}, {longitude:.6f})\n"
        )
        if visual_analysis:
            context += f"\n[Visual Analysis of Current Map View]\n{visual_analysis}\n"

        user_content = (
            f"{context}\n"
            f"[Tool Results]\n{tool_summary}\n\n"
            f"[User Question]\n{user_query}\n\n"
            "Based on the tool results above, write a clear climate projection analysis following "
            "the Response Format in your instructions."
        )

        messages = []
        if self._llm.provider != "anthropic":
            messages.append({"role": "system", "content": EXTREME_WEATHER_AGENT_INSTRUCTIONS})
        messages.extend(session.history[-8:])
        messages.append({"role": "user", "content": user_content})

        kwargs: Dict[str, Any] = {"max_tokens": 1200, "temperature": 0.4}
        if self._llm.provider == "anthropic":
            kwargs["system"] = EXTREME_WEATHER_AGENT_INSTRUCTIONS

        try:
            return await _llm_text(self._llm, messages, **kwargs)
        except Exception as e:
            logger.error(f"[ExtremeWeatherAgent] Synthesis failed: {e}")
            return f"Climate analysis could not be completed: {str(e)}"

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_or_create_session(
        self, session_id: str, latitude: float, longitude: float
    ) -> ExtremeWeatherAgentSession:
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.update_location(latitude, longitude)
            return session
        session = ExtremeWeatherAgentSession(session_id, latitude, longitude)
        self.sessions[session_id] = session
        return session

    def cleanup_old_sessions(self, max_age_minutes: int = 60):
        now = datetime.utcnow()
        expired = [
            sid for sid, s in self.sessions.items()
            if (now - s.last_activity).total_seconds() > max_age_minutes * 60
        ]
        for sid in expired:
            del self.sessions[sid]
            logger.info(f"ExtremeWeatherAgent: expired session {sid}")

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
            cache_key = f"{latitude:.4f}:{longitude:.4f}"
            if cache_key in _reverse_geocode_cache:
                return _reverse_geocode_cache[cache_key]
            fallback = f"Location ({latitude:.4f}, {longitude:.4f})"
            try:
                from semantic_translator import geocoding_plugin
                rg = await geocoding_plugin.reverse_geocode(latitude, longitude)
                data = json.loads(rg)
                if not data.get("error"):
                    n = data.get("name", "")
                    r = data.get("region", "")
                    c = data.get("country", "")
                    parts = [p for p in [r, c] if p and p != n]
                    result = f"{n}, {', '.join(parts)}" if n and parts else n or fallback
                    _reverse_geocode_cache[cache_key] = result
                    return result
            except Exception as e:
                logger.warning(f"Reverse geocode exception: {e}")
            _reverse_geocode_cache[cache_key] = fallback
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
                logger.warning("Climate vision analysis timed out — skipping")
            except Exception as e:
                logger.warning(f"Climate vision error: {e}")
            return None

        location_name, visual_analysis = await asyncio.gather(_reverse_geocode(), _vision())
        logger.info(f"ExtremeWeatherAgent: location={location_name}, vision={len(visual_analysis) if visual_analysis else 0} chars")

        # ---- Select and run tools ----
        tool_calls_spec = await self._select_tools(user_message)
        logger.info(f"ExtremeWeatherAgent: selected tools {[t['tool'] for t in tool_calls_spec]}")

        loop = asyncio.get_event_loop()
        tool_results: Dict[str, str] = {}
        tool_calls_meta: List[Dict[str, Any]] = []

        for spec in tool_calls_spec:
            tool_name = spec.get("tool", "get_climate_overview")
            scenario = spec.get("scenario", "ssp585")
            year = int(spec.get("year", 2030))
            label = f"{tool_name}({scenario},{year})"

            try:
                result_str = await loop.run_in_executor(
                    None,
                    self._call_tool,
                    tool_name,
                    latitude,
                    longitude,
                    scenario,
                    year,
                )
                tool_results[label] = result_str
                tool_calls_meta.append({"tool": tool_name, "scenario": scenario, "year": year, "result": result_str[:300]})
                logger.info(f"ExtremeWeatherAgent: {label} → {result_str[:120]}")
            except Exception as e:
                logger.error(f"ExtremeWeatherAgent: {tool_name} executor failed: {e}")
                tool_results[label] = json.dumps({"error": str(e)})

        # ---- Synthesise response ----
        response_text = await self._synthesise_response(
            user_query=user_message,
            location_name=location_name,
            latitude=latitude,
            longitude=longitude,
            tool_results=tool_results,
            visual_analysis=visual_analysis,
            session=session,
        )

        session.history.append({"role": "user", "content": user_message})
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
            logger.info(f"ExtremeWeatherAgent: cleared session {session_id}")
            return True
        return False

    async def cleanup(self):
        pass  # Nothing to clean up


# Singleton
_extreme_weather_agent: Optional[ExtremeWeatherAgent] = None


def get_extreme_weather_agent() -> ExtremeWeatherAgent:
    global _extreme_weather_agent
    if _extreme_weather_agent is None:
        _extreme_weather_agent = ExtremeWeatherAgent()
    return _extreme_weather_agent
