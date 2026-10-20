"""GEOINT building-damage, comparison, animation and orchestration endpoints."""

import asyncio
import logging
import os
import traceback
from datetime import datetime, timezone

import app_state
from fastapi import APIRouter, HTTPException
from request_models import (
    AnimationRequest,
    BuildingDamageRequest,
    ComparisonRequest,
    OrchestrateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/geoint/building-damage")
async def geoint_building_damage_analysis(body: BuildingDamageRequest):
    """
    [BUILD] GEOINT Building Damage Assessment

    Structure damage assessment using GPT-5 Vision and satellite imagery.

    When a screenshot is provided (user has loaded data on the map), analysis is
    performed on the VISIBLE imagery—the loaded tiles—not independently-fetched
    Sentinel-2.  When no screenshot is available the agent falls back to its own
    satellite imagery retrieval via the Agent Service tools.

    Request body:
    {
        "latitude": float,
        "longitude": float,
        "user_query": str (optional - user's question),
        "user_context": str (optional - legacy alias for user_query),
        "screenshot": str (optional - base64 map screenshot),
        "radius_miles": float (optional, default 5.0)
    }
    """
    try:
        latitude = body.latitude
        longitude = body.longitude
        user_query = body.user_query or ""
        screenshot = body.screenshot
        radius_miles = body.radius_miles

        logger.info(f"Building Damage endpoint: ({latitude}, {longitude})")
        logger.info(
            f"Building Damage: screenshot={'yes' if screenshot else 'no'}, query='{user_query[:100]}'"
            if user_query
            else "Building Damage: no query"
        )

        import time

        agent_start = time.time()

        from geoint.extreme_weather_agent import _reverse_geocode_cache

        # Create vision client for GPT-5 screenshot analysis (local-first, multi-provider)
        from semantic_translator import get_llm_client

        _vision_client = get_llm_client(
            model=os.getenv("LLM_MODEL", "gpt-5"), vision=True
        )

        # ── PATH 1: Screenshot provided — analyze the loaded map imagery ──
        # When the user has loaded data (NAIP, Sentinel-2, Landsat, etc.) and
        # the frontend captured a screenshot, analyze THAT imagery directly via
        # GPT-5 Vision.  Do NOT fetch independent Sentinel-2 tiles.
        if screenshot:
            logger.info("Building Damage: Analyzing loaded map imagery via screenshot")

            # Reverse geocode for location context
            geo_key = f"{latitude:.4f}:{longitude:.4f}"
            location_name = _reverse_geocode_cache.get(
                geo_key, f"({latitude:.4f}, {longitude:.4f})"
            )

            clean_b64 = screenshot
            if clean_b64.startswith("data:image"):
                clean_b64 = clean_b64.split(",", 1)[1]

            prompt = (
                f"You are an expert GEOINT analyst specializing in building damage assessment from satellite and aerial imagery.\n\n"
                f"Location: {location_name} ({latitude:.6f}, {longitude:.6f})\n"
                f"Analysis radius: {radius_miles} miles\n\n"
                f"Analyze the provided satellite/aerial image for building and structural damage.\n\n"
                f"Assess:\n"
                f"1. **Location** — Identify the area shown\n"
                f"2. **Damage Assessment** — Visual observations, damage indicators, severity classification\n"
                f"   Use scale: No Damage | Minor Damage | Major Damage | Destroyed\n"
                f"   Look for: collapsed/missing roofs, debris fields, burn scars, water damage, structural deformation\n"
                f"3. **Infrastructure Impact** — Roads, bridges, utilities affected\n"
                f"4. **Recommendations** — Priority areas for response teams, suggested next steps\n\n"
                f"Keep analysis factual. Acknowledge resolution limitations. Do NOT fetch additional imagery — analyze only what is visible in this image.\n"
            )
            if user_query:
                prompt += f"\nUser question: {user_query}\n"

            try:
                vision_response = await _vision_client.chat.completions.create(
                    model=os.getenv("LLM_MODEL", "gpt-5"),
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a GEOINT Building Damage Assessment expert. Analyze the provided imagery and give structured damage assessments.",
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{clean_b64}",
                                        "detail": "high",
                                    },
                                },
                            ],
                        },
                    ],
                    temperature=1.0,
                    max_completion_tokens=3000,
                )
                response_text = vision_response.choices[0].message.content
                elapsed = time.time() - agent_start
                logger.info(
                    f"Building Damage: Screenshot analysis completed ({elapsed:.1f}s, {len(response_text)} chars)"
                )

                return {
                    "status": "success",
                    "result": {
                        "agent": "building_damage_vision",
                        "response": response_text,
                        "summary": response_text[:500],
                        "tool_calls": [{"tool": "gpt_vision_analysis"}],
                        "location": {"latitude": latitude, "longitude": longitude},
                        "radius_miles": radius_miles,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as vis_err:
                logger.warning(
                    f"Building Damage: Screenshot vision analysis failed: {vis_err}, falling back to Agent Service"
                )
                # Fall through to Agent Service path

        # ── PATH 2: No screenshot — use Agent Service to fetch and analyze imagery ──
        logger.info(
            "Building Damage: No screenshot, using Agent Service with tool-based imagery fetch"
        )
        analysis_result = None
        try:
            from geoint.agents import building_damage_agent

            analysis_result = await building_damage_agent(
                latitude=latitude,
                longitude=longitude,
                screenshot_base64=None,
                user_query=user_query,
                radius_miles=radius_miles,
            )

            # Check if agent returned an error (403 / PE lockdown / model errors)
            resp_text = str(analysis_result.get("response", ""))
            _error_patterns = [
                "403",
                "Public access is disabled",
                "private endpoint",
                "invalid_engine_error",
                "Failed to resolve model",
                "InternalServerError",
                "Unable to get resource",
                "DeploymentNotFound",
                "model_not_found",
            ]
            if any(pat.lower() in resp_text.lower() for pat in _error_patterns):
                logger.warning(
                    f"Building damage agent returned error, falling back to direct tools: {resp_text[:200]}"
                )
                analysis_result = None  # trigger fallback
        except Exception as agent_err:
            err_str = str(agent_err)
            logger.warning(
                f"Building damage agent failed ({err_str}), falling back to direct tools"
            )
            analysis_result = None

        # ── FALLBACK: Direct tool calls when Agent Service is blocked by PE ──
        if analysis_result is None:
            logger.info("Building Damage: Using direct tool fallback (PE lockdown)")

            # 1) Run the damage assessment tool directly (uses warm vision_analyzer)
            from geoint.building_damage_tools import (
                _assess_damage_async,
                _classify_severity_async,
            )

            assess_result = await _assess_damage_async(
                latitude, longitude, radius_miles
            )
            classify_result = await _classify_severity_async(latitude, longitude)

            # 2) If screenshot provided, analyze it with _vision_client
            visual_analysis = None
            if screenshot:
                try:
                    clean_b64 = screenshot
                    if clean_b64.startswith("data:image"):
                        clean_b64 = clean_b64.split(",", 1)[1]

                    vision_response = await _vision_client.chat.completions.create(
                        model=os.getenv("LLM_MODEL", "gpt-5"),
                        messages=[
                            {
                                "role": "system",
                                "content": "You are an expert in structural damage assessment from satellite imagery. Provide detailed, factual analysis.",
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"Analyze this satellite image at ({latitude:.4f}, {longitude:.4f}) for building damage. "
                                        f"Look for: collapsed structures, debris fields, burn scars, water damage, infrastructure impact. "
                                        f"Classify severity as: No Damage, Minor Damage, Major Damage, or Destroyed. "
                                        f"{'Additional context: ' + user_query if user_query else ''}",
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/jpeg;base64,{clean_b64}",
                                            "detail": "high",
                                        },
                                    },
                                ],
                            },
                        ],
                        temperature=1.0,
                        max_completion_tokens=2000,
                    )
                    visual_analysis = vision_response.choices[0].message.content
                    logger.info(
                        f"Building Damage: Screenshot analysis completed ({len(visual_analysis)} chars)"
                    )
                except Exception as vis_err:
                    logger.warning(
                        f"Building Damage: Screenshot analysis failed: {vis_err}"
                    )

            # 3) Combine results into agent-like response
            tool_assessment = assess_result.get("visual_assessment", "")
            severity_assessment = classify_result.get("visual_assessment", "")

            # Build a comprehensive response
            parts = []
            if visual_analysis:
                parts.append(
                    f"## Visual Analysis of Current Map View\n\n{visual_analysis}"
                )
            if tool_assessment:
                parts.append(f"## Satellite Imagery Assessment\n\n{tool_assessment}")
            if severity_assessment and severity_assessment != tool_assessment:
                parts.append(f"## Severity Classification\n\n{severity_assessment}")

            combined_response = (
                "\n\n".join(parts)
                if parts
                else "Building damage assessment completed. No significant damage indicators detected from available imagery."
            )

            analysis_result = {
                "agent": "building_damage_agent_fallback",
                "response": combined_response,
                "summary": combined_response[:500],
                "tool_calls": [
                    {"tool": "assess_building_damage"},
                    {"tool": "classify_damage_severity"},
                ],
                "location": {"latitude": latitude, "longitude": longitude},
                "radius_miles": radius_miles,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            logger.info("Building Damage: Direct fallback completed successfully")

        logger.info("Building damage agent completed")

        return {
            "status": "success",
            "result": analysis_result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.error("Building Damage endpoint timed out")
        raise HTTPException(
            status_code=504, detail="Building damage analysis timed out"
        )
    except Exception as e:
        logger.error(f"Building Damage endpoint failed: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Building damage analysis failed")


@router.post("/api/geoint/comparison")
async def geoint_comparison_analysis(body: ComparisonRequest):
    """
    GEOINT Comparison Analysis - Temporal change detection endpoint

    Supports TWO modes:

    1. QUERY MODE (new): Just provide user_query, we parse location + dates
       {
           "user_query": "How did Miami Beach surface reflectance change between 01/2020 and 01/2025?",
           "latitude": float (optional fallback),
           "longitude": float (optional fallback)
       }
       Returns: before/after tiles for map toggle + analysis summary

    2. SCREENSHOT MODE (legacy): Provide pre-captured screenshots + dates
       {
           "latitude": float,
           "longitude": float,
           "before_date": str,
           "after_date": str,
           ...
       }
    """
    try:
        user_query = body.user_query
        latitude = body.latitude
        longitude = body.longitude
        before_date = body.before_date
        after_date = body.after_date
        screenshot = body.screenshot

        mode = "query" if user_query and not (before_date and after_date) else "direct"
        logger.info(
            f"[CHART] [COMPARISON-ANALYSIS] mode={mode} query='{user_query}' before={before_date} after={after_date}"
        )

        # ================================================================
        # MODE 1: QUERY-BASED COMPARISON (new flow)
        # User provides natural language query, we parse and execute
        # ================================================================
        if user_query and not (before_date and after_date):
            logger.info(
                "[SEARCH] QUERY MODE: Parsing query for comparison parameters..."
            )
            logger.info(f"[MSG] User query: {user_query}")

            try:
                from geoint.comparison_agent import get_comparison_agent

                comparison_agent = get_comparison_agent()

                result = await comparison_agent.handle_query(
                    user_query=user_query,
                    latitude=latitude,
                    longitude=longitude,
                    session_id=body.session_id,
                    screenshot_base64=screenshot,
                )

                logger.info(
                    f"[OK] Comparison agent returned: status={result.get('status')}"
                )

                # If it's a prompt response, return it directly
                if result.get("status") == "prompt":
                    return {
                        "status": "success",
                        "type": "prompt",
                        "message": result.get("message"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }

                # If it's an error, return with error status
                if result.get("status") == "error":
                    return {
                        "status": "error",
                        "message": result.get("message"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }

                # Success - return comparison result with before/after data
                return {
                    "status": "success",
                    "type": "comparison",
                    "result": result,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as e:
                logger.warning(
                    f"[WARN] Comparison agent failed: {e}, falling back to direct STAC approach"
                )
                # --------------------------------------------------------
                # FALLBACK: Use process-comparison-query logic + direct
                # STAC search instead of the Agent Service comparison agent.
                # This avoids the separate AgentsClient that may fail when
                # --------------------------------------------------------
                try:
                    logger.info(
                        "[SEARCH] FALLBACK: Using direct STAC comparison approach"
                    )
                    # Step 1: Parse query with existing agents
                    if not app_state.semantic_translator:
                        raise RuntimeError("Semantic translator not initialized")

                    collections = (
                        await app_state.semantic_translator.collection_mapping_agent(
                            user_query
                        )
                    )
                    stac_query = (
                        await app_state.semantic_translator.build_stac_query_agent(
                            user_query, collections or []
                        )
                    )
                    bbox = stac_query.get("bbox")
                    location_name = stac_query.get("location_name", "Unknown location")

                    if not bbox:
                        raise RuntimeError(
                            f"Could not resolve location from query: '{user_query}'"
                        )

                    datetime_result = (
                        await app_state.semantic_translator.datetime_translation_agent(
                            query=user_query,
                            collections=collections or [],
                            mode="comparison",
                        )
                    )
                    if (
                        not datetime_result
                        or "before" not in datetime_result
                        or "after" not in datetime_result
                    ):
                        raise RuntimeError(
                            "Could not extract before/after timeframes from query"
                        )

                    before_dt = datetime_result["before"]
                    after_dt = datetime_result["after"]

                    # Step 2: Determine collection + asset
                    from geoint.comparison_tools import (
                        _format_date_display,
                        _get_scene_date,
                    )

                    # Pick primary collection from parsed collections
                    primary_collection = None
                    if collections:
                        priority = [
                            "modis-14A2-061",
                            "modis-14A1-061",
                            "sentinel-2-l2a",
                            "landsat-c2-l2",
                            "hls2-l30",
                            "sentinel-1-rtc",
                        ]
                        for pc in priority:
                            if pc in collections:
                                primary_collection = pc
                                break
                        if not primary_collection:
                            primary_collection = collections[0]
                    else:
                        primary_collection = "sentinel-2-l2a"

                    # Step 3: Execute dual STAC searches
                    import asyncio as _asyncio

                    from geoint.comparison_tools import _execute_stac_search

                    before_result, after_result = await _asyncio.gather(
                        _execute_stac_search(
                            primary_collection, bbox, before_dt, limit=3
                        ),
                        _execute_stac_search(
                            primary_collection, bbox, after_dt, limit=3
                        ),
                    )

                    before_features = before_result.get("features", [])
                    after_features = after_result.get("features", [])

                    if not before_features and not after_features:
                        return {
                            "status": "error",
                            "message": f"No imagery found for {location_name} in either time period ({before_dt} / {after_dt}).",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }

                    center_lng = (bbox[0] + bbox[2]) / 2
                    center_lat = (bbox[1] + bbox[3]) / 2

                    fallback_result = {
                        "status": "success",
                        "type": "comparison",
                        "location": location_name,
                        "analysis_type": "surface reflectance",
                        "bbox": bbox,
                        "center": {"lat": center_lat, "lng": center_lng},
                        "before": {
                            "datetime": before_dt,
                            "datetime_display": _format_date_display(before_dt),
                            "features_count": len(before_features),
                            "tile_urls": before_result.get("tile_urls", []),
                            "best_scene_date": (
                                _get_scene_date(before_features[0])
                                if before_features
                                else None
                            ),
                        },
                        "after": {
                            "datetime": after_dt,
                            "datetime_display": _format_date_display(after_dt),
                            "features_count": len(after_features),
                            "tile_urls": after_result.get("tile_urls", []),
                            "best_scene_date": (
                                _get_scene_date(after_features[0])
                                if after_features
                                else None
                            ),
                        },
                        "collection": primary_collection,
                        "analysis": f"**Comparison: {location_name}**\n\n"
                        f"**Before:** {_format_date_display(before_dt)} ({len(before_features)} scenes)\n"
                        f"**After:** {_format_date_display(after_dt)} ({len(after_features)} scenes)\n\n"
                        f"Use the **BEFORE/AFTER** toggle buttons on the map to switch between time periods.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }

                    logger.info(
                        f"[OK] FALLBACK comparison succeeded: {location_name}, {primary_collection}, before={len(before_features)} after={len(after_features)} scenes"
                    )
                    return {
                        "status": "success",
                        "type": "comparison",
                        "result": fallback_result,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                except Exception as fallback_err:
                    logger.error(
                        f"[FAIL] Fallback comparison also failed: {fallback_err}",
                        exc_info=True,
                    )
                    return {
                        "status": "error",
                        "message": f"Comparison query processing failed: {str(e)}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }

        # ================================================================
        # MODE 2: SCREENSHOT-BASED COMPARISON (legacy flow)
        # Requires pre-parsed dates and optional screenshots
        # ================================================================
        logger.info("[SNAP] SCREENSHOT MODE: Using provided dates and screenshots...")

        before_screenshot = body.get("before_screenshot")
        after_screenshot = body.get("after_screenshot")
        before_metadata = body.get("before_metadata")
        after_metadata = body.get("after_metadata")
        comparison_aspect = body.get("comparison_aspect")
        collection_id = body.get("collection_id")
        download_rasters = body.get("download_rasters", True)

        logger.info(f"[PIN] Location: ({latitude}, {longitude})")
        logger.info(f"[DATE] Before Date: {before_date}")
        logger.info(f"[DATE] After Date: {after_date}")
        logger.info(
            f"[SNAP] Before Screenshot: {'Provided' if before_screenshot else 'None'} ({len(before_screenshot) if before_screenshot else 0} chars)"
        )
        logger.info(
            f"[SNAP] After Screenshot: {'Provided' if after_screenshot else 'None'} ({len(after_screenshot) if after_screenshot else 0} chars)"
        )
        logger.info(
            f"[CHART] Before Metadata: {'Provided' if before_metadata else 'None'}"
        )
        logger.info(
            f"[CHART] After Metadata: {'Provided' if after_metadata else 'None'}"
        )
        logger.info(f"[MSG] User Query: {user_query}")
        logger.info(f"[TARGET] Comparison Aspect: {comparison_aspect}")
        logger.info(f"[LIST] Collection ID: {collection_id}")
        logger.info(f"⬇️  Download Rasters: {download_rasters}")

        # Validation
        logger.info("[SEARCH] Validating required parameters...")

        if latitude is None or longitude is None or not before_date or not after_date:
            logger.error("[FAIL] Missing required parameters")
            logger.error(f"   latitude: {latitude}")
            logger.error(f"   longitude: {longitude}")
            logger.error(f"   before_date: {before_date}")
            logger.error(f"   after_date: {after_date}")
            raise HTTPException(
                status_code=400,
                detail="Missing required parameters: latitude, longitude, before_date, after_date",
            )

        if not (-90 <= latitude <= 90):
            logger.error(f"[FAIL] Invalid latitude: {latitude}")
            raise HTTPException(status_code=400, detail=f"Invalid latitude: {latitude}")

        if not (-180 <= longitude <= 180):
            logger.error(f"[FAIL] Invalid longitude: {longitude}")
            raise HTTPException(
                status_code=400, detail=f"Invalid longitude: {longitude}"
            )

        # Screenshots are optional if raster download is enabled with collection_id
        has_screenshots = before_screenshot and after_screenshot
        can_download_rasters = download_rasters and collection_id

        logger.info(f"[OK] Has screenshots: {has_screenshots}")
        logger.info(f"[OK] Can download rasters: {can_download_rasters}")

        if not has_screenshots and not can_download_rasters:
            logger.error("[FAIL] Neither screenshots nor raster data source provided")
            raise HTTPException(
                status_code=400,
                detail="Either screenshots (before_screenshot + after_screenshot) OR raster data (collection_id + download_rasters=true) must be provided",
            )

        logger.info("[OK] All validation checks passed")
        logger.info("")
        logger.info("[BOT] Calling comparison_analysis_agent...")
        logger.info("   Parameters:")
        logger.info(f"     - latitude: {latitude}")
        logger.info(f"     - longitude: {longitude}")
        logger.info(f"     - before_date: {before_date}")
        logger.info(f"     - after_date: {after_date}")
        logger.info(
            f"     - before_screenshot_base64: {'Provided' if before_screenshot else 'None'}"
        )
        logger.info(
            f"     - after_screenshot_base64: {'Provided' if after_screenshot else 'None'}"
        )
        logger.info(f"     - collection_id: {collection_id}")
        logger.info(f"     - download_rasters: {download_rasters}")

        # Call comparison_analysis_agent
        from geoint.agents import comparison_analysis_agent

        analysis_result = await comparison_analysis_agent(
            latitude=latitude,
            longitude=longitude,
            before_date=before_date,
            after_date=after_date,
            before_screenshot_base64=before_screenshot,
            after_screenshot_base64=after_screenshot,
            before_metadata=before_metadata,
            after_metadata=after_metadata,
            user_query=user_query,
            comparison_aspect=comparison_aspect,
            collection_id=collection_id,  # NEW
            download_rasters=download_rasters,  # NEW
        )

        text_len = (
            len(analysis_result.get("text", ""))
            if isinstance(analysis_result, dict)
            else 0
        )
        logger.info(f"[CHART] [COMPARISON-ANALYSIS] [OK] Done — text={text_len} chars")

        return {
            "status": "success",
            "result": analysis_result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CHART] [COMPARISON-ANALYSIS] [FAIL] {type(e).__name__}: {e}")
        logger.exception("Full traceback:")
        raise HTTPException(status_code=500, detail="Comparison failed")


@router.post("/api/geoint/animation")
async def geoint_animation_analysis(body: AnimationRequest):
    """
    GEOINT Animation Generation - Thin wrapper around animation_generation_agent
    """
    try:
        latitude = body.latitude
        longitude = body.longitude
        start_date = body.start_date
        end_date = body.end_date
        collection_id = body.collection_id
        user_query = body.user_query

        logger.info(f"Animation endpoint: ({latitude}, {longitude})")

        # Call animation_generation_agent (new agent-based architecture)
        from geoint.agents import animation_generation_agent

        analysis_result = await animation_generation_agent(
            latitude=latitude,
            longitude=longitude,
            start_date=start_date,
            end_date=end_date,
            collection_id=collection_id,
            user_query=user_query,
        )

        logger.info("Animation agent completed")

        return {
            "status": "success",
            "result": analysis_result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Animation endpoint failed: {e}")
        raise HTTPException(status_code=500, detail="Animation failed")


@router.post("/api/geoint/orchestrate")
async def geoint_orchestrator_endpoint(body: OrchestrateRequest):
    """
    GEOINT Orchestrator - Calls multiple agents in parallel

    Request body:
    {
        "latitude": float,
        "longitude": float,
        "modules": ["terrain", "mobility", ...],
        "screenshot": str (optional),
        "user_query": str (optional),
        "radius_miles": float (optional)
    }
    """
    try:
        latitude = body.latitude
        longitude = body.longitude
        modules = body.modules
        screenshot = body.screenshot
        user_query = body.user_query
        radius_miles = body.radius_miles

        if not modules:
            raise HTTPException(status_code=400, detail="No modules specified")

        logger.info(
            f"Orchestrator endpoint: ({latitude}, {longitude}), modules={modules}"
        )

        # Call geoint_orchestrator (new agent-based architecture)
        from geoint.agents import geoint_orchestrator

        result = await geoint_orchestrator(
            latitude=latitude,
            longitude=longitude,
            modules=modules,
            screenshot_base64=screenshot,
            user_query=user_query,
            radius_miles=radius_miles,
            **body.model_dump(
                exclude={
                    "latitude",
                    "longitude",
                    "modules",
                    "screenshot",
                    "user_query",
                    "radius_miles",
                },
                exclude_none=True,
            ),
        )

        logger.info("Orchestrator completed")

        return {
            "status": "success",
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Orchestrator failed: {e}")
        raise HTTPException(status_code=500, detail="Orchestration failed")
