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
from services.pipeline_trace import (
    get_pipeline_trace,
    log_pipeline_step,
)
from services.query_pipeline.intake import (
    apply_session_bbox_override,
    start_query,
)
from services.query_pipeline.intents import run_early_intents
from services.query_pipeline.messages import (
    build_response_message,
    log_and_sync_features,
)
from services.query_pipeline.quickstart import run_quickstart_fast_path
from services.query_pipeline.rendering import render_tiles
from services.query_pipeline.response import summarize_stac_items
from services.query_pipeline.routing import (
    classify_router_action,
    route_with_agent,
)
from services.query_pipeline.search import run_stac_search
from services.query_pipeline.state import QueryState
from services.query_pipeline.translation import translate_to_stac
from services.tile_urls import clean_tilejson_urls

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
        await run_stac_search(state)

        # PHASE 3: GENERATE RESPONSE MESSAGE
        log_and_sync_features(state)

        await build_response_message(state)

        await render_tiles(state)

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
