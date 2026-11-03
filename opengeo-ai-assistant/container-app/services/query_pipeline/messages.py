"""Chat message for the STAC results, empty results or missing parameters."""

import logging
from typing import Optional

import app_state
from services.query_pipeline.state import QueryState
from services.query_responses import (
    generate_contextual_empty_response,
    generate_fallback_response,
)

logger = logging.getLogger(__name__)


async def await_vision_analysis(state: QueryState) -> Optional[str]:
    """Wait for a hybrid query's parallel vision task; return its analysis."""
    early_contextual_response = None
    vision_result = None
    if state.vision_task is not None:
        try:
            logger.info("[WAIT] Awaiting parallel vision analysis to complete...")
            vision_result = await state.vision_task
            if vision_result and vision_result.get("analysis"):
                early_contextual_response = vision_result["analysis"]
                logger.info(
                    "[OK] Parallel vision analysis completed and ready for combination"
                )
            else:
                logger.warning("[WARN] Vision task completed but no analysis returned")
        except Exception as e:
            logger.error(f"[FAIL] Parallel vision task failed: {e}")
            early_contextual_response = None
    return early_contextual_response


async def compose_results_message(
    state: QueryState, early_contextual_response: Optional[str]
) -> None:
    """Describe the features (or the alternative shown), prefixed by vision."""
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
        contextual_response = (
            await state.translator.generate_contextual_earth_science_response(
                state.natural_query,
                state.classification or {},
                {"success": True, "results": {"features": state.features}},
                None,
            )
        )
        state.response_message = contextual_response.get(
            "message",
            f"Found {len(state.features)} satellite data results for your query.",
        )

        # If we have vision analysis from hybrid query, prepend it
        if early_contextual_response:
            logger.info("[SHUFFLE] Combining vision analysis with STAC data response")
            state.response_message = f"**Visual Analysis:**\n{early_contextual_response}\n\n---\n\n**Data Results:**\n{state.response_message}"


def log_and_sync_features(state: QueryState) -> None:
    """Log pre-message state; copy features from stac_response if they differ."""
    # [SEARCH] DEBUG: Log state before response generation to diagnose "no matches" bug
    logger.info("[SEARCH] RESPONSE GENERATION DEBUG:")
    logger.info(f"   - features count: {len(state.features) if state.features else 0}")
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
    stac_response_features = state.stac_response.get("results", {}).get("features", [])
    if not state.features and stac_response_features:
        logger.warning(
            f"[WARN] MISMATCH DETECTED: features is empty but stac_response has {len(stac_response_features)} features!"
        )
        logger.warning(
            "   Synchronizing features from stac_response to prevent 'no matches' bug"
        )
        state.features = stac_response_features
        state.search_diagnostics["final_count"] = len(state.features)


async def build_response_message(state: QueryState) -> None:
    """Write the chat message for results, empty results or no STAC query."""
    if app_state.SEMANTIC_KERNEL_AVAILABLE and state.translator and state.features:
        try:
            logger.info("[NOTE] Generating contextual response message...")

            early_contextual_response = await await_vision_analysis(state)

            await compose_results_message(state, early_contextual_response)

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
