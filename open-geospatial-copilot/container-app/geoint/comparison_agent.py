"""
GEOINT Comparison Agent — Provider-agnostic rewrite

Uses LLM_PROVIDER / LLM_API_KEY / LLM_MODEL (same as the rest of the app).
No Azure AI Agent Service dependency.

Flow per query:
  1. LLM parses the query → extracts location, before_period, after_period, analysis_type
  2. compare_temporal_imagery tool is called directly
  3. LLM synthesises a natural-language response from the tool output
"""

import logging
import json
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

COMPARISON_AGENT_INSTRUCTIONS = """You are a GEOINT Temporal Comparison Agent specializing in before/after satellite imagery analysis for change detection.

Your role is to compare satellite imagery across two time periods and analyze what has changed at a given location.

## Available Tools:

- **compare_temporal_imagery**: Compare satellite imagery between two time periods. Executes dual STAC queries and returns before/after tile URLs for map display with scene counts and metadata.
- **search_stac_for_period**: Search the STAC catalog for available imagery in a specific time period and location. Returns matching scenes with dates and cloud cover.
- **analyze_comparison_imagery**: Analyze visual differences between before and after imagery using AI vision. Call this AFTER compare_temporal_imagery to provide an AI-powered description of changes.

## Analysis Types Supported (use these as the analysis_type parameter):
- **surface reflectance**: Overall reflectance changes (default)
- **vegetation** or **ndvi**: Vegetation health and cover changes
- **water** or **flood**: Water body extent changes
- **snow**: Snow cover changes
- **fire**: Fire/wildfire activity (MODIS thermal detection)
- **sar** or **radar**: SAR radar imagery (Sentinel-1, works day/night/through clouds)

## Collections Available:
- sentinel-2-l2a / sentinel / sentinel-2: Optical imagery (10m, cloud-filtered)
- landsat-c2-l2 / landsat: Landsat optical (30m)
- hls2-l30 / hls: Harmonized Landsat Sentinel (30m)
- jrc-gsw / water: Water occurrence
- modis-10A1-061 / snow: Snow cover
- modis-14A1-061 / fire / modis fire / wildfire: Fire detection (MODIS thermal)
- sentinel-1-rtc / sentinel-1 / sar / radar: SAR radar imagery (works through clouds)

## Response Format:
1. **Location**: Name and coordinates
2. **Time Periods**: Before and after dates with available scene counts
3. **Change Analysis**: Observations about what changed
4. **Instructions**: Remind user to use the BEFORE/AFTER toggle buttons on the map
5. **Summary**: ALWAYS conclude with a **Summary** section giving a clear, direct answer

Keep responses factual and concise.
"""

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
# ComparisonAgent
# ---------------------------------------------------------------------------

class ComparisonAgent:
    """Provider-agnostic temporal comparison agent."""

    def __init__(self):
        self._llm = None
        self._initialized = False
        self.name = "geoint_comparison"
        logger.info("ComparisonAgent created (provider-agnostic, lazy init)")

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
                    logger.warning(f"ComparisonAgent init attempt {attempt + 1} failed: {e} — retrying in {wait}s")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"ComparisonAgent init failed after 3 attempts: {e}")
                    raise

    async def _do_initialize(self):
        from llm_client import get_llm_client
        compat = get_llm_client()
        self._llm = compat._llm
        self._initialized = True
        logger.info(f"ComparisonAgent initialised (provider={self._llm.provider}, model={self._llm.model})")

    # ------------------------------------------------------------------
    # Step 1 — parse the user query with the LLM
    # ------------------------------------------------------------------

    async def _parse_query(
        self,
        user_query: str,
        latitude: Optional[float],
        longitude: Optional[float],
    ) -> Dict[str, Any]:
        location_hint = ""
        if latitude is not None and longitude is not None:
            location_hint = f"The user's map pin is at ({latitude:.6f}, {longitude:.6f}). Use these coordinates as the location if no explicit place name is given."

        prompt = f"""Extract the geospatial comparison parameters from the user query below.

{location_hint}

USER QUERY: "{user_query}"

Return ONLY valid JSON:
{{
  "location": "<place name or 'LAT,LON' string>",
  "before_period": "<YYYY-MM or YYYY or YYYY-MM-DD/YYYY-MM-DD>",
  "after_period":  "<YYYY-MM or YYYY or YYYY-MM-DD/YYYY-MM-DD>",
  "analysis_type": "<surface reflectance | vegetation | water | flood | snow | fire | sar>"
}}

Rules:
- location: prefer named place; fall back to coordinate string "LAT,LON" if only coords available
- before_period / after_period: use the earlier date as "before"
- analysis_type: infer from keywords (vegetation/NDVI, fire/wildfire, water/flood, SAR/radar, snow, else surface reflectance)
- If a time period is missing, use a reasonable default (e.g. one year ago vs now)"""

        try:
            raw = await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=256,
                temperature=0.0,
            )
            parsed = _parse_json_safe(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception as e:
            logger.error(f"[ComparisonAgent] Query parsing failed: {e}")

        # Fallback with coordinates
        loc = f"{latitude:.6f},{longitude:.6f}" if latitude is not None else "unknown"
        return {
            "location": loc,
            "before_period": "2020-01",
            "after_period": "2024-01",
            "analysis_type": "surface reflectance",
        }

    # ------------------------------------------------------------------
    # Step 2 — call the comparison tool directly
    # ------------------------------------------------------------------

    @staticmethod
    def _run_comparison_tool(location: str, before_period: str, after_period: str, analysis_type: str) -> Dict[str, Any]:
        from geoint.comparison_tools import compare_temporal_imagery, reset_comparison_capture, get_last_comparison_result
        reset_comparison_capture()
        raw = compare_temporal_imagery(
            location=location,
            before_period=before_period,
            after_period=after_period,
            analysis_type=analysis_type,
        )
        # compare_temporal_imagery already captures the result module-level
        captured = get_last_comparison_result()
        if captured and captured.get("status") == "success":
            return captured
        # Try to parse the raw string output
        parsed = _parse_json_safe(raw) if isinstance(raw, str) else None
        if isinstance(parsed, dict):
            return parsed
        return {"status": "no_data", "raw": raw}

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
                f"Analyze this satellite/map image for temporal comparison context.\n"
                f"Location: approximately ({latitude:.4f}, {longitude:.4f})\n\n"
                "Identify land use, water features, vegetation, infrastructure, and any visible change indicators. "
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
            logger.info(f"Comparison vision analysis: {len(result)} chars")
            return result
        except Exception as e:
            logger.warning(f"Comparison vision analysis failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Step 4 — synthesise the final response
    # ------------------------------------------------------------------

    async def _synthesise_response(
        self,
        user_query: str,
        params: Dict[str, Any],
        tool_result: Dict[str, Any],
        visual_analysis: Optional[str],
    ) -> str:
        result_summary = json.dumps(tool_result, indent=2)[:2000]
        visual_section = f"\n\n[Visual Analysis of Current Map View]\n{visual_analysis}" if visual_analysis else ""

        prompt = f"""{COMPARISON_AGENT_INSTRUCTIONS}

---

[User Question]
{user_query}

[Tool Result — compare_temporal_imagery]
{result_summary}{visual_section}

Based on the tool result above, write a clear, structured comparison analysis following the Response Format in your instructions.
If the tool returned no data or an error, explain what happened and suggest alternatives."""

        try:
            return await _llm_text(
                self._llm,
                [{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.4,
            )
        except Exception as e:
            logger.error(f"[ComparisonAgent] Response synthesis failed: {e}")
            status = tool_result.get("status", "unknown")
            if status == "success":
                return f"Comparison data retrieved successfully for {params.get('location')}. Before: {params.get('before_period')}, After: {params.get('after_period')}. Use the BEFORE/AFTER toggle on the map to view the imagery."
            return f"Comparison analysis could not be completed: {str(e)}"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle_query(
        self,
        user_query: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        session_id: Optional[str] = None,
        screenshot_base64: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not user_query or user_query.strip().lower() in ("", "hi", "hello", "comparison", "start"):
            return {
                "status": "prompt",
                "message": (
                    "Please specify the location, date range, and what you would like to compare.\n\n"
                    "Example: *How did Miami Beach surface reflectance change between 01/2020 and 01/2025?*"
                ),
                "type": "comparison",
            }

        await self._ensure_initialized()

        # Step 1 — parse
        params = await self._parse_query(user_query, latitude, longitude)
        logger.info(f"[ComparisonAgent] Parsed params: {params}")

        # Step 2 — screenshot vision (optional, 15s cap)
        visual_analysis = None
        if screenshot_base64 and latitude is not None and longitude is not None:
            import asyncio
            try:
                visual_analysis = await asyncio.wait_for(
                    self._analyze_screenshot(screenshot_base64, latitude, longitude),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Comparison vision analysis timed out (15s) — skipping")
            except Exception as e:
                logger.warning(f"Comparison vision analysis error: {e}")

        # Step 3 — run comparison tool (sync, in executor to avoid blocking)
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            tool_result = await loop.run_in_executor(
                None,
                self._run_comparison_tool,
                params.get("location", "unknown"),
                params.get("before_period", "2020-01"),
                params.get("after_period", "2024-01"),
                params.get("analysis_type", "surface reflectance"),
            )
        except Exception as e:
            logger.error(f"[ComparisonAgent] Tool execution failed: {e}")
            tool_result = {"status": "error", "error": str(e)}

        # Step 4 — synthesise response
        analysis_text = await self._synthesise_response(user_query, params, tool_result, visual_analysis)

        result: Dict[str, Any] = {
            "status": "success",
            "type": "comparison",
            "analysis": analysis_text,
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Merge tile URLs / spatial data from tool result
        if tool_result.get("status") == "success":
            result.update({
                "location": tool_result.get("location"),
                "bbox": tool_result.get("bbox"),
                "center": tool_result.get("center"),
                "before": tool_result.get("before"),
                "after": tool_result.get("after"),
                "collection": tool_result.get("collection"),
            })

        return result

    async def chat(
        self,
        session_id: str,
        user_message: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        screenshot_base64: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.handle_query(
            user_query=user_message,
            latitude=latitude,
            longitude=longitude,
            session_id=session_id,
            screenshot_base64=screenshot_base64,
        )

    async def cleanup(self):
        pass  # Nothing to clean up


# Singleton
_comparison_agent: Optional[ComparisonAgent] = None


def get_comparison_agent() -> ComparisonAgent:
    global _comparison_agent
    if _comparison_agent is None:
        _comparison_agent = ComparisonAgent()
    return _comparison_agent
