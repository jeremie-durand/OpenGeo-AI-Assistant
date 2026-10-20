"""Vision Agent stages for questions about the imagery on the map."""

import asyncio
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import app_state
from services.pipeline_trace import log_pipeline_step
from services.query_pipeline.state import QueryState

logger = logging.getLogger(__name__)


@dataclass
class VisionInputs:
    """Map context for the Vision Agent, from the request and the session."""

    map_bounds: Optional[Dict[str, Any]]
    imagery_url: Optional[str]
    imagery_base64: Optional[str]
    tile_urls: List[str]
    conversation_history: List[Any]
    collections_list: List[str]
    stac_items_from_session: List[Dict[str, Any]]
    is_hybrid_vision: bool


def collect_vision_inputs(state: QueryState, needs_vision: bool) -> VisionInputs:
    """Gather bounds, screenshot, tiles and session STAC items for the agent.

    The session bbox replaces missing or stale (post navigate_to) bounds.
    """
    # Check if frontend provided map context
    map_bounds = state.req_body.get(
        "map_bounds"
    )  # {north, south, east, west, center_lat, center_lng}
    imagery_url = state.req_body.get("imagery_url")  # Current visible imagery URL
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
        session_context = app_state.router_agent.tools.session_contexts.get(
            state.session_id, {}
        )
        logger.info(
            f"[LIST] Session context for Vision Agent: {list(session_context.keys())}"
        )

        # Merge session collections with current collection
        session_collections = session_context.get("last_collections", [])
        if session_collections and not collections_list:
            collections_list = session_collections

        # Get STAC items from session for raster analysis
        stac_items_from_session = session_context.get("last_stac_items", [])
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
    if current_collection and current_collection not in collections_list:
        collections_list = [current_collection] + collections_list

    # Log vision context availability
    logger.info(f"[SNAP] Map screenshot available: {bool(imagery_base64)}")
    logger.info(f"[SAT] Satellite data loaded: {has_satellite_data}")
    logger.info(f"[MAP] Map bounds available: {bool(map_bounds)}")

    # Check if this is a HYBRID query (vision + data)
    needs_data = state.classification.get("needs_satellite_data", False)
    is_hybrid_vision = needs_vision and needs_data

    if is_hybrid_vision:
        logger.info(
            "[SHUFFLE] HYBRID VISION + DATA QUERY: Will run vision + STAC in PARALLEL for optimal performance"
        )
    return VisionInputs(
        map_bounds=map_bounds,
        imagery_url=imagery_url,
        imagery_base64=imagery_base64,
        tile_urls=tile_urls,
        conversation_history=conversation_history,
        collections_list=collections_list,
        stac_items_from_session=stac_items_from_session,
        is_hybrid_vision=is_hybrid_vision,
    )


async def answer_pure_vision(
    state: QueryState, vision_agent: Any, inputs: VisionInputs
) -> Dict[str, Any]:
    """Run the Vision Agent and return its answer; never falls through to STAC."""
    map_bounds = inputs.map_bounds
    imagery_base64 = inputs.imagery_base64
    tile_urls = inputs.tile_urls
    conversation_history = inputs.conversation_history
    collections_list = inputs.collections_list
    stac_items_from_session = inputs.stac_items_from_session
    logger.info("[BOT] Invoking Vision Agent for pure vision query")
    logger.info(f"   Collections: {collections_list}")
    logger.info(f"   Tile URLs: {len(tile_urls)} tiles")
    logger.info(f"   STAC items: {len(stac_items_from_session)} items")
    logger.info(f"   Has screenshot: {bool(imagery_base64)}")
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
    logger.info(f"[SEARCH] Vision Agent returned: {type(vision_result)}")
    if vision_result:
        logger.info(f"   Keys: {list(vision_result.keys())}")
        logger.info(f"   Response: '{str(vision_result.get('response', ''))[:100]}...'")
        logger.info(f"   Analysis: '{str(vision_result.get('analysis', ''))[:100]}...'")

    # Check for response (or analysis as fallback)
    response_text = (
        vision_result.get("response") or vision_result.get("analysis")
        if vision_result
        else None
    )

    if response_text:
        logger.info("[OK] Vision Agent analysis completed")
        logger.info(f"   Cached: {vision_result.get('cached', False)}")

        # ================================================================
        # VISION AGENT TRACING: Log tool usage for debugging
        # ================================================================
        tools_used = vision_result.get("tools_used", [])
        tool_calls = vision_result.get("tool_calls", [])
        logger.info(f"[TOOL] Vision Agent tools used: {tools_used}")

        # Log to pipeline trace
        log_pipeline_step(
            state.pipeline_session_id,
            "VISION_AGENT",
            "OUTPUT",
            {
                "tools_used": tools_used,
                "tool_calls": tool_calls,
                "has_screenshot": vision_result.get("context", {}).get(
                    "has_screenshot", False
                ),
                "collections": vision_result.get("context", {}).get("collections", []),
                "response_length": len(response_text),
            },
        )

        return {
            "success": True,
            "response": response_text,
            "vision_analysis": vision_result,
            "processing_type": "vision_agent",
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
                    str(vision_result)[:500] if vision_result else "None"
                ),
            },
        )

        # Return a helpful error message instead of falling through to STAC
        return {
            "success": False,
            "response": "I tried to analyze the map but couldn't generate a response. Please try asking your question again.",
            "error": "Vision Agent returned empty response",
            "processing_type": "vision_agent_error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


async def analyze_with_map_context(
    state: QueryState, vision_agent: Any, inputs: VisionInputs
) -> Optional[Dict[str, Any]]:
    """Run the Vision Agent on the visible map.

    Hybrid queries start it as a background task and return None so the STAC
    search runs in parallel; errors are returned, never sent on to STAC.
    """
    map_bounds = inputs.map_bounds
    imagery_base64 = inputs.imagery_base64
    tile_urls = inputs.tile_urls
    conversation_history = inputs.conversation_history
    collections_list = inputs.collections_list
    stac_items_from_session = inputs.stac_items_from_session
    is_hybrid_vision = inputs.is_hybrid_vision
    try:
        # For hybrid queries, we'll start vision analysis but continue to STAC
        # Both will run in parallel (vision task created, STAC search proceeds)
        state.vision_task = None

        if is_hybrid_vision:
            # Create vision agent task (runs in background)
            logger.info("[FAST] Starting Vision Agent task (parallel execution)...")
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
            return await answer_pure_vision(state, vision_agent, inputs)
    except Exception as e:
        logger.warning(f"[WARN] Chat vision analysis failed: {e}, returning error...")
        logger.error(traceback.format_exc())

        # Return error, DON'T fall through to STAC for vision queries
        return {
            "success": False,
            "response": "I encountered an error analyzing the imagery.",
            "error": "internal error",
            "processing_type": "vision_agent_error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


async def analyze_without_map_context(
    state: QueryState, vision_agent: Any, inputs: VisionInputs
) -> Dict[str, Any]:
    """Answer through the agent's query_knowledge tool when no map is visible."""
    map_bounds = inputs.map_bounds
    conversation_history = inputs.conversation_history
    collections_list = inputs.collections_list
    logger.info("ℹ️ No map screenshot, but Vision Agent can use LLM tool")
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
        logger.info(f"[SEARCH] Vision Agent (LLM mode) returned: {type(vision_result)}")
        if vision_result:
            logger.info(f"   Keys: {list(vision_result.keys())}")
            logger.info(
                f"   Response: '{str(vision_result.get('response', ''))[:100]}...'"
            )

        # Check for response (or analysis as fallback)
        response_text = (
            vision_result.get("response") or vision_result.get("analysis")
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
            logger.info(f"[TOOL] Vision Agent (LLM mode) tools used: {tools_used}")

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
        logger.warning(f"[WARN] Vision Agent LLM fallback failed: {e}")
        logger.error(traceback.format_exc())

        # Return helpful message instead of falling through to STAC
        return {
            "success": True,
            "response": "I can see satellite imagery on the map but couldn't analyze it in detail. Please try asking a specific question about the location or features you see.",
            "error": "internal error",
            "processing_type": "vision_agent_error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


async def run_vision(
    state: QueryState, vision_agent: Any, needs_vision: bool
) -> Optional[Dict[str, Any]]:
    """Answer a question about the visible imagery, or start it for hybrids.

    Hybrid queries (vision and data) run the agent in parallel with the STAC
    search, which saves several seconds.
    """
    logger.info("[IMG] VISION QUERY DETECTED: User is asking about visible imagery")
    inputs = collect_vision_inputs(state, needs_vision)
    if inputs.map_bounds or inputs.imagery_url or inputs.imagery_base64:
        return await analyze_with_map_context(state, vision_agent, inputs)
    return await analyze_without_map_context(state, vision_agent, inputs)
