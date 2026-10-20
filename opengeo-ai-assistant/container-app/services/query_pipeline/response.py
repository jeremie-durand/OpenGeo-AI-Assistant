"""Final /api/query response assembly and session bookkeeping."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import app_state
from services.pipeline_trace import get_pipeline_trace, log_pipeline_step
from services.query_pipeline.state import QueryState
from services.tile_urls import clean_tilejson_urls

logger = logging.getLogger(__name__)


def summarize_stac_items(
    features: List[Dict[str, Any]], include_cloud_cover_alias: bool
) -> List[Dict[str, Any]]:
    """Keep what the vision agent needs (incl. asset href/type) of 10 items."""
    items = []
    for f in features[:10]:
        properties = {
            "datetime": f.get("properties", {}).get("datetime"),
            "eo:cloud_cover": f.get("properties", {}).get("eo:cloud_cover"),
        }
        if include_cloud_cover_alias:
            properties["cloud_cover"] = f.get("properties", {}).get("cloud_cover")
        items.append(
            {
                "id": f.get("id"),
                "collection": f.get("collection"),
                "bbox": f.get("bbox"),
                "properties": properties,
                "assets": {
                    k: {"href": v.get("href"), "type": v.get("type")}
                    for k, v in (f.get("assets") or {}).items()
                },
            }
        )
    return items


def record_render_in_session(state: QueryState) -> None:
    """Mark the map as rendered so follow-up questions route to vision."""
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
                state.stac_params.get("location_name") if state.stac_params else None
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


def build_complete_response(state: QueryState) -> Dict[str, Any]:
    """Assemble the /api/query response and trace the final step."""
    cleaned_stac_results = clean_tilejson_urls(state.stac_response.get("results", {}))

    # Prepare final response
    complete_response = {
        "success": True,
        "response": state.response_message,
        "data": {
            "stac_results": cleaned_stac_results,
            "search_metadata": {
                "total_items": len(state.features),
                "collections_searched": (
                    state.stac_query.get("collections", []) if state.stac_query else []
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
                    state.stac_query.get("collections", []) if state.stac_query else []
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
    return complete_response
