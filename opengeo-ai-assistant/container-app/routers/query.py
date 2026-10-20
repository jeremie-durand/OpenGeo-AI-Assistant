"""Unified natural-language query endpoint and comparison query endpoint."""

import asyncio
import json
import logging
import os
import traceback
from datetime import datetime, timezone

import aiohttp
import app_state
from fastapi import APIRouter, HTTPException
from quickstart_cache import (
    get_quickstart_location,
    is_quickstart_query,
)
from request_models import ComparisonQueryRequest, QueryRequest
from services.collection_detection import detect_collections
from services.pipeline_trace import (
    clear_pipeline_trace,
    get_pipeline_trace,
    log_pipeline_step,
)
from services.query_pipeline.response import summarize_stac_items
from services.query_pipeline.state import QueryState
from services.query_pipeline.tiles import (
    has_numeric_bbox,
    needs_optical_mosaic,
    optimize_tilejson_url,
    union_of_bboxes,
)
from services.query_responses import (
    generate_contextual_empty_response,
    generate_fallback_response,
)
from services.stac_search import (
    _stac_base,
    build_stac_query,
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
        state.natural_query = body.query or body.user_query or "No query provided"
        state.session_id = body.session_id
        state.pin = body.pin
        selected_model = body.model

        # Mutable dict alias so all downstream req_body.get() calls keep working
        state.req_body = body.model_dump()

        logger.info(f"[BOT] Selected Model: {selected_model}")

        # Set the active model on the semantic translator before processing
        if app_state.semantic_translator:
            app_state.semantic_translator.set_model(selected_model)
        else:
            logger.warning(
                "[WARN] semantic_translator is None - using keyword-based fallback for collection detection."
            )

        # [MICRO] Generate unique pipeline session ID for tracing
        import uuid

        state.pipeline_session_id = str(uuid.uuid4())[:8]

        # [MICRO] Clear any stale trace and start fresh
        clear_pipeline_trace(state.pipeline_session_id)

        # [MICRO] TRACE: Initial query received
        log_pipeline_step(
            state.pipeline_session_id,
            "QUERY_RECEIVED",
            "INPUT",
            {
                "query": state.natural_query,
                "session_id": state.session_id,
                "has_pin": bool(state.pin),
                "pin_coords": (
                    f"({state.pin.get('lat'):.4f}, {state.pin.get('lng'):.4f})"
                    if state.pin
                    else None
                ),
            },
        )

        pin_str = (
            f"({state.pin.get('lat'):.4f},{state.pin.get('lng'):.4f})"
            if state.pin
            else "none"
        )
        logger.info(
            f"[TEXT] Query='{state.natural_query}' session={state.session_id} pin={pin_str}"
        )

        # Normal STAC search routing (vision handled by /api/geoint/vision)

        # Check for pin parameter
        if state.pin:
            pin_lat = state.pin.get("lat")
            pin_lng = state.pin.get("lng")
            logger.info(f"[PIN] Pin detected: ({pin_lat:.4f}, {pin_lng:.4f})")

        # ================================================================
        # [FAST] QUICKSTART FAST PATH: Skip ALL GPT calls for demo queries
        # ================================================================
        # For the pre-defined quick start queries, we have pre-computed
        # collections, bbox, datetime, and classification. This bypasses:
        #   1. RouterAgent GPT call (~2-4s saved)
        #   2. translate_query with 3 GPT agent calls (~3-6s saved)
        #   3. generate_contextual_earth_science_response GPT call (~2-4s saved)
        # Total savings: ~7-14 seconds per demo query
        # ================================================================
        if is_quickstart_query(state.natural_query):
            import time as _time

            _qs_start = _time.perf_counter()

            qs_location = get_quickstart_location(state.natural_query)
            logger.info(
                f"[FAST] QUICKSTART FAST PATH: '{state.natural_query}' -> {qs_location['collections']} @ {qs_location['location']}"
            )

            # Build STAC params from pre-computed data
            temporal = qs_location.get("temporal")
            datetime_range = None
            if temporal:
                if len(temporal) == 4:  # Year: "2017"
                    datetime_range = f"{temporal}-01-01/{temporal}-12-31"
                elif len(temporal) == 7:  # Month: "2025-06"
                    import calendar as _cal

                    _y, _m = int(temporal[:4]), int(temporal[5:7])
                    _last = _cal.monthrange(_y, _m)[1]
                    datetime_range = f"{temporal}-01/{temporal}-{_last:02d}"
                else:
                    datetime_range = temporal

            qs_stac_params = {
                "collections": qs_location["collections"],
                "bbox": qs_location["bbox"],
                "datetime": datetime_range,
                "location_name": qs_location["location"],
                "is_quickstart": True,
            }
            qs_stac_query = build_stac_query(qs_stac_params)

            # Execute STAC search — route to local STAC if configured, else Planetary Computer
            qs_stac_endpoint = (
                app_state.global_translator.determine_stac_source(
                    state.natural_query, qs_stac_params
                )
                if app_state.global_translator
                else "planetary_computer"
            )
            qs_stac_response = await execute_direct_stac_search(
                qs_stac_query, qs_stac_endpoint, original_query=state.natural_query
            )

            qs_features = []
            if qs_stac_response.get("success"):
                qs_features = qs_stac_response.get("results", {}).get("features", [])
                logger.info(f"[FAST] STAC returned {len(qs_features)} features")

            # Generate template response (no GPT call needed)
            qs_dataset = qs_location.get("dataset", qs_location["collections"][0])
            qs_desc = qs_location.get("description", "")
            if qs_features:
                qs_first = qs_features[0]
                qs_props = qs_first.get("properties", {})
                qs_date = (
                    qs_props.get("datetime")
                    or qs_props.get("start_datetime")
                    or qs_props.get("end_datetime")
                    or ""
                )
                if qs_date and len(qs_date) >= 10:
                    qs_date = qs_date[:10]
                if qs_date:
                    qs_response_msg = (
                        f"Loaded {len(qs_features)} {qs_dataset} tiles for {qs_location['location']}. "
                        f"{qs_desc}. Most recent tile: {qs_date}."
                    )
                else:
                    qs_response_msg = (
                        f"Loaded {len(qs_features)} {qs_dataset} tiles for {qs_location['location']}. "
                        f"{qs_desc}."
                    )
            else:
                qs_response_msg = (
                    f"Searched {qs_dataset} for {qs_location['location']} but no tiles were found "
                    f"matching the current filters. Try adjusting the time range."
                )

            # Build tile URLs (same logic as main path but inlined for speed)
            qs_tile_urls = []
            qs_collection_id = (
                qs_location["collections"][0] if qs_location["collections"] else ""
            )

            for feat in qs_features[:20]:
                tj_asset = feat.get("assets", {}).get("tilejson", {})
                if tj_asset and "href" in tj_asset:
                    feat_bbox = feat.get("bbox")
                    if not has_numeric_bbox(feat_bbox):
                        continue

                    optimized_url = optimize_tilejson_url(
                        tj_asset["href"],
                        build_tile_url_params(qs_collection_id, state.natural_query),
                    )

                    qs_tile_urls.append(
                        {
                            "item_id": feat.get("id"),
                            "bbox": feat_bbox,
                            "tilejson_url": optimized_url,
                        }
                    )

            # Mosaic for optical collections
            qs_mosaic = None
            if needs_optical_mosaic(qs_collection_id):
                try:
                    from hybrid_rendering_system import get_mosaic_tilejson_url

                    qs_mosaic = await get_mosaic_tilejson_url(
                        collections=[qs_collection_id],
                        bbox=qs_location["bbox"],
                        datetime_range=datetime_range,
                        query_filters=None,
                        query_context=state.natural_query,
                    )
                except Exception:
                    pass

            # Update session context
            if (
                app_state.router_agent
                and state.session_id
                and (qs_tile_urls or qs_features)
            ):
                stac_items_for_session = summarize_stac_items(
                    qs_features, include_cloud_cover_alias=False
                )
                app_state.router_agent.update_session_context(
                    state.session_id,
                    {
                        "has_rendered_map": True,
                        "last_bbox": qs_location["bbox"],
                        "last_location": qs_location["location"],
                        "last_collections": qs_location["collections"],
                        "last_stac_items": stac_items_for_session,
                        "query_count": app_state.router_agent.tools.session_contexts.get(
                            state.session_id, {}
                        ).get(
                            "query_count", 0
                        )
                        + 1,
                    },
                )

            _qs_elapsed = (_time.perf_counter() - _qs_start) * 1000
            logger.info(
                f"[FAST] QUICKSTART FAST PATH completed in {_qs_elapsed:.0f}ms (skipped RouterAgent + translate_query + response GPT)"
            )

            cleaned_qs_results = clean_tilejson_urls(
                qs_stac_response.get("results", {})
            )

            return {
                "success": True,
                "response": qs_response_msg,
                "data": {
                    "stac_results": cleaned_qs_results,
                    "search_metadata": {
                        "total_items": len(qs_features),
                        "collections_searched": qs_location["collections"],
                        "spatial_extent": qs_location["bbox"],
                        "temporal_range": datetime_range,
                        "search_timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    "query_classification": {
                        "intent_type": "stac",
                        "needs_satellite_data": True,
                        "needs_contextual_info": False,
                        "confidence": 1.0,
                    },
                },
                "translation_metadata": {
                    "original_query": state.natural_query,
                    "translated_params": qs_stac_params,
                    "stac_query": qs_stac_query,
                    "translation_timestamp": datetime.now(timezone.utc).isoformat(),
                    "all_tile_urls": qs_tile_urls if qs_tile_urls else None,
                    "mosaic_tilejson": qs_mosaic,
                },
                "debug": {
                    "semantic_translator": {
                        "available": True,
                        "selected_collections": qs_location["collections"],
                        "location_info": {
                            "bbox": qs_location["bbox"],
                            "location_name": qs_location["location"],
                        },
                    },
                    "fast_path": True,
                    "fast_path_elapsed_ms": round(_qs_elapsed, 1),
                },
            }

        # ================================================================
        # [PIN] EARLY SESSION BBOX OVERRIDE: Fix stale frontend coordinates
        # ================================================================
        # After navigate_to, the frontend's map bounds may still be stale
        # (the useEffect doesn't always re-fire immediately). We override
        # map_bounds with the session bbox here so that ALL downstream
        # handlers (vision, contextual, STAC) get the correct coordinates.
        # ================================================================
        frontend_map_bounds = state.req_body.get("map_bounds")
        if app_state.router_agent and state.session_id:
            session_ctx = app_state.router_agent.tools.session_contexts.get(
                state.session_id, {}
            )
            session_bbox_early = session_ctx.get("last_bbox")
            recently_navigated_early = session_bbox_early and not session_ctx.get(
                "has_rendered_map", True
            )

            if session_bbox_early and (
                not frontend_map_bounds or recently_navigated_early
            ):
                override_reason_early = (
                    "no frontend bounds"
                    if not frontend_map_bounds
                    else "post-navigate_to (frontend bounds stale)"
                )
                corrected_bounds = {
                    "west": session_bbox_early[0],
                    "south": session_bbox_early[1],
                    "east": session_bbox_early[2],
                    "north": session_bbox_early[3],
                    "center_lat": (session_bbox_early[1] + session_bbox_early[3]) / 2,
                    "center_lng": (session_bbox_early[0] + session_bbox_early[2]) / 2,
                }
                state.req_body["map_bounds"] = corrected_bounds
                logger.info(
                    f"[PIN] EARLY BBOX OVERRIDE ({override_reason_early}): Using session bbox for {session_ctx.get('last_location')}: center=({corrected_bounds['center_lat']:.4f}, {corrected_bounds['center_lng']:.4f})"
                )

        # Continue with regular STAC search flow
        state.router_action = None

        # ========================================================================
        # [ROUTE] ROUTER AGENT: Intelligent Query Classification and Routing
        # ========================================================================
        # RouterAgent classifies queries and routes them to the appropriate handler.
        #
        # Action Types:
        # - navigate_to: Pan map to location (no STAC search, immediate response)
        # - stac_search: Load satellite imagery (continues to existing STAC flow)
        #
        # NOTE: Vision analysis is now handled by explicit Vision GEOINT module,
        # not by the RouterAgent. Users activate Vision mode, then all queries
        # route directly to the Vision Agent.
        # ========================================================================

        if state.router_action is None and app_state.router_agent and state.session_id:
            try:
                # Update session context for the router
                if app_state.global_translator:
                    context = app_state.global_translator.get_conversation_context(
                        state.session_id
                    )
                    context["has_screenshot"] = bool(
                        state.req_body.get("imagery_base64")
                    )
                    # ================================================================
                    # CRITICAL FIX: Preserve has_rendered_map from router's existing context
                    # The router agent may have set has_rendered_map=True from a previous
                    # STAC render, and we don't want to overwrite it with the translator's
                    # context (which may not have been updated). This ensures follow-up
                    # questions like "what is on the map" route to vision correctly.
                    # ================================================================
                    existing_router_context = (
                        app_state.router_agent.tools.session_contexts.get(
                            state.session_id, {}
                        )
                    )

                    # Preserve has_rendered_map if already True in router context
                    if existing_router_context.get("has_rendered_map"):
                        context["has_rendered_map"] = True
                    # Also set from frontend's has_satellite_data flag (new request indicator)
                    elif state.req_body.get("has_satellite_data"):
                        context["has_rendered_map"] = True

                    # Preserve last_bbox and last_location from router context if not in translator context
                    if not context.get("last_bbox") and existing_router_context.get(
                        "last_bbox"
                    ):
                        context["last_bbox"] = existing_router_context.get("last_bbox")
                    if not context.get("last_location") and existing_router_context.get(
                        "last_location"
                    ):
                        context["last_location"] = existing_router_context.get(
                            "last_location"
                        )
                    if not context.get(
                        "last_collections"
                    ) and existing_router_context.get("last_collections"):
                        context["last_collections"] = existing_router_context.get(
                            "last_collections"
                        )

                    app_state.router_agent.update_session_context(
                        state.session_id, context
                    )

                # Vision mode already checked above - just invoke RouterAgent
                logger.info("[ROUTE] Invoking RouterAgent for query classification...")
                state.router_action = await app_state.router_agent.route_query(
                    query=state.natural_query,
                    session_id=state.session_id,
                    has_screenshot=bool(state.req_body.get("imagery_base64")),
                )

                logger.info(
                    f"[ROUTE] RouterAgent decision: {state.router_action.get('action_type', 'unknown')}"
                )
                log_pipeline_step(
                    state.pipeline_session_id,
                    "ROUTER_AGENT",
                    "OUTPUT",
                    {
                        "action_type": state.router_action.get("action_type"),
                        "needs_stac": state.router_action.get(
                            "needs_stac_search", False
                        ),
                        "needs_vision": state.router_action.get(
                            "needs_vision_analysis", False
                        ),
                    },
                )

                # ================================================================
                # HANDLE NAVIGATE_TO ACTION: Map-only navigation (no STAC search)
                # ================================================================
                if state.router_action.get("action_type") == "navigate_to":
                    # Get location from router action, or fall back to original query for bare locations
                    location = state.router_action.get(
                        "location"
                    ) or state.router_action.get("original_query", "")
                    zoom_level = state.router_action.get("zoom_level", 12)

                    # Capitalize location name properly (e.g., "albania" -> "Albania")
                    location_display = location.title() if location else location

                    logger.info(
                        f"[MAP] NAVIGATE_TO ACTION: Navigating to '{location_display}' at zoom {zoom_level}"
                    )

                    # Resolve location to coordinates using location_resolver
                    from location_resolver import EnhancedLocationResolver

                    resolver = EnhancedLocationResolver()

                    try:
                        bbox = await resolver.resolve_location_to_bbox(location)
                        if bbox and len(bbox) == 4:
                            # Calculate center from bbox [west, south, east, north]
                            center_lng = (bbox[0] + bbox[2]) / 2
                            center_lat = (bbox[1] + bbox[3]) / 2

                            logger.info(
                                f"[OK] Location resolved: {location_display} -> ({center_lat:.4f}, {center_lng:.4f})"
                            )

                            # ============================================================
                            # [PIN] UPDATE SESSION CONTEXT: Store bbox for follow-up queries
                            # ============================================================
                            # When user navigates to a location, we store the bbox so that
                            # follow-up queries like "show me Sentinel tiles" will use this location
                            # ============================================================
                            if app_state.router_agent and state.session_id:
                                navigate_context = {
                                    "last_bbox": bbox,
                                    "last_location": location_display,
                                    "has_rendered_map": False,  # No imagery yet, just navigation
                                    "query_count": app_state.router_agent.tools.session_contexts.get(
                                        state.session_id, {}
                                    ).get(
                                        "query_count", 0
                                    )
                                    + 1,
                                }
                                app_state.router_agent.update_session_context(
                                    state.session_id, navigate_context
                                )
                                logger.info(
                                    f"[PIN] Updated session context with navigate_to location: {location_display}, bbox: {bbox}"
                                )

                            return {
                                "success": True,
                                "action": "navigate_to",
                                "response": f"Navigating to {location_display}.",
                                "user_response": f"Navigating to {location_display}.",
                                "navigate_to": {
                                    "latitude": center_lat,
                                    "longitude": center_lng,
                                    "zoom": zoom_level,
                                    "bbox": bbox,
                                    "location_name": location_display,
                                },
                                "processing_type": "map_navigation",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                        else:
                            logger.warning(
                                f"[WARN] Could not resolve location '{location}' via stored locations"
                            )
                            return {
                                "success": True,
                                "action": "contextual",
                                "response": f"I couldn't pinpoint the exact location for '{location_display}'. Could you try a more specific place name or add the country?",
                                "user_response": f"I couldn't pinpoint the exact location for '{location_display}'. Could you try a more specific place name or add the country?",
                                "processing_type": "contextual",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                    except Exception as loc_error:
                        logger.error(f"[FAIL] Location resolution failed: {loc_error}")
                        # Return contextual response instead of falling through to STAC
                        return {
                            "success": True,
                            "action": "contextual",
                            "response": f"I had trouble resolving the location '{location_display}'. Please try again or use a different place name.",
                            "user_response": f"I had trouble resolving the location '{location_display}'. Please try again or use a different place name.",
                            "processing_type": "contextual",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }

                # ================================================================
                # [STORM] HANDLE EXTREME_WEATHER ACTION: Climate projection via CMIP6
                # ================================================================
                # Route climate projection queries to ExtremeWeatherAgent inline,
                # so users can ask climate questions from the normal chat without
                # needing to manually select the extreme weather module + drop a pin.
                # ================================================================
                if state.router_action.get("action_type") == "extreme_weather":
                    location = state.router_action.get(
                        "location"
                    ) or state.router_action.get("original_query", "")
                    logger.info(
                        f"[STORM] EXTREME_WEATHER ACTION: Climate query for '{location}'"
                    )

                    try:
                        # Resolve location to coordinates
                        from location_resolver import EnhancedLocationResolver

                        resolver = EnhancedLocationResolver()

                        bbox = (
                            await resolver.resolve_location_to_bbox(location)
                            if location
                            else None
                        )

                        # Use session context bbox as fallback
                        if not bbox and app_state.router_agent and state.session_id:
                            ctx = app_state.router_agent.tools.session_contexts.get(
                                state.session_id, {}
                            )
                            bbox = ctx.get("last_bbox")
                            location = location or ctx.get(
                                "last_location", "this location"
                            )

                        if bbox and len(bbox) == 4:
                            center_lng = (bbox[0] + bbox[2]) / 2
                            center_lat = (bbox[1] + bbox[3]) / 2
                        else:
                            # Cannot determine coordinates — fall through to STAC
                            logger.warning(
                                "[WARN] No coordinates for climate query, falling through"
                            )
                            center_lat = center_lng = None

                        if center_lat is not None and center_lng is not None:
                            import uuid as _uuid

                            from geoint.extreme_weather_agent import (
                                get_extreme_weather_agent,
                            )

                            agent = get_extreme_weather_agent()
                            climate_session_id = state.session_id or str(_uuid.uuid4())
                            user_query = state.router_action.get(
                                "original_query", "Provide a climate overview."
                            )

                            result = await asyncio.wait_for(
                                agent.chat(
                                    session_id=climate_session_id,
                                    user_message=user_query,
                                    latitude=center_lat,
                                    longitude=center_lng,
                                ),
                                timeout=280.0,
                            )

                            analysis = result.get(
                                "response", "Climate analysis complete."
                            )
                            location_display = (
                                location.title() if location else "this location"
                            )

                            logger.info(
                                f"[STORM] Climate analysis complete for {location_display}"
                            )

                            # Return with navigate_to so map pans to location
                            return {
                                "success": True,
                                "action": "navigate_to",
                                "response": analysis,
                                "user_response": analysis,
                                "navigate_to": {
                                    "latitude": center_lat,
                                    "longitude": center_lng,
                                    "zoom": 10,
                                    "bbox": bbox,
                                    "location_name": location_display,
                                },
                                "processing_type": "climate_projection",
                                "session_id": climate_session_id,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                    except asyncio.TimeoutError:
                        logger.error(
                            f"[STORM] Climate analysis timed out for {location}"
                        )
                        return {
                            "success": False,
                            "response": f"[TIME] Climate analysis for {location} timed out. Please try a more specific question.",
                            "user_response": f"[TIME] Climate analysis for {location} timed out. Please try a more specific question.",
                            "processing_type": "climate_projection",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    except Exception as climate_error:
                        logger.error(
                            f"[STORM] Climate analysis failed: {climate_error}"
                        )
                        # Fall through to STAC search as fallback

            except Exception as router_error:
                logger.warning(
                    f"[WARN] RouterAgent failed: {router_error}, falling back to legacy classification"
                )
                state.router_action = None

        # PHASE 0: EARLY QUERY INTENT CLASSIFICATION
        state.classification = None
        early_contextual_response = None
        state.translator = None
        state.vision_task = None  # For parallel processing of hybrid queries

        # ========================================================================
        # ROUTER AGENT -> CLASSIFICATION BRIDGE
        # ========================================================================
        # Convert RouterAgent action_type to classification format for compatibility
        # with existing downstream handlers. This is the SINGLE source of truth for
        # query classification - no legacy Intent Classifier fallback needed.
        # ========================================================================
        if state.router_action:
            action_type = state.router_action.get("action_type", "")

            if action_type == "stac_search":
                logger.info(
                    "Converting RouterAgent 'stac_search' to classification format"
                )
                state.classification = {
                    "intent_type": "stac",
                    "confidence": 0.95,
                    "needs_satellite_data": True,
                    "needs_vision_analysis": False,
                    "needs_contextual_info": False,
                    "reasoning": "RouterAgent routed to STAC search",
                    "router_action": state.router_action,
                }
            elif action_type == "vision_analysis":
                logger.info(
                    "Converting RouterAgent 'vision_analysis' to classification format"
                )
                state.classification = {
                    "intent_type": "vision",
                    "confidence": 0.95,
                    "needs_satellite_data": False,
                    "needs_vision_analysis": True,
                    "needs_contextual_info": False,
                    "reasoning": "RouterAgent routed to vision analysis",
                    "router_action": state.router_action,
                }
            elif action_type == "contextual":
                logger.info(
                    "Converting RouterAgent 'contextual' to classification format"
                )
                state.classification = {
                    "intent_type": "contextual",
                    "confidence": 0.95,
                    "needs_satellite_data": False,
                    "needs_vision_analysis": False,
                    "needs_contextual_info": True,
                    "reasoning": "RouterAgent routed to contextual/knowledge response",
                    "router_action": state.router_action,
                }
            elif action_type == "hybrid":
                logger.info("Converting RouterAgent 'hybrid' to classification format")
                state.classification = {
                    "intent_type": "hybrid",
                    "confidence": 0.95,
                    "needs_satellite_data": True,
                    "needs_vision_analysis": True,
                    "needs_contextual_info": False,
                    "reasoning": "RouterAgent routed to hybrid (STAC + vision)",
                    "router_action": state.router_action,
                }
            elif action_type == "gis_feature_query":
                _vector_base = os.getenv(
                    "VECTOR_API_URL", "http://vector-api:8080"
                ).rstrip("/")
                collection = state.router_action.get("collection", "")
                feature_id = state.router_action.get("feature_id", "")
                try:
                    async with aiohttp.ClientSession() as _sess:
                        async with _sess.get(
                            f"{_vector_base}/parquet/collections/{collection}/items/{feature_id}",
                            headers={"Accept": "application/geo+json"},
                        ) as _fr:
                            feature = await _fr.json()
                        async with _sess.get(
                            f"{_stac_base}/collections",
                            headers={"Accept": "application/json"},
                        ) as _sr:
                            stac = await _sr.json()
                    props = feature.get("properties", {})
                    cols = [c["id"] for c in stac.get("collections", [])]
                    synthesised_query = (
                        f"Feature {feature_id} from collection {collection}. "
                        f"Attributes: {props}. "
                        f"Platform STAC collections: {', '.join(cols)}. "
                        f"Please analyse this feature."
                    )
                    state.router_action = {
                        "action_type": "contextual",
                        "original_query": synthesised_query,
                        "needs_stac_search": False,
                        "needs_vision_analysis": False,
                    }
                except Exception as _e:
                    logger.warning(f"GIS feature query failed: {_e}")
                state.classification = {
                    "intent_type": "contextual",
                    "confidence": 0.95,
                    "needs_satellite_data": False,
                    "needs_vision_analysis": False,
                    "needs_contextual_info": True,
                    "reasoning": "RouterAgent routed to GIS platform feature query",
                    "router_action": state.router_action,
                }
            # Note: navigate_to and extreme_weather return early before this point
            # Safety net: if navigate_to somehow didn't return early, treat as contextual
            if action_type == "navigate_to":
                logger.warning(
                    "[WARN] navigate_to reached classification bridge (should have returned early). Treating as contextual."
                )
                state.classification = {
                    "intent_type": "contextual",
                    "confidence": 0.7,
                    "needs_satellite_data": False,
                    "needs_vision_analysis": False,
                    "needs_contextual_info": True,
                    "reasoning": "navigate_to fallthrough safety net",
                    "router_action": state.router_action,
                }

        state.intent_type = (
            state.classification.get("intent_type", "stac")
            if state.classification
            else None
        )
        logger.info(
            f"[SEARCH] SEMANTIC_KERNEL_AVAILABLE: {app_state.SEMANTIC_KERNEL_AVAILABLE}"
        )
        if app_state.SEMANTIC_KERNEL_AVAILABLE and app_state.global_translator:
            try:
                state.translator = app_state.global_translator

                # ========================================================================
                # [BRAIN] UNIFIED INTENT CLASSIFICATION - Single GPT-5 call
                # ========================================================================
                # Intent Types: vision | stac | hybrid | contextual
                # - vision: Analyze currently visible imagery (no new data loading)
                # - stac: Load new satellite imagery only (no analysis)
                # - hybrid: Load new imagery AND analyze it (sequential operation)
                # - contextual: Information/education only (no map interaction)
                # ========================================================================

                # ========================================================================
                # CLASSIFICATION SOURCE: RouterAgent is the SINGLE source of truth
                # ========================================================================
                # RouterAgent has already classified the query and set `classification`.
                # No fallback to legacy Intent Classifier needed.
                # If RouterAgent fails (router_action is None), default to STAC search.
                # ========================================================================
                if not state.classification:
                    # RouterAgent failed or wasn't available - default to STAC search
                    logger.warning(
                        "RouterAgent did not provide classification, defaulting to STAC search"
                    )
                    state.classification = {
                        "intent_type": "stac",
                        "confidence": 0.5,
                        "needs_satellite_data": True,
                        "needs_vision_analysis": False,
                        "needs_contextual_info": False,
                        "reasoning": "Fallback: RouterAgent unavailable",
                        "modules": [],
                    }
                else:
                    logger.info("Using RouterAgent classification")

                state.intent_type = state.classification.get(
                    "intent_type", "stac"
                )  # Default to STAC query
                state.confidence = state.classification.get("confidence", 0)
                modules_to_execute = state.classification.get("modules", [])

                logger.info("CLASSIFICATION RESULT (from RouterAgent):")
                logger.info(f"   Query: '{state.natural_query}'")
                logger.info(f"   Intent: {state.intent_type}")
                logger.info(f"   Confidence: {state.confidence:.2f}")
                logger.info(
                    f"   Needs satellite data: {state.classification.get('needs_satellite_data', False)}"
                )
                logger.info(
                    f"   Needs contextual info: {state.classification.get('needs_contextual_info', False)}"
                )
                logger.info(
                    f"   Needs vision analysis: {state.classification.get('needs_vision_analysis', False)}"
                )

                # TRACE: Classification result
                log_pipeline_step(
                    state.pipeline_session_id,
                    "CLASSIFICATION",
                    "OUTPUT",
                    {
                        "intent_type": state.intent_type,
                        "confidence": round(state.confidence, 2),
                        "modules": modules_to_execute,
                        "needs_satellite_data": state.classification.get(
                            "needs_satellite_data", False
                        ),
                        "needs_vision": state.classification.get(
                            "needs_vision_analysis", False
                        ),
                        "reasoning": state.classification.get("reasoning", "N/A")[
                            :100
                        ],  # Truncate for readability
                    },
                )

                # Note: GEOINT mode is now UI-driven via /api/geoint/mobility endpoint
                # No query-based detection needed - all triggered by toggle + pin drop

                # ========================================================================
                # CHAT VISION ANALYSIS - Priority Detection
                # ========================================================================
                # RouterAgent classification is the SOURCE OF TRUTH for intent.
                # The keyword detector is a FALLBACK only — it must NOT override
                # the Router when the Router explicitly classified as stac_search.
                #
                # Bug this fixes: "Show me Sentinel-2 imagery here" after navigating
                # to Canada. The keyword detector sees "show me" + "sentinel" in
                # conversation history and flags it as vision. But the Router correctly
                # classified it as stac_search (has_collection=true). The keyword
                # detector was hijacking STAC queries into the Vision path, which
                # then fails with "No STAC items available to sample" because no
                # imagery has been loaded yet.
                # ========================================================================

                from agents import get_vision_agent  # EnhancedVisionAgent with 5 tools

                vision_agent = get_vision_agent()
                conversation_history = state.req_body.get(
                    "conversation_history", []
                ) or state.req_body.get("messages", [])

                # STEP 1: Check if Router explicitly classified as STAC search
                # If so, do NOT let keyword detector override — the user wants to LOAD data, not analyze it
                router_says_stac = (
                    state.classification
                    and state.classification.get("intent_type") == "stac"
                )

                if router_says_stac:
                    needs_vision = False
                    logger.info(
                        "[CHART] RouterAgent classified as STAC search — skipping vision keyword detection"
                    )
                else:
                    # STEP 2: Use keyword detector for non-STAC queries
                    from geoint.chat_vision_analyzer import get_chat_vision_analyzer

                    chat_vision_detector = get_chat_vision_analyzer()
                    needs_vision = chat_vision_detector.should_use_vision(
                        state.natural_query, conversation_history
                    )

                    if needs_vision:
                        logger.info(
                            "[SEARCH] Vision query detected by KEYWORD/CONTEXT matching (priority check)"
                        )
                    else:
                        # STEP 3: Fallback to classification if keywords didn't match
                        # This catches edge cases where user doesn't use explicit vision keywords
                        needs_vision = state.classification.get(
                            "needs_vision_analysis", False
                        )
                        if needs_vision:
                            logger.info(
                                "[SEARCH] Vision query detected by GPT CLASSIFICATION (fallback check)"
                            )

                # ========================================================================
                # [LAUNCH] PARALLEL PROCESSING: Vision + Data for Hybrid Queries
                # ========================================================================
                # For hybrid queries that need both vision analysis AND STAC data,
                # we can run them in parallel to save ~5-8 seconds
                # ========================================================================

                if needs_vision:
                    logger.info(
                        "[IMG] VISION QUERY DETECTED: User is asking about visible imagery"
                    )

                    # Check if frontend provided map context
                    map_bounds = state.req_body.get(
                        "map_bounds"
                    )  # {north, south, east, west, center_lat, center_lng}
                    imagery_url = state.req_body.get(
                        "imagery_url"
                    )  # Current visible imagery URL
                    imagery_base64 = state.req_body.get(
                        "imagery_base64"
                    )  # Base64 screenshot from frontend
                    current_collection = state.req_body.get(
                        "current_collection"
                    )  # Collection ID (string)
                    tile_urls = state.req_body.get(
                        "tile_urls", []
                    )  # TiTiler URLs from prior STAC response
                    has_satellite_data = state.req_body.get(
                        "has_satellite_data", False
                    )  # Flag if STAC imagery loaded
                    conversation_history = state.req_body.get(
                        "conversation_history", []
                    ) or state.req_body.get("messages", [])

                    # Initialize collections_list (will be populated from session context or current_collection)
                    collections_list = []

                    # ================================================================
                    # ENHANCED CONTEXT: Include session history from router
                    # ================================================================
                    # The Vision Agent should have full context from the session:
                    # - Previous locations viewed
                    # - Collections previously loaded
                    # - Conversation history
                    # ================================================================
                    session_context = {}
                    stac_items_from_session = []
                    if app_state.router_agent and state.session_id:
                        session_context = (
                            app_state.router_agent.tools.session_contexts.get(
                                state.session_id, {}
                            )
                        )
                        logger.info(
                            f"[LIST] Session context for Vision Agent: {list(session_context.keys())}"
                        )

                        # Merge session collections with current collection
                        session_collections = session_context.get(
                            "last_collections", []
                        )
                        if session_collections and not collections_list:
                            collections_list = session_collections

                        # Get STAC items from session for raster analysis
                        stac_items_from_session = session_context.get(
                            "last_stac_items", []
                        )
                        if stac_items_from_session:
                            logger.info(
                                f"[PKG] Retrieved {len(stac_items_from_session)} STAC items from session for vision agent"
                            )

                        # ================================================================
                        # [PIN] SESSION BBOX OVERRIDE
                        # ================================================================
                        # Use session bbox in TWO cases:
                        # 1. Frontend sent no bounds at all (map_bounds is None)
                        # 2. User recently navigated (navigate_to set has_rendered_map=False)
                        #    but frontend bounds are STALE because the useEffect that
                        #    computes mapContext.bounds doesn't track camera position —
                        #    it only re-fires on satelliteData/visionMode changes.
                        #    After navigate_to, the camera moved but no dependency changed,
                        #    so frontend still sends the old US-center coordinates.
                        # ================================================================
                        session_bbox = session_context.get("last_bbox")
                        recently_navigated = session_bbox and not session_context.get(
                            "has_rendered_map", True
                        )

                        if session_bbox and (not map_bounds or recently_navigated):
                            bbox = session_bbox
                            map_bounds = {
                                "west": bbox[0],
                                "south": bbox[1],
                                "east": bbox[2],
                                "north": bbox[3],
                                "center_lat": (bbox[1] + bbox[3]) / 2,
                                "center_lng": (bbox[0] + bbox[2]) / 2,
                            }
                            override_reason = (
                                "no frontend bounds"
                                if not map_bounds
                                else "post-navigate_to (frontend bounds stale)"
                            )
                            logger.info(
                                f"[PIN] Using session bbox for Vision Agent ({override_reason}): {session_context.get('last_location')}"
                            )

                    # Convert collection to list for Vision Agent (typically one collection per query)
                    if (
                        current_collection
                        and current_collection not in collections_list
                    ):
                        collections_list = [current_collection] + collections_list

                    # Log vision context availability
                    logger.info(
                        f"[SNAP] Map screenshot available: {bool(imagery_base64)}"
                    )
                    logger.info(f"[SAT] Satellite data loaded: {has_satellite_data}")
                    logger.info(f"[MAP] Map bounds available: {bool(map_bounds)}")

                    # Check if this is a HYBRID query (vision + data)
                    needs_data = state.classification.get("needs_satellite_data", False)
                    is_hybrid_vision = needs_vision and needs_data

                    if is_hybrid_vision:
                        logger.info(
                            "[SHUFFLE] HYBRID VISION + DATA QUERY: Will run vision + STAC in PARALLEL for optimal performance"
                        )

                    if map_bounds or imagery_url or imagery_base64:
                        try:
                            # For hybrid queries, we'll start vision analysis but continue to STAC
                            # Both will run in parallel (vision task created, STAC search proceeds)
                            state.vision_task = None

                            if is_hybrid_vision:
                                # Create vision agent task (runs in background)
                                logger.info(
                                    "[FAST] Starting Vision Agent task (parallel execution)..."
                                )
                                state.vision_task = asyncio.create_task(
                                    vision_agent.analyze(
                                        user_query=state.natural_query,
                                        session_id=state.session_id,
                                        imagery_base64=imagery_base64,
                                        map_bounds=map_bounds or {},
                                        collections=collections_list,
                                        tile_urls=tile_urls,
                                        stac_items=stac_items_from_session,
                                        conversation_history=conversation_history,
                                    )
                                )
                                # Don't await yet - let it run in parallel with STAC search
                                logger.info(
                                    "[SYNC] Vision Agent task created, continuing to STAC data retrieval (parallel)..."
                                )
                            else:
                                # Pure vision query - invoke Vision Agent and return immediately
                                logger.info(
                                    "[BOT] Invoking Vision Agent for pure vision query"
                                )
                                logger.info(f"   Collections: {collections_list}")
                                logger.info(f"   Tile URLs: {len(tile_urls)} tiles")
                                logger.info(
                                    f"   STAC items: {len(stac_items_from_session)} items"
                                )
                                logger.info(
                                    f"   Has screenshot: {bool(imagery_base64)}"
                                )
                                logger.info(f"   Has map_bounds: {bool(map_bounds)}")

                                # EnhancedVisionAgent with tools - pass full context including STAC items
                                vision_result = await vision_agent.analyze(
                                    user_query=state.natural_query,
                                    session_id=state.session_id,
                                    imagery_base64=imagery_base64,
                                    map_bounds=map_bounds or {},
                                    collections=collections_list,
                                    tile_urls=tile_urls,
                                    stac_items=stac_items_from_session,
                                    conversation_history=conversation_history,
                                )

                                # ================================================================
                                # [SEARCH] DEBUG: Log vision_result to trace issues
                                # ================================================================
                                logger.info(
                                    f"[SEARCH] Vision Agent returned: {type(vision_result)}"
                                )
                                if vision_result:
                                    logger.info(
                                        f"   Keys: {list(vision_result.keys())}"
                                    )
                                    logger.info(
                                        f"   Response: '{str(vision_result.get('response', ''))[:100]}...'"
                                    )
                                    logger.info(
                                        f"   Analysis: '{str(vision_result.get('analysis', ''))[:100]}...'"
                                    )

                                # Check for response (or analysis as fallback)
                                response_text = (
                                    vision_result.get("response")
                                    or vision_result.get("analysis")
                                    if vision_result
                                    else None
                                )

                                if response_text:
                                    logger.info("[OK] Vision Agent analysis completed")
                                    logger.info(
                                        f"   Cached: {vision_result.get('cached', False)}"
                                    )

                                    # ================================================================
                                    # VISION AGENT TRACING: Log tool usage for debugging
                                    # ================================================================
                                    tools_used = vision_result.get("tools_used", [])
                                    tool_calls = vision_result.get("tool_calls", [])
                                    logger.info(
                                        f"[TOOL] Vision Agent tools used: {tools_used}"
                                    )

                                    # Log to pipeline trace
                                    log_pipeline_step(
                                        state.pipeline_session_id,
                                        "VISION_AGENT",
                                        "OUTPUT",
                                        {
                                            "tools_used": tools_used,
                                            "tool_calls": tool_calls,
                                            "has_screenshot": vision_result.get(
                                                "context", {}
                                            ).get("has_screenshot", False),
                                            "collections": vision_result.get(
                                                "context", {}
                                            ).get("collections", []),
                                            "response_length": len(response_text),
                                        },
                                    )

                                    return {
                                        "success": True,
                                        "response": response_text,
                                        "vision_analysis": vision_result,
                                        "processing_type": "vision_agent",
                                        "timestamp": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                    }
                                else:
                                    # ================================================================
                                    # [ALERT] VISION AGENT FAILED - Return error, DON'T fall through to STAC
                                    # ================================================================
                                    logger.warning(
                                        f"[WARN] Vision Agent returned empty response. Result: {vision_result}"
                                    )
                                    log_pipeline_step(
                                        state.pipeline_session_id,
                                        "VISION_AGENT",
                                        "ERROR",
                                        {
                                            "error": "Empty response from Vision Agent",
                                            "vision_result": (
                                                str(vision_result)[:500]
                                                if vision_result
                                                else "None"
                                            ),
                                        },
                                    )

                                    # Return a helpful error message instead of falling through to STAC
                                    return {
                                        "success": False,
                                        "response": "I tried to analyze the map but couldn't generate a response. Please try asking your question again.",
                                        "error": "Vision Agent returned empty response",
                                        "processing_type": "vision_agent_error",
                                        "timestamp": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                    }
                        except Exception as e:
                            logger.warning(
                                f"[WARN] Chat vision analysis failed: {e}, returning error..."
                            )
                            logger.error(traceback.format_exc())

                            # Return error, DON'T fall through to STAC for vision queries
                            return {
                                "success": False,
                                "response": "I encountered an error analyzing the imagery.",
                                "error": "internal error",
                                "processing_type": "vision_agent_error",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                    else:
                        # ================================================================
                        # NO MAP CONTEXT: Still invoke Vision Agent with LLM tool
                        # ================================================================
                        # The Vision Agent can use its query_knowledge tool to provide
                        # a helpful response even without map context. This is better than
                        # just returning an error message.
                        # ================================================================
                        logger.info(
                            "ℹ️ No map screenshot, but Vision Agent can use LLM tool"
                        )
                        try:
                            vision_result = await vision_agent.analyze(
                                user_query=state.natural_query,
                                session_id=state.session_id,
                                imagery_base64=None,
                                map_bounds=map_bounds or {},
                                collections=collections_list,
                                conversation_history=conversation_history,
                            )

                            # [SEARCH] DEBUG: Log vision_result
                            logger.info(
                                f"[SEARCH] Vision Agent (LLM mode) returned: {type(vision_result)}"
                            )
                            if vision_result:
                                logger.info(f"   Keys: {list(vision_result.keys())}")
                                logger.info(
                                    f"   Response: '{str(vision_result.get('response', ''))[:100]}...'"
                                )

                            # Check for response (or analysis as fallback)
                            response_text = (
                                vision_result.get("response")
                                or vision_result.get("analysis")
                                if vision_result
                                else None
                            )

                            if response_text:
                                logger.info("[OK] Vision Agent (LLM mode) completed")

                                # ================================================================
                                # VISION AGENT TRACING: Log tool usage for debugging
                                # ================================================================
                                tools_used = vision_result.get("tools_used", [])
                                tool_calls = vision_result.get("tool_calls", [])
                                logger.info(
                                    f"[TOOL] Vision Agent (LLM mode) tools used: {tools_used}"
                                )

                                # Log to pipeline trace
                                log_pipeline_step(
                                    state.pipeline_session_id,
                                    "VISION_AGENT_LLM",
                                    "OUTPUT",
                                    {
                                        "tools_used": tools_used,
                                        "tool_calls": tool_calls,
                                        "has_screenshot": False,
                                        "collections": collections_list,
                                        "response_length": len(response_text),
                                    },
                                )

                                return {
                                    "success": True,
                                    "response": response_text,
                                    "vision_analysis": vision_result,
                                    "processing_type": "vision_agent_llm",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }
                            else:
                                # ================================================================
                                # [ALERT] VISION AGENT LLM MODE FAILED - Return helpful message
                                # ================================================================
                                logger.warning(
                                    f"[WARN] Vision Agent (LLM mode) returned empty. Result: {vision_result}"
                                )

                                # For "what is on the map" without screenshot, give a helpful response
                                return {
                                    "success": True,
                                    "response": "I can see you're asking about what's on the map. I can see satellite imagery is displayed. Could you ask a more specific question about what you'd like to know, such as 'What city is shown?' or 'What features can you identify?'",
                                    "processing_type": "vision_agent_no_context",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }
                        except Exception as e:
                            logger.warning(
                                f"[WARN] Vision Agent LLM fallback failed: {e}"
                            )
                            logger.error(traceback.format_exc())

                            # Return helpful message instead of falling through to STAC
                            return {
                                "success": True,
                                "response": "I can see satellite imagery on the map but couldn't analyze it in detail. Please try asking a specific question about the location or features you see.",
                                "error": "internal error",
                                "processing_type": "vision_agent_error",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }

                # For contextual queries, skip STAC search and generate direct educational response
                # intent_type = "contextual" for information/education queries (e.g., "How do hurricanes form?")
                if (
                    state.intent_type == "contextual"
                    and state.confidence > 0.8
                    and not state.classification.get("needs_satellite_data", False)
                ):
                    logger.info(
                        "[MSG] CONTEXTUAL REQUEST: Skipping STAC search, generating educational response..."
                    )

                    # ================================================================
                    # LOCATION CONTEXT INJECTION: Enrich query with session location
                    # ================================================================
                    # If the user asks "what is on the map" after navigating to Tokyo,
                    # the contextual handler has no idea where the user is looking.
                    # We inject the session location so the LLM generates a correct answer.
                    # ================================================================
                    contextual_query = state.natural_query
                    if app_state.router_agent and state.session_id:
                        ctx = app_state.router_agent.tools.session_contexts.get(
                            state.session_id, {}
                        )
                        session_location = ctx.get("last_location")
                        session_bbox = ctx.get("last_bbox")
                        current_map_bounds = state.req_body.get("map_bounds")
                        if session_location or session_bbox or current_map_bounds:
                            location_hint = session_location or ""
                            if not location_hint and current_map_bounds:
                                location_hint = f"({current_map_bounds.get('center_lat', 'N/A')}, {current_map_bounds.get('center_lng', 'N/A')})"
                            elif not location_hint and session_bbox:
                                center_lat = (session_bbox[1] + session_bbox[3]) / 2
                                center_lng = (session_bbox[0] + session_bbox[2]) / 2
                                location_hint = f"({center_lat:.4f}, {center_lng:.4f})"

                            if location_hint:
                                contextual_query = f"{state.natural_query} [The user is currently viewing the map at: {location_hint}]"
                                logger.info(
                                    f"[PIN] Enriched contextual query with location: {location_hint}"
                                )

                    try:
                        contextual_response = await asyncio.wait_for(
                            state.translator.generate_contextual_earth_science_response(
                                contextual_query,
                                state.classification,
                                {
                                    "success": True,
                                    "results": {"features": []},
                                },  # Empty STAC response for contextual-only
                            ),
                            timeout=30.0,
                        )

                        if contextual_response and contextual_response.get("message"):
                            logger.info(
                                "[OK] Generated contextual response successfully"
                            )
                            return {
                                "success": True,
                                "response": contextual_response.get("message"),
                                "query_classification": {
                                    "intent_type": state.intent_type,
                                    "confidence": state.confidence,
                                    "needs_satellite_data": False,
                                    "needs_contextual_info": True,
                                },
                                "processing_type": "contextual_only",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                    except Exception as e:
                        logger.warning(
                            f"[WARN] Contextual response generation failed: {e}, proceeding with STAC search..."
                        )

            except Exception as e:
                logger.error(f"[FAIL] Early classification failed: {e}")
                state.classification = None

        # PHASE 1: SEMANTIC TRANSLATION TO STAC PARAMETERS
        # ========================================================================
        # [GEAR] CONDITIONAL COLLECTION MAPPING: Only run for STAC and Hybrid queries
        # ========================================================================
        # Skip collection mapping for:
        # - "vision" queries (analyze existing imagery, no new data needed)
        # - "contextual" queries (information only, no map interaction)
        # Run collection mapping for:
        # - "stac" queries (load new imagery)
        # - "hybrid" queries (load new imagery + analyze)
        # ========================================================================
        state.stac_params = None
        state.stac_query = None
        state.response_message = "I can help you find Earth science data, but I need more specific information."

        # Check if we need to run collection mapping based on intent
        skip_collection_mapping = False
        if state.classification and state.intent_type:
            if state.intent_type in ["vision", "contextual"]:
                skip_collection_mapping = True
                logger.info(
                    f"[SKIP] Skipping collection mapping for {state.intent_type} query (no STAC search needed)"
                )

        if not skip_collection_mapping:
            if not app_state.SEMANTIC_KERNEL_AVAILABLE:
                logger.warning(
                    "[WARN] Semantic Kernel not available, using fallback processing"
                )

                # Fallback: Simple collection detection (like Router Function App)
                collections = detect_collections(state.natural_query)
                state.stac_params = {
                    "collections": collections,
                    "limit": 20,  # PERFORMANCE: Reduced from 100 to 20 for faster queries (3-5s improvement)
                    "original_query": state.natural_query,
                }

                if collections:
                    state.stac_query = build_stac_query(state.stac_params)
                    logger.info(
                        f"[TOOL] Built fallback STAC query: {json.dumps(state.stac_query, indent=2)}"
                    )

            elif app_state.SEMANTIC_KERNEL_AVAILABLE and state.translator:
                try:
                    logger.info(
                        f"[BOT] Starting multi-agent pipeline — query='{state.natural_query}' pin={state.pin}"
                    )

                    # ========================================================================
                    # [PIN] SESSION BBOX: Fallback location for queries without explicit location
                    # ========================================================================
                    # Always pass the session's last_bbox as a fallback. The translate_query
                    # method will use it if Agent 2 doesn't extract a location from the query.
                    # This makes location handling more deterministic and less reliant on the
                    # LLM correctly setting use_current_location=True.
                    # ========================================================================
                    session_bbox = None
                    session_location = None

                    # Get session context from router_agent (session_contexts is on router_agent.tools)
                    if app_state.router_agent and state.session_id:
                        try:
                            session_context = (
                                app_state.router_agent.tools.session_contexts.get(
                                    state.session_id, {}
                                )
                            )
                            session_bbox = session_context.get("last_bbox")
                            session_location = session_context.get("last_location")
                            if session_bbox:
                                logger.info(
                                    f"[PIN] Session fallback available: {session_location} bbox={session_bbox}"
                                )
                        except AttributeError:
                            logger.debug(
                                "[PIN] No session context available (router_agent.tools.session_contexts not found)"
                            )

                    # If router explicitly said use_current_location, prioritize it
                    if state.router_action and state.router_action.get(
                        "use_current_location"
                    ):
                        logger.info("[PIN] Router set use_current_location=True")

                    logger.info(
                        "[SYNC] Translating natural language to STAC parameters..."
                    )

                    # Use the semantic translator's translate_query method with pin and session_bbox fallback
                    state.stac_params = await state.translator.translate_query(
                        state.natural_query,
                        pin_location=state.pin,
                        session_bbox=session_bbox,
                    )

                    # ========================================================================
                    # [ALERT] LOCATION VALIDATION: Check if location is required but missing
                    # ========================================================================
                    if (
                        state.stac_params
                        and state.stac_params.get("error") == "LOCATION_REQUIRED"
                    ):
                        logger.warning(
                            "[WARN] Location required but not found in query"
                        )
                        return {
                            "success": False,
                            "error": "LOCATION_REQUIRED",
                            "response": state.stac_params.get(
                                "message", "Please specify a location for your search."
                            ),
                            "suggestions": state.stac_params.get("suggestions", []),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    # ========================================================================

                    if state.stac_params and state.stac_params.get("collections"):
                        logger.info(
                            f"[OK] Translation successful: {len(state.stac_params.get('collections', []))} collections selected"
                        )

                        # [MICRO] TRACE: Semantic translation result
                        log_pipeline_step(
                            state.pipeline_session_id,
                            "SEMANTIC_TRANSLATION",
                            "OUTPUT",
                            {
                                "collections": state.stac_params.get("collections", []),
                                "bbox": state.stac_params.get("bbox"),
                                "datetime": state.stac_params.get("datetime"),
                                "location_name": state.stac_params.get(
                                    "location_name", "N/A"
                                ),
                            },
                        )

                        # Build STAC query from semantic analysis (like Router Function App)
                        state.stac_query = build_stac_query(state.stac_params)
                        logger.info(
                            f"[TOOL] Built STAC query from params: {json.dumps(state.stac_query, indent=2)}"
                        )

                        # ========================================================================
                        # [DATE] DATETIME SAFETY NET: Regex fallback if GPT datetime agent failed
                        # ========================================================================
                        # If the query mentions a date but stac_query has no datetime filter,
                        # use regex extraction as a deterministic fallback. This catches cases
                        # where the GPT datetime_translation_agent returns None or fails.
                        # ========================================================================
                        if not state.stac_query.get("datetime") and state.natural_query:
                            import calendar as cal_mod
                            import re

                            query_lower = state.natural_query.lower()
                            month_names = {
                                "january": 1,
                                "february": 2,
                                "march": 3,
                                "april": 4,
                                "may": 5,
                                "june": 6,
                                "july": 7,
                                "august": 8,
                                "september": 9,
                                "october": 10,
                                "november": 11,
                                "december": 12,
                            }
                            extracted_datetime = None

                            # Pattern 1: "from/in/for MONTH YEAR" (e.g., "from December 2018")
                            month_year_match = re.search(
                                r"(?:from|in|for|of)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})",
                                query_lower,
                            )
                            if month_year_match:
                                m_name, y_str = month_year_match.group(
                                    1
                                ), month_year_match.group(2)
                                m_num = month_names[m_name]
                                y_num = int(y_str)
                                last_day = cal_mod.monthrange(y_num, m_num)[1]
                                extracted_datetime = f"{y_str}-{m_num:02d}-01/{y_str}-{m_num:02d}-{last_day:02d}"

                            # Pattern 2: "MONTH YEAR" without preposition (e.g., "December 2018")
                            if not extracted_datetime:
                                month_year_match2 = re.search(
                                    r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})",
                                    query_lower,
                                )
                                if month_year_match2:
                                    m_name, y_str = month_year_match2.group(
                                        1
                                    ), month_year_match2.group(2)
                                    m_num = month_names[m_name]
                                    y_num = int(y_str)
                                    last_day = cal_mod.monthrange(y_num, m_num)[1]
                                    extracted_datetime = f"{y_str}-{m_num:02d}-01/{y_str}-{m_num:02d}-{last_day:02d}"

                            # Pattern 3: "from/in/for YEAR" (e.g., "from 2018")
                            if not extracted_datetime:
                                year_match = re.search(
                                    r"(?:from|in|for)\s+((?:19|20)\d{2})\b", query_lower
                                )
                                if year_match:
                                    y_str = year_match.group(1)
                                    extracted_datetime = f"{y_str}-01-01/{y_str}-12-31"

                            if extracted_datetime:
                                state.stac_query["datetime"] = extracted_datetime
                                state.stac_params["datetime"] = extracted_datetime
                                logger.info(
                                    f"[DATE] REGEX FALLBACK: Extracted datetime '{extracted_datetime}' from query (GPT agent missed it)"
                                )

                    else:
                        logger.warning(
                            f"[WARN] Translation did not produce valid STAC parameters. Got: {state.stac_params}"
                        )

                        # [MICRO] TRACE: Translation failed
                        log_pipeline_step(
                            state.pipeline_session_id,
                            "SEMANTIC_TRANSLATION",
                            "ERROR",
                            {
                                "error": "No valid collections returned",
                                "raw_params": str(state.stac_params)[:200],
                            },
                        )

                except Exception as e:
                    logger.error(
                        f"[FAIL] Semantic translation failed: {e}", exc_info=True
                    )
                    logger.error(f"[FAIL] Full traceback: {traceback.format_exc()}")
                    state.stac_params = None
                    state.stac_query = None

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
