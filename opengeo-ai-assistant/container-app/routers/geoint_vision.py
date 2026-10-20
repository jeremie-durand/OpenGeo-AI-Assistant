"""GEOINT vision analysis and vision chat endpoints."""

import logging
import traceback
from datetime import datetime, timezone

import app_state
from fastapi import APIRouter, HTTPException
from request_models import VisionChatRequest, VisionRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/geoint/vision")
async def geoint_vision_analysis(body: VisionRequest):
    """
    GEOINT Vision Analysis - Unified endpoint for initial analysis AND follow-ups

    This endpoint supports:
    1. Initial vision analysis (no session_id) - Screenshot + raster analysis
    2. Follow-up questions (with session_id) - Uses VisionAgent with conversation memory

    Request body:
    {
        "latitude": float,
        "longitude": float,
        "screenshot": str (base64 screenshot),
        "user_query": str (optional - user question),
        "radius_miles": float (optional - defaults to 5.0),
        "session_id": str (optional - provide for follow-up questions to maintain context)
    }

    Response includes session_id that can be used for follow-up questions.
    """
    import uuid

    request_id = f"vision-{datetime.now(timezone.utc).timestamp()}"
    try:
        logger.info(
            f"[EYE] [{request_id}] ============================================================"
        )
        logger.info(f"[EYE] [{request_id}] VISION ENDPOINT CALLED")
        logger.info(
            f"[EYE] [{request_id}] ============================================================"
        )

        latitude = body.latitude
        longitude = body.longitude
        screenshot = body.screenshot
        user_query = body.user_query
        radius_miles = body.radius_miles
        session_id = body.session_id
        tile_urls_from_request = body.tile_urls
        collection_from_request = body.collection
        map_bounds_from_request = body.map_bounds
        stac_items_from_request = body.stac_items
        analysis_type = body.analysis_type

        screenshot_size = len(screenshot) if screenshot else 0
        logger.info(f"[EYE] [{request_id}] Screenshot: {screenshot_size / 1024:.1f} KB")
        logger.info(f"[EYE] [{request_id}] Session ID: {session_id or 'NEW SESSION'}")
        logger.info(f"[EYE] [{request_id}] User query: {user_query}")
        logger.info(
            f"[EYE] [{request_id}] Tile URLs from request: {len(tile_urls_from_request)}"
        )
        logger.info(
            f"[EYE] [{request_id}] STAC items from request: {len(stac_items_from_request)}"
        )
        logger.info(
            f"[EYE] [{request_id}] Collection from request: {collection_from_request}"
        )
        if analysis_type:
            logger.info(f"[TARGET] [{request_id}] Analysis type hint: {analysis_type}")

        # Validate required parameters
        if latitude is None or longitude is None:
            raise HTTPException(
                status_code=400, detail="latitude and longitude are required"
            )

        if not screenshot:
            raise HTTPException(
                status_code=400, detail="screenshot is required for vision analysis"
            )

        # Validate coordinates
        if not (-90 <= latitude <= 90):
            raise HTTPException(status_code=400, detail=f"Invalid latitude: {latitude}")
        if not (-180 <= longitude <= 180):
            raise HTTPException(
                status_code=400, detail=f"Invalid longitude: {longitude}"
            )

        logger.info(f"[EYE] [{request_id}] Coordinates: ({latitude}, {longitude})")
        logger.info(f"[EYE] [{request_id}] Radius: {radius_miles} miles")

        import time

        agent_start = time.time()

        # Get Vision Agent (SK Agent with memory and vision tools)
        from agents import get_vision_agent

        vision_agent = get_vision_agent()

        # Create session_id if not provided
        if not session_id:
            session_id = str(uuid.uuid4())
            logger.info(f"[EYE] [{request_id}] Created new session: {session_id}")

        # Build the message for the agent
        if not user_query:
            user_query = (
                "Analyze this location. What can you identify from the imagery?"
            )

        # Gather STAC context - prefer request data over session context
        # Use bounds from request if provided, otherwise build from coordinates
        if map_bounds_from_request:
            map_bounds = map_bounds_from_request
            # Ensure pin coordinates are set (this is a pin-based analysis)
            # Pin coordinates take priority for sampling
            map_bounds["pin_lat"] = latitude
            map_bounds["pin_lng"] = longitude
            # Also ensure center coordinates are present as fallback
            if "center_lat" not in map_bounds:
                map_bounds["center_lat"] = latitude
            if "center_lng" not in map_bounds:
                map_bounds["center_lng"] = longitude
        else:
            # No map bounds from request - build from pin coordinates
            map_bounds = {
                "pin_lat": latitude,
                "pin_lng": longitude,
                "center_lat": latitude,
                "center_lng": longitude,
            }

        logger.info(
            f"[EYE] [{request_id}] Pin coordinates set: ({map_bounds.get('pin_lat')}, {map_bounds.get('pin_lng')})"
        )

        # Get STAC context - prefer request data, fallback to session context
        collections_list = []
        stac_items_from_session = []
        tile_urls_for_agent = tile_urls_from_request  # Direct from frontend

        # Use collection from request if provided
        if collection_from_request:
            collections_list = [collection_from_request]

        # [CHART] PREFER STAC items from request (frontend has them with assets)
        # Only fallback to session context if request didn't provide STAC items
        if stac_items_from_request:
            stac_items_from_session = stac_items_from_request
            logger.info(
                f"[EYE] [{request_id}] Using {len(stac_items_from_request)} STAC items from request (with assets for NDVI)"
            )
        elif app_state.router_agent and session_id:
            # Fallback: Try to get STAC items from session context
            session_context = app_state.router_agent.tools.session_contexts.get(
                session_id, {}
            )
            if not collections_list:
                collections_list = session_context.get("last_collections", [])
            stac_items_from_session = session_context.get("last_stac_items", [])
            logger.info(
                f"[EYE] [{request_id}] Loaded session context: collections={collections_list}, stac_items={len(stac_items_from_session)}"
            )

        logger.info(
            f"[EYE] [{request_id}] Tile URLs for agent: {len(tile_urls_for_agent)}"
        )
        logger.info(
            f"[EYE] [{request_id}] STAC Items from session: {len(stac_items_from_session)}"
        )
        logger.info(f"[EYE] [{request_id}] Collections: {collections_list}")

        # Call the vision agent
        import asyncio

        try:
            result = await asyncio.wait_for(
                vision_agent.analyze(
                    user_query=user_query,
                    session_id=session_id,
                    imagery_base64=screenshot,
                    map_bounds=map_bounds,
                    collections=collections_list,
                    stac_items=stac_items_from_session,
                    tile_urls=tile_urls_for_agent,  # NEW: Pass tile URLs directly
                    conversation_history=[],
                    analysis_type=analysis_type,  # [TARGET] Pass hint from frontend
                ),
                timeout=240.0,
            )
        except asyncio.TimeoutError:
            agent_elapsed = time.time() - agent_start
            logger.error(
                f"[EYE] [{request_id}] [FAIL] AGENT TIMEOUT after {agent_elapsed:.1f}s"
            )
            raise HTTPException(
                status_code=504,
                detail=f"Vision analysis timed out after {agent_elapsed:.1f}s. Please try again.",
            )

        agent_elapsed = time.time() - agent_start
        logger.info(
            f"[EYE] [{request_id}] [OK] VISION AGENT COMPLETED ({agent_elapsed:.2f}s)"
        )

        response_text = result.get("response") or result.get("analysis")

        if not response_text:
            logger.warning(f"[EYE] [{request_id}] Empty response from vision agent")
            raise HTTPException(
                status_code=500, detail="Vision agent returned empty response"
            )

        return {
            "status": "success",
            "result": {
                "analysis": response_text,
                "tools_used": result.get("tools_used", []),
                "vision_data": result.get("vision_data", {}),
            },
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[EYE] [{request_id}] [FAIL] VISION ENDPOINT FAILED")
        logger.error(f"[EYE] [{request_id}] Error: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Vision analysis failed")


@router.post("/api/geoint/vision/chat")
async def geoint_vision_chat(body: VisionChatRequest):
    """
    [BOT] GEOINT Vision Agent Chat - Multi-turn conversation with memory

    Request body:
    {
        "session_id": str (required for follow-ups),
        "message": str (user's follow-up question),
        "latitude": float (optional - for context),
        "longitude": float (optional - for context),
        "screenshot": str (optional - updated screenshot),
        "tile_urls": list (optional - tile URLs from frontend),
        "collection": str (optional - current collection name)
    }
    """
    request_id = f"vision-chat-{datetime.now(timezone.utc).timestamp()}"
    try:
        logger.info(f"[MSG] [{request_id}] VISION CHAT endpoint called")

        session_id = body.session_id
        message = body.message
        latitude = body.latitude
        longitude = body.longitude
        screenshot = body.screenshot
        tile_urls_from_request = body.tile_urls
        collection_from_request = body.collection
        stac_items_from_request = body.stac_items
        analysis_type = body.analysis_type

        logger.info(f"[MSG] [{request_id}] Session: {session_id}")
        logger.info(f"[MSG] [{request_id}] Message: {message[:100]}")
        logger.info(
            f"[MSG] [{request_id}] Tile URLs from request: {len(tile_urls_from_request)}"
        )
        logger.info(
            f"[MSG] [{request_id}] STAC items from request: {len(stac_items_from_request)}"
        )

        if analysis_type:
            logger.info(
                f"[TARGET] [{request_id}] Analysis type hint from frontend: {analysis_type}"
            )

        # [SEARCH] DETAILED STAC ITEM LOGGING
        if stac_items_from_request:
            for i, item in enumerate(stac_items_from_request[:2]):
                logger.info(
                    f"[MSG] [{request_id}] STAC item {i}: id={item.get('id', 'unknown')}, collection={item.get('collection', 'unknown')}, assets={list(item.get('assets', {}).keys())[:5]}"
                )
        logger.info(
            f"[MSG] [{request_id}] Collection from request: {collection_from_request}"
        )

        from agents import get_vision_agent

        vision_agent = get_vision_agent()

        # Prepare context - ensure pin coordinates are set for point-based analysis
        map_bounds = None
        if latitude is not None and longitude is not None:
            map_bounds = {
                "pin_lat": latitude,  # Pin location takes priority for point sampling
                "pin_lng": longitude,
                "center_lat": latitude,  # Also set as center for fallback
                "center_lng": longitude,
            }
            logger.info(
                f"[MSG] [{request_id}] Pin coordinates set: ({latitude}, {longitude})"
            )

        # Get STAC context - prefer request data, fallback to session context
        collections_list = []
        stac_items_from_session = []
        tile_urls_for_agent = tile_urls_from_request

        # Use collection from request if provided
        if collection_from_request:
            collections_list = [collection_from_request]

        # Fallback to session context if no data from request
        if app_state.router_agent and session_id and not tile_urls_for_agent:
            session_context = app_state.router_agent.tools.session_contexts.get(
                session_id, {}
            )
            if not collections_list:
                collections_list = session_context.get("last_collections", [])
            stac_items_from_session = session_context.get("last_stac_items", [])

        # [CHART] CRITICAL: Prefer STAC items from request (has fresh assets/band URLs for NDVI)
        final_stac_items = (
            stac_items_from_request
            if stac_items_from_request
            else stac_items_from_session
        )
        logger.info(
            f"[MSG] [{request_id}] Using {len(final_stac_items)} STAC items for analysis (request: {len(stac_items_from_request)}, session: {len(stac_items_from_session)})"
        )

        import asyncio

        result = await asyncio.wait_for(
            vision_agent.analyze(
                user_query=message,
                session_id=session_id,
                imagery_base64=screenshot,
                map_bounds=map_bounds or {},
                collections=collections_list,
                stac_items=final_stac_items,  # Use request data if available
                tile_urls=tile_urls_for_agent,  # NEW: Pass tile URLs directly
                conversation_history=[],
                analysis_type=analysis_type,  # [TARGET] Pass hint from frontend
            ),
            timeout=120.0,
        )

        logger.info(f"[MSG] [{request_id}] [OK] Vision chat completed")

        response_text = result.get("response") or result.get("analysis")
        if not response_text:
            logger.warning(
                f"[MSG] [{request_id}] Empty response from vision agent in chat endpoint"
            )
            response_text = (
                "I wasn't able to generate a response for this query. "
                "Please try rephrasing your question or adjusting the pin location."
            )

        return {
            "status": "success",
            "result": {
                "response": response_text,
                "tools_used": result.get("tools_used", []),
            },
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MSG] [{request_id}] [FAIL] Vision chat failed: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Vision chat failed")


@router.delete("/api/geoint/vision/chat/{session_id}")
async def clear_vision_chat_session(session_id: str):
    """Clear a vision agent session (reset memory)."""
    try:
        # Clear session (vision agent should implement this)
        cleared = True  # Placeholder - implement session clearing in vision agent

        return {
            "status": "success" if cleared else "not_found",
            "session_id": session_id,
            "cleared": cleared,
        }
    except Exception as e:
        logger.error(f"Vision chat clear error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
