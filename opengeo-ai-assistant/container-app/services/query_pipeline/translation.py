"""Translate the natural-language query into STAC search parameters."""

import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import app_state
from services.collection_detection import detect_collections
from services.pipeline_trace import log_pipeline_step
from services.query_pipeline.state import QueryState
from services.stac_search import build_stac_query

logger = logging.getLogger(__name__)


def apply_regex_datetime_fallback(state: QueryState) -> None:
    """Fill a missing datetime from "MONTH YEAR" or "in YEAR" in the query.

    A deterministic safety net for when the LLM datetime agent found nothing.
    """
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
            m_name, y_str = month_year_match.group(1), month_year_match.group(2)
            m_num = month_names[m_name]
            y_num = int(y_str)
            last_day = cal_mod.monthrange(y_num, m_num)[1]
            extracted_datetime = (
                f"{y_str}-{m_num:02d}-01/{y_str}-{m_num:02d}-{last_day:02d}"
            )

        # Pattern 2: "MONTH YEAR" without preposition (e.g., "December 2018")
        if not extracted_datetime:
            month_year_match2 = re.search(
                r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})",
                query_lower,
            )
            if month_year_match2:
                m_name, y_str = month_year_match2.group(1), month_year_match2.group(2)
                m_num = month_names[m_name]
                y_num = int(y_str)
                last_day = cal_mod.monthrange(y_num, m_num)[1]
                extracted_datetime = (
                    f"{y_str}-{m_num:02d}-01/{y_str}-{m_num:02d}-{last_day:02d}"
                )

        # Pattern 3: "from/in/for YEAR" (e.g., "from 2018")
        if not extracted_datetime:
            year_match = re.search(r"(?:from|in|for)\s+((?:19|20)\d{2})\b", query_lower)
            if year_match:
                y_str = year_match.group(1)
                extracted_datetime = f"{y_str}-01-01/{y_str}-12-31"

        if extracted_datetime:
            state.stac_query["datetime"] = extracted_datetime
            state.stac_params["datetime"] = extracted_datetime
            logger.info(
                f"[DATE] REGEX FALLBACK: Extracted datetime '{extracted_datetime}' from query (GPT agent missed it)"
            )


def build_query_from_params(state: QueryState) -> None:
    """Build the STAC query from translated params, or trace why there is none."""
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
                "location_name": state.stac_params.get("location_name", "N/A"),
            },
        )

        # Build STAC query from semantic analysis (like Router Function App)
        state.stac_query = build_stac_query(state.stac_params)
        logger.info(
            f"[TOOL] Built STAC query from params: {json.dumps(state.stac_query, indent=2)}"
        )

        apply_regex_datetime_fallback(state)

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


async def translate_with_agents(state: QueryState) -> Optional[Dict[str, Any]]:
    """Translate with the multi-agent translator, session bbox as fallback.

    Returns the LOCATION_REQUIRED response when a location is needed but
    missing; translation errors leave stac_params and stac_query None.
    """
    try:
        logger.info(
            f"[BOT] Starting multi-agent pipeline — query='{state.natural_query}' pin={state.pin}"
        )

        # Always pass the session bbox: translate_query uses it as a fallback
        # when Agent 2 extracts no location
        session_bbox = None
        session_location = None

        # Get session context from router_agent (session_contexts is on router_agent.tools)
        if app_state.router_agent and state.session_id:
            try:
                session_context = app_state.router_agent.tools.session_contexts.get(
                    state.session_id, {}
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
        if state.router_action and state.router_action.get("use_current_location"):
            logger.info("[PIN] Router set use_current_location=True")

        logger.info("[SYNC] Translating natural language to STAC parameters...")

        # Use the semantic translator's translate_query method with pin and session_bbox fallback
        state.stac_params = await state.translator.translate_query(
            state.natural_query,
            pin_location=state.pin,
            session_bbox=session_bbox,
        )

        if state.stac_params and state.stac_params.get("error") == "LOCATION_REQUIRED":
            logger.warning("[WARN] Location required but not found in query")
            return {
                "success": False,
                "error": "LOCATION_REQUIRED",
                "response": state.stac_params.get(
                    "message", "Please specify a location for your search."
                ),
                "suggestions": state.stac_params.get("suggestions", []),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        build_query_from_params(state)

    except Exception as e:
        logger.error(f"[FAIL] Semantic translation failed: {e}", exc_info=True)
        logger.error(f"[FAIL] Full traceback: {traceback.format_exc()}")
        state.stac_params = None
        state.stac_query = None


def translate_with_keywords(state: QueryState) -> None:
    """Without an LLM, pick collections by keyword and build a basic query."""
    logger.warning("[WARN] Semantic Kernel not available, using fallback processing")

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


async def translate_to_stac(state: QueryState) -> Optional[Dict[str, Any]]:
    """Map stac and hybrid queries to STAC parameters.

    Vision and contextual intents skip collection mapping (no new data).
    """
    state.stac_params = None
    state.stac_query = None
    state.response_message = (
        "I can help you find Earth science data, but I need more specific information."
    )

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
            translate_with_keywords(state)

        elif app_state.SEMANTIC_KERNEL_AVAILABLE and state.translator:
            early_response = await translate_with_agents(state)
            if early_response is not None:
                return early_response
