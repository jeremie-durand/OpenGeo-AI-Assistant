"""Unified natural-language query endpoint and comparison query endpoint."""

import asyncio
import logging
import traceback
from datetime import datetime
from typing import Any, Dict

import aiohttp
import app_state
from fastapi import APIRouter, HTTPException
from quickstart_cache import is_quickstart_query
from request_models import ComparisonQueryRequest, QueryRequest
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
from services.query_pipeline.response import (
    build_complete_response,
    record_render_in_session,
)
from services.query_pipeline.routing import (
    classify_router_action,
    route_with_agent,
)
from services.query_pipeline.search import run_stac_search
from services.query_pipeline.state import QueryState
from services.query_pipeline.translation import translate_to_stac

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/query")
async def unified_query_processor(body: QueryRequest) -> Dict[str, Any]:
    """Answer a natural-language query by running the services.query_pipeline stages.

    Order: intake, quickstart fast path, RouterAgent, early intents (vision,
    contextual), translation, STAC search, message, tile rendering, response.
    """
    try:
        state = QueryState()
        start_query(body, state)

        if is_quickstart_query(state.natural_query):
            return await run_quickstart_fast_path(state)

        apply_session_bbox_override(state)

        early_response = await route_with_agent(state)
        if early_response is not None:
            return early_response

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

        early_response = await translate_to_stac(state)
        if early_response is not None:
            return early_response

        await run_stac_search(state)
        log_and_sync_features(state)
        await build_response_message(state)
        await render_tiles(state)

        complete_response = build_complete_response(state)
        record_render_in_session(state)

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
