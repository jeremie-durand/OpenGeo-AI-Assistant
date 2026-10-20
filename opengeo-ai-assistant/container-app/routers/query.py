"""Unified natural-language query endpoint and comparison query endpoint."""

import asyncio
import logging
import traceback
from datetime import datetime, timezone

import aiohttp
import app_state
from fastapi import APIRouter, HTTPException
from quickstart_cache import (
    is_quickstart_query,
)
from request_models import ComparisonQueryRequest, QueryRequest
from services.collection_detection import detect_collections
from services.pipeline_trace import (
    get_pipeline_trace,
    log_pipeline_step,
)
from services.query_pipeline.intake import (
    apply_session_bbox_override,
    start_query,
)
from services.query_pipeline.intents import run_early_intents
from services.query_pipeline.quickstart import run_quickstart_fast_path
from services.query_pipeline.response import summarize_stac_items
from services.query_pipeline.routing import (
    classify_router_action,
    route_with_agent,
)
from services.query_pipeline.state import QueryState
from services.query_pipeline.tiles import (
    has_numeric_bbox,
    needs_optical_mosaic,
    optimize_tilejson_url,
    union_of_bboxes,
)
from services.query_pipeline.translation import translate_to_stac
from services.query_responses import (
    generate_contextual_empty_response,
    generate_fallback_response,
)
from services.stac_search import (
    execute_direct_stac_search,
    try_alternative_queries,
)
from services.tile_urls import build_tile_url_params, clean_tilejson_urls

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/query")
async def unified_query_processor(body: QueryRequest):
    """
    Unified query processor that combines Router Function logic with direct STAC search
    This implements the complete OpenGeo AI Assistant query processing pipeline
    """
    try:
        state = QueryState()
        start_query(body, state)

        if is_quickstart_query(state.natural_query):
            return await run_quickstart_fast_path(state)

        apply_session_bbox_override(state)

        # Continue with regular STAC search flow
        state.router_action = None

        early_response = await route_with_agent(state)
        if early_response is not None:
            return early_response

        # PHASE 0: EARLY QUERY INTENT CLASSIFICATION
        state.classification = None
        early_contextual_response = None
        state.translator = None
        state.vision_task = None  # For parallel processing of hybrid queries

        await classify_router_action(state)

        state.intent_type = (
            state.classification.get("intent_type", "stac")
            if state.classification
            else None
        )
        logger.info(
            f"[SEARCH] SEMANTIC_KERNEL_AVAILABLE: {app_state.SEMANTIC_KERNEL_AVAILABLE}"
        )
        if app_state.SEMANTIC_KERNEL_AVAILABLE and app_state.global_translator:
            early_response = await run_early_intents(state)
            if early_response is not None:
                return early_response

        # PHASE 1: SEMANTIC TRANSLATION TO STAC PARAMETERS
        early_response = await translate_to_stac(state)
        if early_response is not None:
            return early_response

        # PHASE 2: EXECUTE STAC SEARCH (if we have a valid query)
        state.stac_response = {"results": {"type": "FeatureCollection", "features": []}}
        state.features = []

        # [CHART] DIAGNOSTICS: Track filtering stages for helpful error messages
        state.search_diagnostics = {
            "raw_count": 0,
            "spatial_filtered_count": 0,
            "final_count": 0,
            "stac_query": state.stac_query,
            "failure_stage": "unknown",
        }

        # Note: pipeline_session_id already set at start of query processing

        if state.stac_query:
            try:
                logger.info("[WEB] Executing STAC search...")

                # [SEARCH] PIPELINE LOG: STAC Search Input
                log_pipeline_step(
                    state.pipeline_session_id,
                    "STAC_SEARCH",
                    "INPUT",
                    {
                        "collections": state.stac_query.get("collections"),
                        "bbox": state.stac_query.get("bbox"),
                        "datetime": state.stac_query.get("datetime"),
                        "filter": state.stac_query.get("filter"),
                    },
                )

                # Determine which STAC endpoint to use (intelligent routing from Router Function App)
                state.stac_endpoint = (
                    state.translator.determine_stac_source(
                        state.natural_query, state.stac_params
                    )
                    if state.translator
                    else "planetary_computer"
                )
                logger.info(f"[SIGNAL] STAC source determined: {state.stac_endpoint}")

                # Pass original_query for smart deduplication (removes duplicate grid cells)
                state.stac_response = await execute_direct_stac_search(
                    state.stac_query,
                    state.stac_endpoint,
                    original_query=state.natural_query,
                )

                # If local STAC returned nothing, fall back to Planetary Computer.
                # Always substitute PC-compatible collection IDs in case local STAC used
                # non-standard names (e.g. "sentinel2_eo_products" instead of "sentinel-2-l2a").
                if state.stac_endpoint != "planetary_computer":
                    local_features = state.stac_response.get("results", {}).get(
                        "features", []
                    )
                    if not local_features:
                        logger.info(
                            "[SIGNAL] Local STAC returned 0 features — retrying against Planetary Computer"
                        )
                        pc_collections = detect_collections(state.natural_query)
                        pc_stac_query = {
                            **state.stac_query,
                            "collections": pc_collections,
                        }
                        state.stac_response = await execute_direct_stac_search(
                            pc_stac_query,
                            "planetary_computer",
                            original_query=state.natural_query,
                        )
                        state.stac_query = pc_stac_query

                if state.stac_response.get("success"):
                    raw_features = state.stac_response.get("results", {}).get(
                        "features", []
                    )
                    state.search_diagnostics["raw_count"] = len(raw_features)
                    logger.info(
                        f"[OK] STAC search completed: {len(raw_features)} raw features found"
                    )

                    # [SEARCH] PIPELINE LOG: STAC Search Output
                    log_pipeline_step(
                        state.pipeline_session_id,
                        "STAC_SEARCH",
                        "OUTPUT",
                        {
                            "feature_count": len(raw_features),
                            "first_feature_id": (
                                raw_features[0].get("id") if raw_features else None
                            ),
                            "first_feature_bbox": (
                                raw_features[0].get("bbox") if raw_features else None
                            ),
                        },
                    )

                    # [SEARCH] MODIS BBOX FILTERING: MODIS STAC API returns tiles outside requested bbox
                    # MODIS uses sinusoidal grid (h/v tiles), so bbox filter isn't strict in PC STAC API
                    # Filter out tiles whose center is outside requested bbox to prevent showing
                    # Arizona/Texas tiles when user requested California
                    collections = state.stac_query.get("collections", [])
                    is_modis = any(col.startswith("modis-") for col in collections)
                    requested_bbox = state.stac_query.get("bbox")

                    if is_modis and requested_bbox and raw_features:
                        logger.info(
                            f"[MAP]  MODIS BBOX FILTER: Checking {len(raw_features)} tiles against requested bbox"
                        )
                        logger.info(
                            f"   Requested bbox: [{requested_bbox[0]:.2f}, {requested_bbox[1]:.2f}, {requested_bbox[2]:.2f}, {requested_bbox[3]:.2f}]"
                        )

                        filtered_features = []
                        req_west, req_south, req_east, req_north = requested_bbox

                        for feature in raw_features:
                            tile_bbox = feature.get("bbox")
                            if not tile_bbox or len(tile_bbox) < 4:
                                continue

                            # Calculate tile center
                            tile_west, tile_south, tile_east, tile_north = tile_bbox[:4]
                            tile_center_lon = (tile_west + tile_east) / 2
                            tile_center_lat = (tile_south + tile_north) / 2

                            # Check if tile center is within requested bbox
                            if (
                                req_west <= tile_center_lon <= req_east
                                and req_south <= tile_center_lat <= req_north
                            ):
                                filtered_features.append(feature)
                            else:
                                logger.debug(
                                    f"   [FAIL] Filtered out tile with center ({tile_center_lon:.2f}, {tile_center_lat:.2f}) outside bbox"
                                )

                        logger.info(
                            f"[OK] MODIS BBOX FILTER: Kept {len(filtered_features)}/{len(raw_features)} tiles within requested bbox"
                        )
                        raw_features = filtered_features
                        state.search_diagnostics["raw_count"] = len(raw_features)
                        state.search_diagnostics["modis_bbox_filtered"] = True

                    # [SEARCH] NO SPATIAL PRE-FILTERING: Agent 3 handles spatial coverage intelligently
                    # Agent 3's GPT-5 ensures full coverage by:
                    # 1. Understanding tile mosaicking and grid systems
                    # 2. Ensuring "NO GAPS in spatial coverage" (explicit in GPT prompt)
                    # 3. Using context-aware tile limits (5-50 based on AOI size)
                    # Pre-filtering with geometric overlap could remove tiles Agent 3 needs for seamless coverage
                    if (
                        app_state.SEMANTIC_KERNEL_AVAILABLE
                        and state.translator
                        and raw_features
                        and state.stac_query.get("bbox")
                    ):
                        requested_bbox = state.stac_query.get("bbox")

                        # Pass ALL tiles to Agent 3 for intelligent selection
                        spatially_filtered_features = raw_features
                        state.search_diagnostics["spatial_filter_skipped"] = True
                        state.search_diagnostics["spatial_filtered_count"] = len(
                            raw_features
                        )
                        logger.info(
                            f"[SKIP] Skipping spatial pre-filter: Agent 3 will ensure coverage from {len(raw_features)} tiles"
                        )

                        # [CLOUD] CLIENT-SIDE CLOUD COVER FILTERING: Safety net in case STAC API ignored the filter
                        # Extract cloud cover limit from stac_params (LLM-extracted), then query filter, then keywords
                        cloud_cover_limit = None

                        # Primary source: LLM-extracted cloud_cover from translate_query
                        if (
                            state.stac_params
                            and state.stac_params.get("cloud_cover") is not None
                        ):
                            cloud_cover_limit = int(state.stac_params["cloud_cover"])
                            logger.info(
                                f"[CLOUD] Cloud cover limit from stac_params: {cloud_cover_limit}%"
                            )
                        else:
                            # Secondary: check STAC query filter (eo:cloud_cover / cloud_cover CQL2 extension)
                            query_filter = state.stac_query.get("query", {})
                            logger.info(
                                f"[SEARCH] DEBUG: Client-side check - stac_query.get('query') = {query_filter}"
                            )
                            for prop_name in ["eo:cloud_cover", "cloud_cover"]:
                                if prop_name in query_filter:
                                    cloud_cover_filter = query_filter[prop_name]
                                    logger.info(
                                        f"[SEARCH] DEBUG: Found {prop_name} in query_filter: {cloud_cover_filter}"
                                    )
                                    if "lte" in cloud_cover_filter:
                                        cloud_cover_limit = cloud_cover_filter["lte"]
                                    elif "lt" in cloud_cover_filter:
                                        cloud_cover_limit = cloud_cover_filter["lt"]
                                    break
                                else:
                                    logger.info(
                                        f"[SEARCH] DEBUG: {prop_name} NOT in query_filter"
                                    )

                        # [TOOL] FALLBACK: Keyword detection if no cloud filter in query parameters
                        # This catches cases where the cloud filter wasn't propagated through the pipeline
                        if cloud_cover_limit is None and state.natural_query:
                            query_lower = state.natural_query.lower()
                            low_cloud_keywords = [
                                "low cloud",
                                "no cloud",
                                "clear",
                                "cloudless",
                                "minimal cloud",
                                "cloud-free",
                                "without clouds",
                                "clear sky",
                                "clear skies",
                                "no clouds",
                            ]
                            if any(kw in query_lower for kw in low_cloud_keywords):
                                cloud_cover_limit = 25
                                logger.info(
                                    f"[TOOL] FALLBACK: Detected low cloud keywords in query, applying {cloud_cover_limit}% filter"
                                )

                        logger.info(
                            f"[SEARCH] DEBUG: Final cloud_cover_limit = {cloud_cover_limit}"
                        )

                        # Get collection ID for property name lookup
                        collections = state.stac_query.get("collections", [])
                        primary_collection = collections[0] if collections else None

                        # Apply client-side cloud cover filtering with PROGRESSIVE RELAXATION
                        # If strict threshold returns 0, progressively relax to give users SOME results
                        if cloud_cover_limit is not None:
                            original_cloud_limit = cloud_cover_limit
                            relaxation_thresholds = [
                                cloud_cover_limit,
                                50,
                                75,
                                100,
                            ]  # Progressive relaxation

                            for threshold in relaxation_thresholds:
                                cloud_filtered_results = state.translator._filter_stac_results_by_cloud_cover(
                                    {"features": spatially_filtered_features},
                                    max_cloud_cover=threshold,
                                    collection_id=primary_collection,
                                )
                                filtered_features = cloud_filtered_results.get(
                                    "features", []
                                )

                                if filtered_features or threshold == 100:
                                    spatially_filtered_features = filtered_features
                                    state.search_diagnostics["cloud_filtered_count"] = (
                                        len(filtered_features)
                                    )

                                    if threshold > original_cloud_limit:
                                        logger.info(
                                            f"[CLOUD] Cloud filter relaxed: {original_cloud_limit}% -> {threshold}% (found {len(filtered_features)} tiles)"
                                        )
                                        state.search_diagnostics["cloud_relaxation"] = (
                                            f"{original_cloud_limit}% -> {threshold}%"
                                        )
                                    else:
                                        logger.info(
                                            f"[CLOUD] After cloud cover filtering (≤{threshold}%): {len(filtered_features)} features kept"
                                        )
                                    break

                        # [TARGET] TILE SELECTION: Use STAC API ordering (sorted by datetime desc)
                        # When no datetime is specified, STAC API returns results sorted by most recent first
                        # This is more efficient than GPT-5 selection and respects user's implicit "latest" intent
                        logger.info(
                            "[LIST] Using STAC API ordering (results already sorted by datetime desc)"
                        )

                        # Use all spatially filtered features (already ordered by recency from STAC API)
                        state.features = spatially_filtered_features
                        state.search_diagnostics["final_count"] = len(state.features)
                        state.search_diagnostics["tile_selection_method"] = (
                            "stac_api_ordering"
                        )
                        logger.info(
                            f"[OK] Using {len(state.features)} tiles from STAC API (pre-sorted by datetime desc)"
                        )

                        # Update stac_response with filtered results
                        state.stac_response["results"]["features"] = state.features
                    else:
                        state.features = raw_features
                        state.search_diagnostics["spatial_filtered_count"] = len(
                            raw_features
                        )
                        state.search_diagnostics["final_count"] = len(state.features)
                        logger.info(
                            "[WARN] Skipping spatial filtering and tile ranking (no translator or bbox)"
                        )

                    # Determine failure stage if no results
                    if state.search_diagnostics["final_count"] == 0:
                        if state.search_diagnostics["raw_count"] == 0:
                            state.search_diagnostics["failure_stage"] = "stac_api"
                        elif state.search_diagnostics["spatial_filtered_count"] == 0:
                            state.search_diagnostics["failure_stage"] = "spatial_filter"
                        else:
                            state.search_diagnostics["failure_stage"] = "tile_selection"

                        # [NEW] TRY ALTERNATIVE QUERIES: Automatically find available alternatives
                        logger.info(
                            "[SYNC] No results found - attempting to find alternatives with relaxed filters..."
                        )
                        logger.info(
                            f"[SYNC] STAC query datetime: {state.stac_query.get('datetime')}, collections: {state.stac_query.get('collections')}, bbox: {state.stac_query.get('bbox')}"
                        )
                        try:
                            alternative_result = await try_alternative_queries(
                                state.natural_query,
                                state.stac_query,
                                state.stac_params,
                                state.translator,
                                state.stac_endpoint,
                                requested_bbox,
                            )
                            logger.info(
                                f"[SYNC] Alternative result: success={alternative_result.get('success')}, features={len(alternative_result.get('features', []))}"
                            )
                        except Exception as alt_ex:
                            logger.error(
                                f"[SYNC] EXCEPTION in try_alternative_queries: {type(alt_ex).__name__}: {alt_ex}"
                            )
                            logger.error(f"[SYNC] Traceback: {traceback.format_exc()}")
                            alternative_result = {"success": False, "features": []}

                        if alternative_result.get("success") and alternative_result.get(
                            "features"
                        ):
                            # Successfully found alternatives!
                            state.features = alternative_result["features"]
                            state.search_diagnostics["final_count"] = len(
                                state.features
                            )
                            state.search_diagnostics["alternative_used"] = True
                            state.search_diagnostics["relaxation_applied"] = (
                                alternative_result.get("relaxation_applied")
                            )
                            state.search_diagnostics["alternative_explanation"] = (
                                alternative_result.get("explanation")
                            )
                            state.search_diagnostics["original_filters"] = (
                                alternative_result.get("original_filters")
                            )
                            state.search_diagnostics["alternative_filters"] = (
                                alternative_result.get("alternative_filters")
                            )

                            # Update stac_response with alternative features
                            state.stac_response["results"]["features"] = state.features

                            logger.info(
                                f"[OK] Found {len(state.features)} alternative results: {alternative_result.get('explanation')}"
                            )
                        else:
                            logger.info(
                                "[FAIL] No alternatives found - will generate empty result response"
                            )
                else:
                    logger.error(
                        f"[FAIL] STAC search failed: {state.stac_response.get('error', 'Unknown error')}"
                    )
                    state.search_diagnostics["failure_stage"] = "stac_error"

            except Exception as e:
                logger.error(f"[FAIL] STAC search execution error: {e}")
                state.stac_response = {
                    "results": {"type": "FeatureCollection", "features": []}
                }
                state.search_diagnostics["failure_stage"] = "exception"

        # PHASE 3: GENERATE RESPONSE MESSAGE
        # [SEARCH] DEBUG: Log state before response generation to diagnose "no matches" bug
        logger.info("[SEARCH] RESPONSE GENERATION DEBUG:")
        logger.info(
            f"   - features count: {len(state.features) if state.features else 0}"
        )
        logger.info(
            f"   - stac_response features: {len(state.stac_response.get('results', {}).get('features', []))}"
        )
        logger.info(
            f"   - SEMANTIC_KERNEL_AVAILABLE: {app_state.SEMANTIC_KERNEL_AVAILABLE}"
        )
        logger.info(f"   - translator: {state.translator is not None}")
        logger.info(f"   - stac_query: {state.stac_query is not None}")

        # [TOOL] SAFETY NET: Ensure features variable is synchronized with stac_response
        # This fixes a bug where features could be empty but stac_response has data
        stac_response_features = state.stac_response.get("results", {}).get(
            "features", []
        )
        if not state.features and stac_response_features:
            logger.warning(
                f"[WARN] MISMATCH DETECTED: features is empty but stac_response has {len(stac_response_features)} features!"
            )
            logger.warning(
                "   Synchronizing features from stac_response to prevent 'no matches' bug"
            )
            state.features = stac_response_features
            state.search_diagnostics["final_count"] = len(state.features)

        if app_state.SEMANTIC_KERNEL_AVAILABLE and state.translator and state.features:
            try:
                logger.info("[NOTE] Generating contextual response message...")

                # ========================================================================
                # [LAUNCH] AWAIT PARALLEL VISION TASK (if hybrid query)
                # ========================================================================
                # If we started a vision task in parallel with STAC search,
                # now is the time to wait for it to complete before combining results
                # ========================================================================
                vision_result = None
                if state.vision_task is not None:
                    try:
                        logger.info(
                            "[WAIT] Awaiting parallel vision analysis to complete..."
                        )
                        vision_result = await state.vision_task
                        if vision_result and vision_result.get("analysis"):
                            early_contextual_response = vision_result["analysis"]
                            logger.info(
                                "[OK] Parallel vision analysis completed and ready for combination"
                            )
                        else:
                            logger.warning(
                                "[WARN] Vision task completed but no analysis returned"
                            )
                    except Exception as e:
                        logger.error(f"[FAIL] Parallel vision task failed: {e}")
                        early_contextual_response = None

                # Check if we're showing alternative results
                if state.search_diagnostics.get("alternative_used"):
                    # [NEW] Generate special message explaining what alternative was shown
                    logger.info("[INFO] Generating alternative result explanation...")
                    contextual_response = (
                        await state.translator.generate_alternative_result_response(
                            state.natural_query,
                            state.classification or {},
                            {"success": True, "results": {"features": state.features}},
                            state.search_diagnostics.get("original_filters", {}),
                            state.search_diagnostics.get("alternative_filters", {}),
                            state.search_diagnostics.get("alternative_explanation", ""),
                            None,
                        )
                    )
                    state.response_message = contextual_response.get(
                        "message", f"Found {len(state.features)} alternative results."
                    )
                else:
                    # Standard success response
                    contextual_response = await state.translator.generate_contextual_earth_science_response(
                        state.natural_query,
                        state.classification or {},
                        {"success": True, "results": {"features": state.features}},
                        None,
                    )
                    state.response_message = contextual_response.get(
                        "message",
                        f"Found {len(state.features)} satellite data results for your query.",
                    )

                    # If we have vision analysis from hybrid query, prepend it
                    if early_contextual_response:
                        logger.info(
                            "[SHUFFLE] Combining vision analysis with STAC data response"
                        )
                        state.response_message = f"""**Visual Analysis:**
{early_contextual_response}

---

**Data Results:**
{state.response_message}"""

                logger.info("[OK] Contextual response message generated successfully")
            except Exception as e:
                logger.error(f"[FAIL] Response generation failed: {e}")
                state.response_message = generate_fallback_response(
                    state.natural_query,
                    state.features,
                    state.stac_query.get("collections", []) if state.stac_query else [],
                )
        elif (
            not state.features
            and state.stac_query
            and app_state.SEMANTIC_KERNEL_AVAILABLE
            and state.translator
        ):
            # [NEW] ENHANCED: Use GPT to generate context-aware response for empty results
            # GPT analyzes the specific failure point and provides intelligent, actionable suggestions
            try:
                logger.info(
                    f"[BOT] Generating GPT-powered empty result response (failure stage: {state.search_diagnostics.get('failure_stage')})"
                )
                state.response_message = (
                    await state.translator.generate_empty_result_response(
                        state.natural_query,
                        state.stac_query,
                        state.stac_query.get("collections", []),
                        state.search_diagnostics,
                    )
                )
                logger.info("[OK] GPT-generated empty result response created")
            except Exception as e:
                logger.error(
                    f"[FAIL] GPT empty result generation failed, using rule-based fallback: {e}"
                )
                # Fall back to rule-based response if GPT fails
                state.response_message = generate_contextual_empty_response(
                    state.natural_query,
                    state.stac_query.get("collections", []),
                    state.search_diagnostics,
                )
        elif not state.features and state.stac_query:
            # Fallback: Use rule-based response if Semantic Kernel not available
            logger.info(
                f"ℹ️ No features found - using rule-based diagnostic response (failure stage: {state.search_diagnostics.get('failure_stage')})"
            )
            state.response_message = generate_contextual_empty_response(
                state.natural_query,
                state.stac_query.get("collections", []),
                state.search_diagnostics,
            )
        else:
            # Fallback for edge cases (no stac_query generated)
            logger.info("[WARN] No STAC query generated - using generic fallback")
            state.response_message = "I can help you find Earth science data, but I need more specific information about the location, time period, or type of imagery you're looking for."

        # [MAP] Generate optimized tile URLs for collections that need seamless multi-tile coverage
        # Multi-tile rendering is for: DEM, MODIS composites, global coverage collections
        # Single-tile rendering is for: Optical imagery (Sentinel-2, HLS, Landsat) where we want the BEST image
        state.all_tile_urls = []
        all_bboxes = []  # [OK] Collect all bboxes for union calculation
        state.mosaic_result = (
            None  # [GLOBE] NEW: Mosaic tilejson result for seamless coverage
        )
        collections = (
            state.stac_query.get("collections", []) if state.stac_query else []
        )
        collection_id = collections[0].lower() if collections else ""

        # [SEARCH] DEBUG: Log what collections are in the features vs what was requested
        if state.features:
            feature_collections = list(
                set(f.get("collection", "unknown") for f in state.features)
            )
            logger.info(
                f"[SEARCH] COLLECTION DEBUG: Requested={collections}, Features contain={feature_collections}"
            )
            if set(feature_collections) != set(collections):
                logger.warning(
                    "[WARN] MISMATCH: STAC returned different collections than requested!"
                )

        # ========================================================================
        # [GLOBE] MOSAIC SERVICE: RE-ENABLED FOR OPTICAL IMAGERY
        # ========================================================================
        # Mosaic provides seamless tile coverage for high-resolution optical imagery.
        # Without mosaic, item-level tiles cause 404 errors when zooming outside
        # individual granule bounds (causing white/missing tiles).
        #
        # Enabled for: sentinel-2-l2a, landsat collections, hls collections
        # These have large granule footprints where users often zoom to areas
        # that fall between granule coverage.
        # ========================================================================
        needs_mosaic = needs_optical_mosaic(collection_id)

        if needs_mosaic and state.stac_query and state.stac_query.get("bbox"):
            logger.info(
                f"[GLOBE] MOSAIC: Enabling mosaic for optical collection {collection_id}"
            )
            try:
                # Import the mosaic function
                from hybrid_rendering_system import get_mosaic_tilejson_url

                state.mosaic_result = await get_mosaic_tilejson_url(
                    collections=[collection_id],
                    bbox=state.stac_query.get("bbox"),
                    datetime_range=state.stac_query.get("datetime"),
                    query_filters=(
                        {"eo:cloud_cover": {"lt": 30}}
                        if "cloud" not in state.natural_query.lower()
                        else None
                    ),
                    query_context=state.natural_query,
                )

                if state.mosaic_result:
                    logger.info(
                        f"[OK] MOSAIC: Successfully registered mosaic for {collection_id}"
                    )
                else:
                    logger.warning(
                        f"[WARN] MOSAIC: Registration failed for {collection_id}, will use item tiles"
                    )
            except Exception as e:
                logger.warning(f"[WARN] MOSAIC: Error registering mosaic: {e}")
                state.mosaic_result = None
        else:
            state.mosaic_result = None
            if not needs_mosaic:
                logger.info(
                    f"[MAP] MOSAIC: Skipping mosaic for {collection_id} (not an optical collection)"
                )

        # [MAP] TILE RENDERING: Generate individual tile URLs as fallback or supplement
        # The STAC query already returns features that cover the query bbox
        # We generate tile URLs for all features (up to 20) to ensure full area coverage
        # [ART] OPTIMIZATION: Generate optimized URLs for all features
        if state.features and len(state.features) > 0:
            # Process up to 20 features for full bbox coverage
            top_features = state.features[:20]
            logger.info(
                f"[MAP] Generating tile URLs for {len(top_features)} features (of {len(state.features)} total) for {collection_id}"
            )

            for feature in top_features:
                tilejson_asset = feature.get("assets", {}).get("tilejson", {})
                if tilejson_asset and "href" in tilejson_asset:
                    feature_bbox = feature.get("bbox")

                    if not feature_bbox or len(feature_bbox) != 4:
                        logger.warning(
                            f"[WARN] Skipping feature {feature.get('id')} - invalid bbox: {feature_bbox}"
                        )
                        continue

                    if any(
                        v is None or not isinstance(v, (int, float))
                        for v in feature_bbox
                    ):
                        logger.warning(
                            f"[WARN] Skipping feature {feature.get('id')} - bbox contains null values: {feature_bbox}"
                        )
                        continue

                    collection_id = feature.get(
                        "collection", collections[0] if collections else "unknown"
                    )

                    # [ART] Apply quality optimization to tilejson URL
                    original_url = tilejson_asset["href"]

                    # [SHIELD] EXPRESSION-BASED COLLECTIONS: Keep original STAC tilejson URL
                    # Collections like alos-palsar-mosaic use expressions (HH;HV;HH/HV) with multiple
                    # rescale values. The STAC tilejson already has the correct rendering params.
                    # Modifying the URL would strip the multi-rescale values and break rendering.
                    expression_collections = [
                        "alos-palsar-mosaic",
                        "sentinel-1-grd",
                        "sentinel-1-rtc",
                    ]
                    if (
                        collection_id in expression_collections
                        and "expression=" in original_url
                    ):
                        logger.info(
                            f"[SHIELD] Keeping original STAC tilejson for expression-based collection {collection_id}"
                        )
                        state.all_tile_urls.append(
                            {
                                "item_id": feature.get("id"),
                                "bbox": feature_bbox,
                                "tilejson_url": original_url,
                            }
                        )
                        if feature_bbox and len(feature_bbox) == 4:
                            all_bboxes.append(feature_bbox)
                        continue

                    # Build quality parameters for this collection
                    quality_params = build_tile_url_params(
                        collection_id, state.natural_query
                    )

                    # Enhance the URL with quality parameters
                    if quality_params:
                        optimized_url = optimize_tilejson_url(
                            original_url, quality_params
                        )

                        logger.info(
                            f"[ART] Optimized tile URL for {feature.get('id')}: {quality_params}"
                        )
                    else:
                        optimized_url = original_url
                        logger.info(
                            f"[WARN] No quality params generated for {feature.get('id')}"
                        )

                    state.all_tile_urls.append(
                        {
                            "item_id": feature.get("id"),
                            "bbox": feature_bbox,
                            "tilejson_url": optimized_url,  # [+] Use optimized URL
                        }
                    )

                    # [OK] Collect bbox for union calculation
                    if feature_bbox and len(feature_bbox) == 4:
                        all_bboxes.append(feature_bbox)

            # Calculate skipped features (features with invalid bbox)
            skipped_count = len(top_features) - len(state.all_tile_urls)

            if state.all_tile_urls:
                logger.info(
                    f"[MAP] Generated {len(state.all_tile_urls)} tile URLs for bbox coverage (skipped {skipped_count} with invalid bbox)"
                )

                # [SEARCH] PIPELINE LOG: Tile URLs Output
                log_pipeline_step(
                    state.pipeline_session_id,
                    "TILE_URLS",
                    "OUTPUT",
                    {
                        "tile_count": len(state.all_tile_urls),
                        "skipped_invalid_bbox": skipped_count,
                        "tile_ids": [
                            t.get("item_id") for t in state.all_tile_urls[:5]
                        ],  # First 5 for brevity
                        "has_more": len(state.all_tile_urls) > 5,
                    },
                )

                # [OK] Calculate union bbox covering ALL tiles
                if all_bboxes:
                    union_bbox = union_of_bboxes(all_bboxes)
                    logger.info(f"[MAP] Union bbox calculated: {union_bbox}")
                    logger.info(
                        f"   Coverage: {union_bbox[2] - union_bbox[0]:.2f}° × {union_bbox[3] - union_bbox[1]:.2f}°"
                    )

                    # [SEARCH] PIPELINE LOG: Union BBox
                    log_pipeline_step(
                        state.pipeline_session_id,
                        "UNION_BBOX",
                        "OUTPUT",
                        {
                            "bbox": union_bbox,
                            "width_deg": round(union_bbox[2] - union_bbox[0], 2),
                            "height_deg": round(union_bbox[3] - union_bbox[1], 2),
                        },
                    )

                    # [OK] Update stac_params with union bbox for response metadata
                    if state.stac_params:
                        state.stac_params["bbox"] = union_bbox
                        logger.info(
                            f"[OK] Updated stac_params bbox to union: {union_bbox}"
                        )

        # If local STAC returned features but none had tilejson assets (i.e. they are
        # raw local STAC items without PC-style tile endpoints), retry against PC so
        # the user still gets renderable imagery.
        if (
            not state.all_tile_urls
            and not state.mosaic_result
            and state.features
            and state.stac_endpoint != "planetary_computer"
        ):
            logger.info(
                "[SIGNAL] Local STAC features have no tilejson assets — retrying against Planetary Computer"
            )
            pc_collections = detect_collections(state.natural_query)
            pc_stac_query = {**state.stac_query, "collections": pc_collections}
            pc_stac_response = await execute_direct_stac_search(
                pc_stac_query, "planetary_computer", original_query=state.natural_query
            )
            if pc_stac_response.get("success"):
                pc_features = pc_stac_response.get("results", {}).get("features", [])
                if pc_features:
                    logger.info(
                        f"[OK] PC fallback returned {len(pc_features)} renderable features"
                    )
                    state.features = pc_features
                    state.stac_response = pc_stac_response
                    # Re-run tile URL generation for PC features
                    state.all_tile_urls = []
                    collection_id = pc_features[0].get(
                        "collection", collections[0] if collections else ""
                    )
                    for feature in pc_features[:20]:
                        tilejson_asset = feature.get("assets", {}).get("tilejson", {})
                        if tilejson_asset and "href" in tilejson_asset:
                            feature_bbox = feature.get("bbox")
                            if not has_numeric_bbox(feature_bbox):
                                continue
                            feat_collection_id = feature.get(
                                "collection", collection_id
                            )
                            optimized_url = optimize_tilejson_url(
                                tilejson_asset["href"],
                                build_tile_url_params(
                                    feat_collection_id, state.natural_query
                                ),
                            )
                            state.all_tile_urls.append(
                                {
                                    "item_id": feature.get("id"),
                                    "bbox": feature_bbox,
                                    "tilejson_url": optimized_url,
                                }
                            )
                    # Re-register mosaic for PC features
                    if (
                        not state.mosaic_result
                        and state.stac_query
                        and state.stac_query.get("bbox")
                    ):
                        pc_collection_id = (
                            pc_features[0].get("collection", "") if pc_features else ""
                        )
                        if needs_optical_mosaic(pc_collection_id):
                            try:
                                from hybrid_rendering_system import (
                                    get_mosaic_tilejson_url,
                                )

                                state.mosaic_result = await get_mosaic_tilejson_url(
                                    collections=[pc_collection_id],
                                    bbox=state.stac_query.get("bbox"),
                                    datetime_range=state.stac_query.get("datetime"),
                                    query_filters=(
                                        {"eo:cloud_cover": {"lt": 30}}
                                        if "cloud" not in state.natural_query.lower()
                                        else None
                                    ),
                                    query_context=state.natural_query,
                                )
                                if state.mosaic_result:
                                    logger.info(
                                        f"[OK] MOSAIC: PC fallback mosaic registered for {pc_collection_id}"
                                    )
                            except aiohttp.ClientError as e:
                                logger.warning(
                                    f"[WARN] MOSAIC: PC fallback mosaic registration network error: {e}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"[WARN] MOSAIC: PC fallback mosaic registration failed: {e}"
                                )

        # Clean up STAC results to remove problematic asset_bidx parameters
        cleaned_stac_results = clean_tilejson_urls(
            state.stac_response.get("results", {})
        )

        # Prepare final response
        complete_response = {
            "success": True,
            "response": state.response_message,
            "data": {
                "stac_results": cleaned_stac_results,
                "search_metadata": {
                    "total_items": len(state.features),
                    "collections_searched": (
                        state.stac_query.get("collections", [])
                        if state.stac_query
                        else []
                    ),
                    "spatial_extent": (
                        state.stac_query.get("bbox") if state.stac_query else None
                    ),
                    "temporal_range": (
                        state.stac_query.get("datetime") if state.stac_query else None
                    ),
                    "search_timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "query_classification": (
                    {
                        "intent_type": (
                            state.classification.get("intent_type")
                            if state.classification
                            else "unknown"
                        ),
                        "needs_satellite_data": (
                            state.classification.get("needs_satellite_data")
                            if state.classification
                            else True
                        ),
                        "needs_contextual_info": (
                            state.classification.get("needs_contextual_info")
                            if state.classification
                            else False
                        ),
                        "confidence": (
                            state.classification.get("confidence")
                            if state.classification
                            else 0
                        ),
                    }
                    if state.classification
                    else None
                ),
            },
            "translation_metadata": {
                "original_query": state.natural_query,
                "translated_params": state.stac_params,
                "stac_query": state.stac_query,
                "translation_timestamp": datetime.now(timezone.utc).isoformat(),
                "all_tile_urls": (
                    state.all_tile_urls if state.all_tile_urls else None
                ),  # [+] Individual tile URLs for multi-tile rendering
                # [GLOBE] NEW: Mosaic tilejson for seamless composited coverage
                "mosaic_tilejson": state.mosaic_result if state.mosaic_result else None,
            },
            "debug": {
                "semantic_translator": {
                    "available": app_state.SEMANTIC_KERNEL_AVAILABLE,
                    "selected_collections": (
                        state.stac_query.get("collections", [])
                        if state.stac_query
                        else []
                    ),
                    "location_info": {
                        "bbox": (
                            state.stac_query.get("bbox") if state.stac_query else None
                        ),
                        "location_name": state.natural_query,
                    },
                    "query_processing": {
                        "original_query": state.natural_query,
                        "stac_parameters": state.stac_params,
                        "processing_timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                },
                # [MICRO] INSTANT PIPELINE TRACE - Visible in browser console!
                "pipeline_trace": {
                    "session_id": state.pipeline_session_id,
                    "steps": get_pipeline_trace(state.pipeline_session_id),
                },
            },
        }

        # [MICRO] TRACE: Final response summary
        log_pipeline_step(
            state.pipeline_session_id,
            "RESPONSE",
            "OUTPUT",
            {
                "success": True,
                "total_tiles": len(state.all_tile_urls) if state.all_tile_urls else 0,
                "total_features": len(state.features),
                "has_mosaic": state.mosaic_result is not None,
                "collections": (
                    state.stac_query.get("collections", []) if state.stac_query else []
                ),
            },
        )

        logger.info("[OK] Unified query processing completed successfully")

        # ========================================================================
        # �️ UPDATE SESSION CONTEXT: Mark map as rendered for follow-up queries
        # ========================================================================
        # CRITICAL: This enables the router to default to VISION for follow-up questions
        # like "What city is on the map?" instead of trying another STAC search
        # ========================================================================
        if (
            app_state.router_agent
            and state.session_id
            and (state.all_tile_urls or state.mosaic_result or state.features)
        ):
            # Prepare STAC items for vision agent (lightweight version with key fields)
            stac_items_for_vision = summarize_stac_items(
                state.features or [], include_cloud_cover_alias=True
            )

            stac_render_context = {
                "has_rendered_map": True,  # Map now has rendered imagery
                "last_bbox": state.stac_query.get("bbox") if state.stac_query else None,
                "last_location": (
                    state.stac_params.get("location_name")
                    if state.stac_params
                    else None
                ),
                "last_collections": (
                    state.stac_query.get("collections", []) if state.stac_query else []
                ),
                "last_stac_items": stac_items_for_vision,  # STAC items for vision agent raster analysis
                "query_count": app_state.router_agent.tools.session_contexts.get(
                    state.session_id, {}
                ).get("query_count", 0)
                + 1,
            }
            app_state.router_agent.update_session_context(
                state.session_id, stac_render_context
            )
            logger.info(
                f"[MAP] Updated session context: has_rendered_map=True, collections={stac_render_context['last_collections']}, stac_items={len(stac_items_for_vision)}"
            )

        return complete_response

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.error("[FAIL] Unified query processor timed out")
        raise HTTPException(status_code=504, detail="Query processing timed out")
    except aiohttp.ClientError as e:
        logger.error(f"[FAIL] Unified query processor network error: {e}")
        raise HTTPException(status_code=502, detail="Upstream service unavailable")
    except Exception as e:
        logger.error(f"[FAIL] Unified query processor error: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Query processing failed")


@router.post("/api/process-comparison-query")
async def process_comparison_query(body: ComparisonQueryRequest):
    """
    Process natural language comparison query to extract parameters.

    REUSES EXISTING AGENTS:
    - Collection selection: collection_mapping_agent (same as regular queries)
    - Location resolution: build_stac_query_agent -> bbox extraction (same as regular queries)
    - Datetime extraction: datetime_translation_agent in "comparison" mode (NEW dual-date support)

    Request body:
    {
        "query": "Show wildfire activity in Southern California January 2025 over 48 hours"
    }

    Returns:
    {
        "location": {"lat": 34.05, "lng": -118.24},
        "aspect": "wildfire activity",
        "before_date": "2025-01-15T00:00:00Z",
        "after_date": "2025-01-17T00:00:00Z",
        "collections": ["modis-14A1-061", "sentinel-2-l2a"],
        "primary_collection": "modis-14A1-061",
        "explanation": "Analyzing wildfire progression over 48-hour period"
    }
    """
    try:
        user_query = body.query

        if not user_query:
            raise HTTPException(status_code=400, detail="Query is required")

        logger.info(f"[CHART] [COMPARISON] query='{user_query}'")

        if not app_state.semantic_translator:
            raise HTTPException(
                status_code=500, detail="Semantic translator not initialized"
            )

        # Step 1: Collection Selection
        collections = await app_state.semantic_translator.collection_mapping_agent(
            user_query
        )
        logger.info(f"[CHART] [COMPARISON] Step 1 collections={collections}")

        if not collections:
            logger.warning("[CHART] [COMPARISON] No collections returned")

        # Step 2: Location Resolution
        stac_query = await app_state.semantic_translator.build_stac_query_agent(
            user_query, collections
        )
        bbox = stac_query.get("bbox")
        location_name = stac_query.get("location_name", "Unknown location")

        if not bbox:
            logger.error(f"[CHART] [COMPARISON] No bbox returned: {stac_query}")
            raise HTTPException(
                status_code=400,
                detail=f"Could not resolve location from query: '{user_query}'",
            )

        lng = (bbox[0] + bbox[2]) / 2
        lat = (bbox[1] + bbox[3]) / 2
        logger.info(
            f"[CHART] [COMPARISON] Step 2 location={location_name} ({lat:.4f},{lng:.4f})"
        )

        # Step 3: Dual-Date Extraction
        datetime_result = (
            await app_state.semantic_translator.datetime_translation_agent(
                query=user_query, collections=collections, mode="comparison"
            )
        )

        if not datetime_result:
            raise HTTPException(
                status_code=400,
                detail="Could not extract timeframes from query. Please specify time periods (e.g., 'between 2023 and 2024').",
            )

        if "before" not in datetime_result or "after" not in datetime_result:
            logger.error(
                f"[CHART] [COMPARISON] Missing before/after in datetime result: {datetime_result}"
            )
            raise HTTPException(
                status_code=400,
                detail="Could not extract before/after timeframes from query. Please specify time periods (e.g., 'between 2023 and 2024').",
            )

        before_date = datetime_result["before"]
        after_date = datetime_result["after"]
        explanation = datetime_result.get("explanation", "")
        logger.info(
            f"[CHART] [COMPARISON] Step 3 before={before_date} after={after_date}"
        )

        # Step 4: Primary Collection Selection
        primary_collection = None
        aspect = "general change"

        if collections:
            priority_map = {
                "modis-14A1-061": "wildfire activity",
                "cop-dem-glo-30": "terrain/elevation",
                "cop-dem-glo-90": "terrain/elevation",
                "alos-dem": "terrain/elevation",
                "sentinel-2-l2a": "optical imagery",
                "landsat-c2-l2": "optical imagery",
            }

            for priority_collection, priority_aspect in priority_map.items():
                if priority_collection in collections:
                    primary_collection = priority_collection
                    aspect = priority_aspect
                    break

            if not primary_collection and collections:
                primary_collection = collections[0]
                aspect = "general change"

        logger.info(
            f"[CHART] [COMPARISON] Step 4 primary={primary_collection} aspect={aspect}"
        )

        result = {
            "status": "success",
            "location": {"lat": lat, "lng": lng},
            "location_name": location_name,
            "bbox": bbox,
            "aspect": aspect,
            "before_date": before_date,
            "after_date": after_date,
            "collections": collections,
            "primary_collection": primary_collection,
            "explanation": explanation,
            "timestamp": datetime.now().isoformat(),
        }

        logger.info(
            f"[CHART] [COMPARISON] [OK] Done — {location_name}, {primary_collection}, {before_date}->{after_date}"
        )
        return result

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.error("[CHART] [COMPARISON] [FAIL] Request timed out")
        raise HTTPException(status_code=504, detail="Comparison query timed out")
    except Exception as e:
        logger.error(f"[CHART] [COMPARISON] [FAIL] {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to process comparison query"
        )
