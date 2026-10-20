"""Request intake: read the body, open the pipeline trace, fix stale bounds."""

import logging
import uuid

import app_state
from request_models import QueryRequest
from services.pipeline_trace import clear_pipeline_trace, log_pipeline_step
from services.query_pipeline.state import QueryState

logger = logging.getLogger(__name__)


def start_query(body: QueryRequest, state: QueryState) -> None:
    """Read the request into state, select the model and open the trace."""
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

    # Check for pin parameter
    if state.pin:
        pin_lat = state.pin.get("lat")
        pin_lng = state.pin.get("lng")
        logger.info(f"[PIN] Pin detected: ({pin_lat:.4f}, {pin_lng:.4f})")


def apply_session_bbox_override(state: QueryState) -> None:
    """Use the session bbox when the frontend sent no or stale map bounds.

    After navigate_to the frontend may still send its old bounds, so every
    downstream handler (vision, contextual, STAC) gets the session bbox.
    """
    frontend_map_bounds = state.req_body.get("map_bounds")
    if app_state.router_agent and state.session_id:
        session_ctx = app_state.router_agent.tools.session_contexts.get(
            state.session_id, {}
        )
        session_bbox_early = session_ctx.get("last_bbox")
        recently_navigated_early = session_bbox_early and not session_ctx.get(
            "has_rendered_map", True
        )

        if session_bbox_early and (not frontend_map_bounds or recently_navigated_early):
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
