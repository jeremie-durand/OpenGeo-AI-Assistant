"""Early intent handling: classification defaults, vision detection, contextual."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import app_state
from services.pipeline_trace import log_pipeline_step
from services.query_pipeline.state import QueryState
from services.query_pipeline.vision import run_vision

logger = logging.getLogger(__name__)


def resolve_classification(state: QueryState) -> None:
    """Default to a STAC search when the RouterAgent gave no classification.

    The RouterAgent is the single source of truth for intent
    (vision | stac | hybrid | contextual); the result is logged and traced.
    """
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
            "needs_vision": state.classification.get("needs_vision_analysis", False),
            "reasoning": state.classification.get("reasoning", "N/A")[
                :100
            ],  # Truncate for readability
        },
    )


def detect_vision_intent(state: QueryState) -> bool:
    """Decide whether the query is about imagery already on the map.

    A RouterAgent stac classification wins (so "show me Sentinel-2 here" loads
    data); otherwise the keyword detector, then needs_vision_analysis.
    """
    conversation_history = state.req_body.get(
        "conversation_history", []
    ) or state.req_body.get("messages", [])

    # STEP 1: Check if Router explicitly classified as STAC search
    # If so, do NOT let keyword detector override — the user wants to LOAD data, not analyze it
    router_says_stac = (
        state.classification and state.classification.get("intent_type") == "stac"
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
            needs_vision = state.classification.get("needs_vision_analysis", False)
            if needs_vision:
                logger.info(
                    "[SEARCH] Vision query detected by GPT CLASSIFICATION (fallback check)"
                )
    return needs_vision


async def answer_contextual(state: QueryState) -> Optional[Dict[str, Any]]:
    """Answer information-only questions without a STAC search.

    The session location is added to the prompt so "what is on the map"
    gets a located answer; returns None to fall back to STAC on failure.
    """
    logger.info(
        "[MSG] CONTEXTUAL REQUEST: Skipping STAC search, generating educational response..."
    )

    # Inject the session location: the contextual handler can't see the map
    contextual_query = state.natural_query
    if app_state.router_agent and state.session_id:
        ctx = app_state.router_agent.tools.session_contexts.get(state.session_id, {})
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
            logger.info("[OK] Generated contextual response successfully")
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


async def run_early_intents(state: QueryState) -> Optional[Dict[str, Any]]:
    """Resolve the intent and answer vision and contextual queries up front.

    Any failure clears the classification so the STAC flow runs with defaults.
    """
    try:
        state.translator = app_state.global_translator

        resolve_classification(state)

        from agents import get_vision_agent  # EnhancedVisionAgent with 5 tools

        vision_agent = get_vision_agent()
        needs_vision = detect_vision_intent(state)

        if needs_vision:
            early_response = await run_vision(state, vision_agent, needs_vision)
            if early_response is not None:
                return early_response

        # For contextual queries, skip STAC search and generate direct educational response
        # intent_type = "contextual" for information/education queries (e.g., "How do hurricanes form?")
        if (
            state.intent_type == "contextual"
            and state.confidence > 0.8
            and not state.classification.get("needs_satellite_data", False)
        ):
            early_response = await answer_contextual(state)
            if early_response is not None:
                return early_response

    except Exception as e:
        logger.error(f"[FAIL] Early classification failed: {e}")
        state.classification = None
