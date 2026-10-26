"""RouterAgent stages: session sync, navigate_to, climate queries, intent bridge."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp
import app_state
from services.pipeline_trace import log_pipeline_step
from services.query_pipeline.dashboard_context import is_dashboard_question
from services.query_pipeline.state import QueryState
from services.stac_search import _stac_base

logger = logging.getLogger(__name__)


def sync_router_session_context(state: QueryState) -> None:
    """Merge the translator's conversation context into the router session.

    Values the router already knows (has_rendered_map, last_bbox,
    last_location, last_collections) are preserved so follow-ups route right.
    """
    if app_state.global_translator:
        context = app_state.global_translator.get_conversation_context(state.session_id)
        context["has_screenshot"] = bool(state.req_body.get("imagery_base64"))
        # The translator's context may be stale; keep the router's has_rendered_map.
        existing_router_context = app_state.router_agent.tools.session_contexts.get(
            state.session_id, {}
        )

        # Preserve has_rendered_map if already True in router context
        if existing_router_context.get("has_rendered_map"):
            context["has_rendered_map"] = True
        # Also set from frontend's has_satellite_data flag (new request indicator)
        elif state.req_body.get("has_satellite_data"):
            context["has_rendered_map"] = True

        # Preserve last_bbox and last_location from router context if not in translator context
        if not context.get("last_bbox") and existing_router_context.get("last_bbox"):
            context["last_bbox"] = existing_router_context.get("last_bbox")
        if not context.get("last_location") and existing_router_context.get(
            "last_location"
        ):
            context["last_location"] = existing_router_context.get("last_location")
        if not context.get("last_collections") and existing_router_context.get(
            "last_collections"
        ):
            context["last_collections"] = existing_router_context.get(
                "last_collections"
            )

        app_state.router_agent.update_session_context(state.session_id, context)


async def handle_navigate_to(state: QueryState) -> Dict[str, Any]:
    """Resolve the location and pan the map there without a STAC search.

    The bbox is stored in the session so "show me Sentinel tiles" follows up
    on this location.
    """
    # Get location from router action, or fall back to original query for bare locations
    location = state.router_action.get("location") or state.router_action.get(
        "original_query", ""
    )
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

            # Store the bbox so follow-ups like "show me Sentinel tiles" reuse it
            if app_state.router_agent and state.session_id:
                navigate_context = {
                    "last_bbox": bbox,
                    "last_location": location_display,
                    "has_rendered_map": False,  # No imagery yet, just navigation
                    "query_count": app_state.router_agent.tools.session_contexts.get(
                        state.session_id, {}
                    ).get("query_count", 0)
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


async def handle_extreme_weather(state: QueryState) -> Optional[Dict[str, Any]]:
    """Answer a climate-projection query inline with the ExtremeWeatherAgent.

    Returns None to fall through to the STAC search when the coordinates are
    unknown or the agent fails. A timeout returns an error response instead.
    """
    location = state.router_action.get("location") or state.router_action.get(
        "original_query", ""
    )
    logger.info(f"[STORM] EXTREME_WEATHER ACTION: Climate query for '{location}'")

    try:
        # Resolve location to coordinates
        from location_resolver import EnhancedLocationResolver

        resolver = EnhancedLocationResolver()

        bbox = await resolver.resolve_location_to_bbox(location) if location else None

        # Use session context bbox as fallback
        if not bbox and app_state.router_agent and state.session_id:
            ctx = app_state.router_agent.tools.session_contexts.get(
                state.session_id, {}
            )
            bbox = ctx.get("last_bbox")
            location = location or ctx.get("last_location", "this location")

        if bbox and len(bbox) == 4:
            center_lng = (bbox[0] + bbox[2]) / 2
            center_lat = (bbox[1] + bbox[3]) / 2
        else:
            # Cannot determine coordinates — fall through to STAC
            logger.warning("[WARN] No coordinates for climate query, falling through")
            center_lat = center_lng = None

        if center_lat is not None and center_lng is not None:
            from geoint.extreme_weather_agent import (
                get_extreme_weather_agent,
            )

            agent = get_extreme_weather_agent()
            climate_session_id = state.session_id or str(uuid.uuid4())
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

            analysis = result.get("response", "Climate analysis complete.")
            location_display = location.title() if location else "this location"

            logger.info(f"[STORM] Climate analysis complete for {location_display}")

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
        logger.error(f"[STORM] Climate analysis timed out for {location}")
        return {
            "success": False,
            "response": f"[TIME] Climate analysis for {location} timed out. Please try a more specific question.",
            "user_response": f"[TIME] Climate analysis for {location} timed out. Please try a more specific question.",
            "processing_type": "climate_projection",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as climate_error:
        logger.error(f"[STORM] Climate analysis failed: {climate_error}")
        # Fall through to STAC search as fallback


def preroute_dashboard_question(state: QueryState) -> bool:
    """Send a question about the dashboard's figures to the contextual answer.

    The RouterAgent never sees the dashboard, so it reads "March" in "why so
    few scenes in March?" as a place. Only requests with dashboard context
    are pre-routed.
    """
    if not is_dashboard_question(state.req_body):
        return False
    logger.info("[CHART] Dashboard question -> contextual (RouterAgent skipped)")
    state.router_action = {
        "action_type": "contextual",
        "routing_reason": "dashboard_question",
    }
    return True


async def route_with_agent(state: QueryState) -> Optional[Dict[str, Any]]:
    """Classify the query with the RouterAgent.

    navigate_to and extreme_weather are answered here; the other actions
    (stac_search, vision_analysis, contextual, hybrid, gis_feature_query)
    continue through the pipeline. A router failure leaves router_action None.
    An extreme_weather fall-through leaves router_action set without a
    derived classification.
    """
    if state.router_action is None and app_state.router_agent and state.session_id:
        try:
            sync_router_session_context(state)

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
                    "needs_stac": state.router_action.get("needs_stac_search", False),
                    "needs_vision": state.router_action.get(
                        "needs_vision_analysis", False
                    ),
                },
            )

            if state.router_action.get("action_type") == "navigate_to":
                return await handle_navigate_to(state)

            if state.router_action.get("action_type") == "extreme_weather":
                early_response = await handle_extreme_weather(state)
                if early_response is not None:
                    return early_response

        except Exception as router_error:
            logger.warning(
                f"[WARN] RouterAgent failed: {router_error}, falling back to legacy classification"
            )
            state.router_action = None


async def classify_router_action(state: QueryState) -> None:
    """Turn the RouterAgent action into the classification later stages read.

    gis_feature_query is rewritten into a contextual question about the
    feature fetched from the vector API.
    """
    if state.router_action:
        action_type = state.router_action.get("action_type", "")

        if action_type == "stac_search":
            logger.info("Converting RouterAgent 'stac_search' to classification format")
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
            logger.info("Converting RouterAgent 'contextual' to classification format")
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
            vector_base = os.getenv("VECTOR_API_URL", "http://vector-api:8080").rstrip(
                "/"
            )
            collection = state.router_action.get("collection", "")
            feature_id = state.router_action.get("feature_id", "")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{vector_base}/parquet/collections/{collection}/items/{feature_id}",
                        headers={"Accept": "application/geo+json"},
                    ) as feature_resp:
                        feature = await feature_resp.json()
                    async with session.get(
                        f"{_stac_base}/collections",
                        headers={"Accept": "application/json"},
                    ) as stac_resp:
                        stac = await stac_resp.json()
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
            except Exception as error:
                logger.warning(f"GIS feature query failed: {error}")
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
