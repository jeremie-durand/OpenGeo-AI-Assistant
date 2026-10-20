"""GEOINT extreme-weather (CMIP6) analysis and diagnostic endpoints."""

import json
import logging
import traceback
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from request_models import ExtremeWeatherRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/geoint/extreme-weather")
async def geoint_extreme_weather_analysis(body: ExtremeWeatherRequest):
    """
    [STORM] GEOINT Extreme Weather & Climate Projection Analysis

    Climate projections from NASA NEX-GDDP-CMIP6 (NetCDF data, point-sampled).
    Returns temperature, precipitation, wind, humidity, and radiation projections.
    No map tiles — data is chat-based point values only.

    Request body:
    {
        "latitude": float,
        "longitude": float,
        "user_query": str (optional - climate question),
        "user_context": str (optional - legacy field name),
        "session_id": str (optional - for follow-up questions)
    }

    Response includes session_id for follow-up questions.
    """
    import uuid

    request_id = f"climate-{datetime.now(timezone.utc).timestamp()}"
    try:
        logger.info(f"[STORM] [{request_id}] EXTREME WEATHER ENDPOINT CALLED")

        latitude = body.latitude
        longitude = body.longitude
        user_query = body.user_query
        session_id = body.session_id
        screenshot = body.screenshot

        logger.info(
            f"[STORM] [{request_id}] Coordinates: ({latitude}, {longitude}), query: {user_query}"
        )

        import asyncio
        import time

        agent_start = time.time()

        # Use ExtremeWeatherAgent with conversation memory
        from geoint.extreme_weather_agent import get_extreme_weather_agent

        agent = get_extreme_weather_agent()

        if not session_id:
            session_id = str(uuid.uuid4())
            logger.info(f"[STORM] [{request_id}] Created new session: {session_id}")

        message = (
            user_query
            or "Provide a comprehensive climate projection overview for this location."
        )

        # ====================================================================
        # FAST PATH: Direct tool call for overview queries
        # ====================================================================
        # For first-time "general overview" queries, bypass the Agent Service
        # entirely.  Instead of LLM→plan→tool→results→LLM→synthesize
        # (2 LLM round-trips, ~10-20s overhead), call get_climate_overview
        # directly and format the results with a single LLM call.
        # This saves ~8-20s per overview query.
        # Only for new sessions with no custom query or the default overview query.
        is_overview_query = (
            not user_query
            or "overview" in message.lower()
            or ("climate" in message.lower() and "projection" in message.lower())
            or message
            == "Provide a comprehensive climate projection overview for this location."
        )

        if is_overview_query and not session_id:
            try:
                logger.info(
                    f"[STORM] [{request_id}] FAST PATH: Direct get_climate_overview call"
                )
                from geoint.extreme_weather_agent import _reverse_geocode_cache
                from geoint.extreme_weather_tools import get_climate_overview

                # Run climate overview in thread pool (it's sync code with blocking I/O)
                loop = asyncio.get_event_loop()
                raw_result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, get_climate_overview, latitude, longitude, "ssp585", 2030
                    ),
                    timeout=120.0,
                )

                overview_data = json.loads(raw_result)
                fast_elapsed = time.time() - agent_start
                logger.info(
                    f"[STORM] [{request_id}] FAST PATH: Data fetched in {fast_elapsed:.1f}s"
                )

                if "error" not in overview_data:
                    # Resolve location name (cached)
                    geo_key = f"{latitude:.4f}:{longitude:.4f}"
                    location_name = _reverse_geocode_cache.get(
                        geo_key, f"({latitude:.4f}, {longitude:.4f})"
                    )

                    # Single LLM call to format the raw data into prose
                    from semantic_translator import get_llm_client

                    _fmt_client = get_llm_client()

                    fmt_resp = await _fmt_client.chat.completions.create(
                        messages=[
                            {
                                "role": "system",
                                "content": "You are a climate analyst. Format the provided climate projection data into a clear, informative summary. Use the same style and formatting as your normal extreme weather analysis responses. Include all available metrics with their values and units.",
                            },
                            {
                                "role": "user",
                                "content": f"Format this climate projection data for {location_name} (SSP5-8.5, 2030) into a comprehensive summary:\n\n{raw_result}",
                            },
                        ],
                        max_completion_tokens=2000,
                        temperature=0.3,
                    )

                    formatted_response = fmt_resp.choices[0].message.content
                    total_elapsed = time.time() - agent_start
                    logger.info(
                        f"[STORM] [{request_id}] FAST PATH COMPLETE in {total_elapsed:.1f}s (data: {fast_elapsed:.1f}s, format: {total_elapsed - fast_elapsed:.1f}s)"
                    )

                    # --------------------------------------------------------
                    # PRE-WARM: Kick off background ssp245 STAC item fetch
                    # so the cache is ready if user follows up with a
                    # "compare scenarios" question (saves ~2-5s on compare).
                    # --------------------------------------------------------
                    try:
                        from geoint.extreme_weather_tools import (
                            _search_cmip6_items as _prewarm_stac,
                        )

                        loop.run_in_executor(
                            None,
                            _prewarm_stac,
                            latitude,
                            longitude,
                            "tasmax",
                            "ssp245",
                            2030,
                            1,
                        )
                        logger.info(
                            f"[STORM] [{request_id}] Pre-warming ssp245 STAC cache"
                        )
                    except Exception:
                        pass  # best-effort

                    return {
                        "status": "success",
                        "result": {
                            "analysis": formatted_response,
                            "tool_calls": [
                                {
                                    "tool": "get_climate_overview",
                                    "result": overview_data,
                                }
                            ],
                            "message_count": 1,
                        },
                        "session_id": session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
            except asyncio.TimeoutError:
                logger.warning(
                    f"[STORM] [{request_id}] FAST PATH timed out, falling back to agent"
                )
            except Exception as e:
                logger.warning(
                    f"[STORM] [{request_id}] FAST PATH failed ({e}), falling back to agent"
                )

        # ====================================================================
        # FAST PATH 2: Direct tool call for comparison queries
        # ====================================================================
        # Detects "compare scenarios" / "SSP245 vs SSP585" patterns and calls
        # compare_climate_scenarios directly, saving 2 LLM round-trips (~8-15s).
        # Works for both new and follow-up sessions.
        import re as _re

        _msg_lower = message.lower()
        is_comparison_query = (
            (
                "compare" in _msg_lower
                and ("scenario" in _msg_lower or "ssp" in _msg_lower)
            )
            or ("ssp245" in _msg_lower and "ssp585" in _msg_lower)
            or ("ssp2" in _msg_lower and "ssp5" in _msg_lower)
            or (
                "moderate" in _msg_lower
                and "worst" in _msg_lower
                and (
                    "scenario" in _msg_lower
                    or "emission" in _msg_lower
                    or "climate" in _msg_lower
                )
            )
        )

        if is_comparison_query:
            try:
                logger.info(
                    f"[STORM] [{request_id}] FAST PATH COMPARE: Direct compare_climate_scenarios call"
                )
                from geoint.extreme_weather_agent import _reverse_geocode_cache
                from geoint.extreme_weather_tools import compare_climate_scenarios

                # Extract year from query if mentioned (e.g. "by 2050", "in 2060")
                year_match = _re.search(r"\b(20[2-9]\d|2100)\b", message)
                target_year = int(year_match.group()) if year_match else 2030

                loop = asyncio.get_event_loop()
                raw_result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        compare_climate_scenarios,
                        latitude,
                        longitude,
                        target_year,
                    ),
                    timeout=120.0,
                )

                comparison_data = json.loads(raw_result)
                fast_elapsed = time.time() - agent_start
                logger.info(
                    f"[STORM] [{request_id}] FAST PATH COMPARE: Data fetched in {fast_elapsed:.1f}s"
                )

                if "error" not in comparison_data:
                    # Resolve location name (cached)
                    geo_key = f"{latitude:.4f}:{longitude:.4f}"
                    location_name = _reverse_geocode_cache.get(
                        geo_key, f"({latitude:.4f}, {longitude:.4f})"
                    )

                    # Single LLM call to format comparison data into prose
                    from semantic_translator import get_llm_client

                    _fmt_client = get_llm_client()

                    fmt_resp = await _fmt_client.chat.completions.create(
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "You are a climate analyst. Format the provided climate scenario comparison data "
                                    "into a clear, informative analysis. Compare the moderate (SSP2-4.5) and worst-case "
                                    "(SSP5-8.5) scenarios side by side. Highlight the temperature and precipitation "
                                    "differences and their real-world implications for the region. "
                                    "Use the same style and formatting as your normal extreme weather analysis responses."
                                ),
                            },
                            {
                                "role": "user",
                                "content": (
                                    f"Format this climate scenario comparison for {location_name} into a comprehensive analysis. "
                                    f"The user asked: '{user_query}'\n\nRaw data:\n{raw_result}"
                                ),
                            },
                        ],
                        max_completion_tokens=2000,
                        temperature=0.3,
                    )

                    formatted_response = fmt_resp.choices[0].message.content
                    total_elapsed = time.time() - agent_start
                    logger.info(
                        f"[STORM] [{request_id}] FAST PATH COMPARE COMPLETE in {total_elapsed:.1f}s (data: {fast_elapsed:.1f}s, format: {total_elapsed - fast_elapsed:.1f}s)"
                    )

                    return {
                        "status": "success",
                        "result": {
                            "analysis": formatted_response,
                            "tool_calls": [
                                {
                                    "tool": "compare_climate_scenarios",
                                    "result": comparison_data,
                                }
                            ],
                            "message_count": 1,
                        },
                        "session_id": session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
            except asyncio.TimeoutError:
                logger.warning(
                    f"[STORM] [{request_id}] FAST PATH COMPARE timed out, falling back to agent"
                )
            except Exception as e:
                logger.warning(
                    f"[STORM] [{request_id}] FAST PATH COMPARE failed ({e}), falling back to agent"
                )

        # ====================================================================
        # FAST PATH 3: Direct tool call for specific variable queries
        # ====================================================================
        # Detects "temperature" / "precipitation" / "wind" / "humidity" / "radiation"
        # and calls the matching tool directly.  Saves 2 LLM round-trips.
        #
        # NOTE: Cold-cache NetCDF reads from NASA Blob Storage can take 40-60s
        # per model.  With 3 models sampled in parallel, a single variable call
        # may take 60-180s on cold cache.  Timeout is set to 200s to cover
        # worst-case cold reads + STAC search overhead.
        _VARIABLE_FAST_PATHS = {
            "temperature": (
                "get_temperature_projection",
                ["temperature", "temp ", "how hot", "heat"],
            ),
            "precipitation": (
                "get_precipitation_projection",
                [
                    "precipitation",
                    "precip",
                    "rain",
                    "rainfall",
                    "monsoon",
                    "flooding",
                    "flood risk",
                ],
            ),
            "wind": (
                "get_wind_projection",
                ["wind speed", "wind projection", "how windy"],
            ),
            "humidity": ("get_humidity_projection", ["humidity", "humid", "moisture"]),
            "radiation": (
                "get_radiation_projection",
                ["radiation", "solar", "shortwave", "longwave"],
            ),
        }

        matched_tool = None
        for var_key, (tool_name, keywords) in _VARIABLE_FAST_PATHS.items():
            if any(kw in _msg_lower for kw in keywords):
                matched_tool = (var_key, tool_name)
                break

        # ----------------------------------------------------------------
        # TREND DETECTION: Does the user ask about change over time?
        # e.g., "is rainfall increasing?", "monsoon trends", "peak changing"
        # If so, fetch TWO years (2030 + 2070) in parallel.
        # ----------------------------------------------------------------
        _TREND_KEYWORDS = [
            "increasing",
            "decreasing",
            "trend",
            "changing",
            "change over",
            "getting worse",
            "getting better",
            "intensif",
            "peak daily",
            "over time",
            "by 2050",
            "by 2070",
            "by 2100",
            "future",
            "will it get",
            "projected to",
            "expected to",
        ]
        is_trend_query = any(kw in _msg_lower for kw in _TREND_KEYWORDS)

        if matched_tool and not is_overview_query and not is_comparison_query:
            var_key, tool_name = matched_tool
            try:
                import geoint.extreme_weather_tools as _ewt
                from geoint.extreme_weather_agent import _reverse_geocode_cache

                tool_fn = getattr(_ewt, tool_name)

                # Extract scenario from query
                if (
                    "ssp245" in _msg_lower
                    or "ssp2-4.5" in _msg_lower
                    or "moderate" in _msg_lower
                ):
                    scenario = "ssp245"
                else:
                    scenario = "ssp585"

                # Extract year from query
                year_match = _re.search(r"\b(20[2-9]\d|2100)\b", message)
                target_year = int(year_match.group()) if year_match else 2030

                loop = asyncio.get_event_loop()

                # --------------------------------------------------------
                # TREND PATH: Fetch 2 years in parallel (2030 + 2070)
                # --------------------------------------------------------
                if is_trend_query:
                    trend_years = [2030, 2070]
                    if target_year not in trend_years and target_year != 2030:
                        trend_years[1] = target_year  # honour explicit year

                    logger.info(
                        f"[STORM] [{request_id}] FAST PATH TREND: Parallel {tool_name} for years {trend_years}"
                    )

                    trend_tasks = [
                        loop.run_in_executor(
                            None, tool_fn, latitude, longitude, scenario, yr
                        )
                        for yr in trend_years
                    ]

                    trend_results_raw = await asyncio.wait_for(
                        asyncio.gather(*trend_tasks, return_exceptions=True),
                        timeout=200.0,
                    )

                    tool_calls_out = []
                    all_data_parts = []
                    for i, (yr, raw) in enumerate(zip(trend_years, trend_results_raw)):
                        if isinstance(raw, Exception):
                            logger.warning(
                                f"[STORM] [{request_id}] Trend year {yr} failed: {raw}"
                            )
                            continue
                        td = json.loads(raw)
                        if "error" not in td:
                            tool_calls_out.append({"tool": tool_name, "result": td})
                            all_data_parts.append(f"--- Year {yr} ---\n{raw}")

                    fast_elapsed = time.time() - agent_start
                    logger.info(
                        f"[STORM] [{request_id}] FAST PATH TREND: Data for {len(all_data_parts)}/{len(trend_years)} years in {fast_elapsed:.1f}s"
                    )

                    if all_data_parts:
                        geo_key = f"{latitude:.4f}:{longitude:.4f}"
                        location_name = _reverse_geocode_cache.get(
                            geo_key, f"({latitude:.4f}, {longitude:.4f})"
                        )

                        from semantic_translator import get_llm_client

                        _fmt_client = get_llm_client()

                        scenario_desc = (
                            "SSP2-4.5 (moderate)"
                            if scenario == "ssp245"
                            else "SSP5-8.5 (worst-case)"
                        )
                        combined_data = "\n\n".join(all_data_parts)
                        fmt_resp = await _fmt_client.chat.completions.create(
                            messages=[
                                {
                                    "role": "system",
                                    "content": (
                                        "You are a climate analyst. The user asked about a climate trend. "
                                        "You have projection data for multiple years from NASA NEX-GDDP-CMIP6. "
                                        "Compare the data across years to answer whether the variable is "
                                        "increasing, decreasing, or stable. Include actual values with units. "
                                        "Conclude with a Summary section that gives a clear, direct answer to the user's specific question, "
                                        "grounded in the data (e.g., 'Yes, peak daily rainfall is projected to "
                                        "increase from X mm/day in 2030 to Y mm/day in 2070')."
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        f"Analyze {var_key} trend data for {location_name} "
                                        f"({scenario_desc}). "
                                        f"The user asked: '{user_query}'\n\n{combined_data}"
                                    ),
                                },
                            ],
                            max_completion_tokens=2000,
                            temperature=1.0,
                        )

                        formatted_response = fmt_resp.choices[0].message.content
                        total_elapsed = time.time() - agent_start
                        logger.info(
                            f"[STORM] [{request_id}] FAST PATH TREND COMPLETE in {total_elapsed:.1f}s"
                        )

                        return {
                            "status": "success",
                            "result": {
                                "analysis": formatted_response,
                                "tool_calls": tool_calls_out,
                                "message_count": 1,
                            },
                            "session_id": session_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }

                # --------------------------------------------------------
                # SINGLE-YEAR PATH (non-trend queries)
                # --------------------------------------------------------
                else:
                    logger.info(
                        f"[STORM] [{request_id}] FAST PATH VARIABLE: Direct {tool_name} call"
                    )
                    raw_result = await asyncio.wait_for(
                        loop.run_in_executor(
                            None, tool_fn, latitude, longitude, scenario, target_year
                        ),
                        timeout=200.0,
                    )

                    tool_data = json.loads(raw_result)
                    fast_elapsed = time.time() - agent_start
                    logger.info(
                        f"[STORM] [{request_id}] FAST PATH VARIABLE: {tool_name} data in {fast_elapsed:.1f}s"
                    )

                    if "error" not in tool_data:
                        geo_key = f"{latitude:.4f}:{longitude:.4f}"
                        location_name = _reverse_geocode_cache.get(
                            geo_key, f"({latitude:.4f}, {longitude:.4f})"
                        )

                        from semantic_translator import get_llm_client

                        _fmt_client = get_llm_client()

                        scenario_desc = (
                            "SSP2-4.5 (moderate)"
                            if scenario == "ssp245"
                            else "SSP5-8.5 (worst-case)"
                        )
                        fmt_resp = await _fmt_client.chat.completions.create(
                            messages=[
                                {
                                    "role": "system",
                                    "content": (
                                        "You are a climate analyst. Format the provided climate projection data "
                                        "into a clear, informative summary. Include all values with units. "
                                        "Address the user's specific question directly. "
                                        "Conclude with a Summary section that gives a clear, direct answer grounded in the data."
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        f"Format this {var_key} projection data for {location_name} "
                                        f"({scenario_desc}, {target_year}). "
                                        f"The user asked: '{user_query}'\n\nData:\n{raw_result}"
                                    ),
                                },
                            ],
                            max_completion_tokens=1500,
                            temperature=1.0,
                        )

                        formatted_response = fmt_resp.choices[0].message.content
                        total_elapsed = time.time() - agent_start
                        logger.info(
                            f"[STORM] [{request_id}] FAST PATH VARIABLE COMPLETE in {total_elapsed:.1f}s"
                        )

                        return {
                            "status": "success",
                            "result": {
                                "analysis": formatted_response,
                                "tool_calls": [
                                    {"tool": tool_name, "result": tool_data}
                                ],
                                "message_count": 1,
                            },
                            "session_id": session_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
            except asyncio.TimeoutError:
                logger.warning(
                    f"[STORM] [{request_id}] FAST PATH VARIABLE timed out after {time.time() - agent_start:.0f}s, falling back to agent"
                )
            except Exception as e:
                logger.warning(
                    f"[STORM] [{request_id}] FAST PATH VARIABLE failed ({e}), falling back to agent"
                )

        # ====================================================================
        # STANDARD PATH: Full Agent Service round-trip
        # ====================================================================

        try:
            # Compute remaining time budget: Container Apps ingress hard limit
            # is 240s.  Subtract elapsed time (fast paths may have consumed
            # some) and leave a 15s buffer for JSON serialization + response.
            elapsed_so_far = time.time() - agent_start
            remaining_budget = max(230.0 - elapsed_so_far, 60.0)  # at least 60s
            logger.info(
                f"[STORM] [{request_id}] STANDARD PATH: elapsed={elapsed_so_far:.0f}s, budget={remaining_budget:.0f}s"
            )

            result = await asyncio.wait_for(
                agent.chat(
                    session_id=session_id,
                    user_message=message,
                    latitude=latitude,
                    longitude=longitude,
                    screenshot_base64=screenshot,
                ),
                timeout=remaining_budget,
            )
        except asyncio.TimeoutError:
            agent_elapsed = time.time() - agent_start
            logger.error(
                f"[STORM] [{request_id}] [FAIL] AGENT TIMEOUT after {agent_elapsed:.1f}s"
            )
            raise HTTPException(
                status_code=504,
                detail=f"Climate analysis timed out after {agent_elapsed:.0f}s. The query may involve too many data lookups. Try a simpler question (e.g. 'What is the projected temperature for this location?').",
            )

        agent_elapsed = time.time() - agent_start
        logger.info(
            f"[STORM] [{request_id}] [OK] EXTREME WEATHER AGENT COMPLETED ({agent_elapsed:.2f}s)"
        )

        # Enhanced diagnostic logging
        tool_calls_list = result.get("tool_calls", [])
        response_text = result.get("response", "")
        logger.info(f"[STORM] [{request_id}] Tool calls made: {len(tool_calls_list)}")
        for i, tc in enumerate(tool_calls_list):
            tool_name = tc.get("tool", "unknown")
            tool_result = tc.get("result", {})
            if isinstance(tool_result, dict):
                has_error = "error" in tool_result or any(
                    "error" in str(v)
                    for v in tool_result.values()
                    if isinstance(v, (dict, str))
                )
                logger.info(
                    f"[STORM] [{request_id}]   Tool[{i}]: {tool_name} -> {'[FAIL] ERROR' if has_error else '[OK] OK'} (keys: {list(tool_result.keys())[:8]})"
                )
                if has_error:
                    logger.warning(
                        f"[STORM] [{request_id}]   Tool[{i}] error detail: {json.dumps(tool_result, default=str)[:500]}"
                    )
            else:
                logger.info(
                    f"[STORM] [{request_id}]   Tool[{i}]: {tool_name} -> {str(tool_result)[:200]}"
                )
        logger.info(
            f"[STORM] [{request_id}] Response preview ({len(response_text)} chars): {response_text[:300]}..."
        )

        return {
            "status": "success",
            "result": {
                "analysis": result.get("response", ""),
                "tool_calls": tool_calls_list,
                "message_count": result.get("message_count", 1),
            },
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.error(
            f"[STORM] [{request_id}] [FAIL] Extreme weather endpoint timed out"
        )
        raise HTTPException(status_code=504, detail="Climate analysis timed out")
    except Exception as e:
        logger.error(
            f"[STORM] [{request_id}] [FAIL] EXTREME WEATHER ENDPOINT FAILED: {e}"
        )
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Climate analysis failed")


@router.get("/api/geoint/cmip6-test")
async def cmip6_diagnostic_test(
    lat: float = 42.15,
    lng: float = -100.27,
    variable: str = "tasmax",
    scenario: str = "ssp585",
    year: int = 2030,
    aggregate: str = "last",
):
    """
    [MICRO] CMIP6 Diagnostic Test Endpoint

    Quick test of CMIP6 STAC search + NetCDF sampling without going through
    the full agent. Use this to verify:
    1. STAC search finds items
    2. Items have the requested variable as an asset
    3. NetCDF sampling returns valid data

    Example: /api/geoint/cmip6-test?lat=42.15&lng=-100.27&variable=tasmax
    Example: /api/geoint/cmip6-test?lat=18.47&lng=-66.10&variable=pr&aggregate=annual
    """
    import time

    from geoint.extreme_weather_tools import _sample_netcdf, _search_cmip6_items

    result = {"steps": [], "success": False}

    # Step 1: STAC Search
    t0 = time.time()
    try:
        items = _search_cmip6_items(lat, lng, variable, scenario, year, limit=3)
        elapsed = time.time() - t0
        result["steps"].append(
            {
                "step": "STAC Search",
                "status": "ok" if items else "no_items",
                "items_found": len(items),
                "item_ids": [it.get("id", "?") for it in items],
                "elapsed_s": round(elapsed, 2),
            }
        )
    except Exception as e:
        result["steps"].append(
            {"step": "STAC Search", "status": "error", "error": str(e)}
        )
        return result

    if not items:
        result["steps"].append(
            {"step": "NetCDF Sample", "status": "skipped", "reason": "No items found"}
        )
        return result

    # Step 2: NetCDF Sampling (first item)
    first_item = items[0]
    asset = first_item.get("assets", {}).get(variable, {})
    href = asset.get("href", "") if isinstance(asset, dict) else ""

    if not href:
        result["steps"].append(
            {
                "step": "NetCDF Sample",
                "status": "error",
                "error": f"No href for {variable} asset",
            }
        )
        return result

    t1 = time.time()
    try:
        sample = _sample_netcdf(href, variable, lat, lng, aggregate=aggregate)
        elapsed = time.time() - t1
        result["steps"].append(
            {
                "step": "NetCDF Sample",
                "status": "ok" if "error" not in sample else "error",
                "result": sample,
                "elapsed_s": round(elapsed, 2),
                "item_id": first_item.get("id", "?"),
                "href_prefix": href[:100] + "...",
            }
        )
        result["success"] = "error" not in sample
    except Exception as e:
        result["steps"].append(
            {"step": "NetCDF Sample", "status": "error", "error": str(e)}
        )

    return result
