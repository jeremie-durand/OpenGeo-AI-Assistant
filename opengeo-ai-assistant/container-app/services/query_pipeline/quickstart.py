"""Quickstart fast path: demo queries answered from pre-computed cache data."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import app_state
from quickstart_cache import get_quickstart_location
from services.query_pipeline.response import summarize_stac_items
from services.query_pipeline.state import QueryState
from services.query_pipeline.tiles import (
    has_numeric_bbox,
    needs_optical_mosaic,
    optimize_tilejson_url,
)
from services.stac_search import build_stac_query, execute_direct_stac_search
from services.tile_urls import build_tile_url_params, clean_tilejson_urls

logger = logging.getLogger(__name__)


async def run_quickstart_fast_path(state: QueryState) -> Dict[str, Any]:
    """Answer a pre-defined demo query from the quickstart cache.

    Skips the RouterAgent, translate_query and response-generation LLM calls,
    saving roughly 7-14 seconds per demo query.
    """
    import time as _time

    _qs_start = _time.perf_counter()

    qs_location = get_quickstart_location(state.natural_query)
    logger.info(
        f"[FAST] QUICKSTART FAST PATH: '{state.natural_query}' -> {qs_location['collections']} @ {qs_location['location']}"
    )

    # Build STAC params from pre-computed data
    temporal = qs_location.get("temporal")
    datetime_range = quickstart_datetime_range(temporal)

    qs_stac_params = {
        "collections": qs_location["collections"],
        "bbox": qs_location["bbox"],
        "datetime": datetime_range,
        "location_name": qs_location["location"],
        "is_quickstart": True,
    }
    qs_stac_query = build_stac_query(qs_stac_params)

    # Execute STAC search — route to local STAC if configured, else Planetary Computer
    qs_stac_endpoint = (
        app_state.global_translator.determine_stac_source(
            state.natural_query, qs_stac_params
        )
        if app_state.global_translator
        else "planetary_computer"
    )
    qs_stac_response = await execute_direct_stac_search(
        qs_stac_query, qs_stac_endpoint, original_query=state.natural_query
    )

    qs_features = []
    if qs_stac_response.get("success"):
        qs_features = qs_stac_response.get("results", {}).get("features", [])
        logger.info(f"[FAST] STAC returned {len(qs_features)} features")

    # Generate template response (no GPT call needed)
    qs_response_msg = quickstart_message(qs_location, qs_features)

    # Build tile URLs (same logic as main path but inlined for speed)
    qs_tile_urls = []
    qs_collection_id = (
        qs_location["collections"][0] if qs_location["collections"] else ""
    )

    for feat in qs_features[:20]:
        tj_asset = feat.get("assets", {}).get("tilejson", {})
        if tj_asset and "href" in tj_asset:
            feat_bbox = feat.get("bbox")
            if not has_numeric_bbox(feat_bbox):
                continue

            optimized_url = optimize_tilejson_url(
                tj_asset["href"],
                build_tile_url_params(qs_collection_id, state.natural_query),
            )

            qs_tile_urls.append(
                {
                    "item_id": feat.get("id"),
                    "bbox": feat_bbox,
                    "tilejson_url": optimized_url,
                }
            )

    # Mosaic for optical collections
    qs_mosaic = None
    if needs_optical_mosaic(qs_collection_id):
        try:
            from hybrid_rendering_system import get_mosaic_tilejson_url

            qs_mosaic = await get_mosaic_tilejson_url(
                collections=[qs_collection_id],
                bbox=qs_location["bbox"],
                datetime_range=datetime_range,
                query_filters=None,
                query_context=state.natural_query,
            )
        except Exception:
            pass

    # Update session context
    if app_state.router_agent and state.session_id and (qs_tile_urls or qs_features):
        stac_items_for_session = summarize_stac_items(
            qs_features, include_cloud_cover_alias=False
        )
        app_state.router_agent.update_session_context(
            state.session_id,
            {
                "has_rendered_map": True,
                "last_bbox": qs_location["bbox"],
                "last_location": qs_location["location"],
                "last_collections": qs_location["collections"],
                "last_stac_items": stac_items_for_session,
                "query_count": app_state.router_agent.tools.session_contexts.get(
                    state.session_id, {}
                ).get("query_count", 0)
                + 1,
            },
        )

    _qs_elapsed = (_time.perf_counter() - _qs_start) * 1000
    logger.info(
        f"[FAST] QUICKSTART FAST PATH completed in {_qs_elapsed:.0f}ms (skipped RouterAgent + translate_query + response GPT)"
    )

    cleaned_qs_results = clean_tilejson_urls(qs_stac_response.get("results", {}))

    return {
        "success": True,
        "response": qs_response_msg,
        "data": {
            "stac_results": cleaned_qs_results,
            "search_metadata": {
                "total_items": len(qs_features),
                "collections_searched": qs_location["collections"],
                "spatial_extent": qs_location["bbox"],
                "temporal_range": datetime_range,
                "search_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "query_classification": {
                "intent_type": "stac",
                "needs_satellite_data": True,
                "needs_contextual_info": False,
                "confidence": 1.0,
            },
        },
        "translation_metadata": {
            "original_query": state.natural_query,
            "translated_params": qs_stac_params,
            "stac_query": qs_stac_query,
            "translation_timestamp": datetime.now(timezone.utc).isoformat(),
            "all_tile_urls": qs_tile_urls if qs_tile_urls else None,
            "mosaic_tilejson": qs_mosaic,
        },
        "debug": {
            "semantic_translator": {
                "available": True,
                "selected_collections": qs_location["collections"],
                "location_info": {
                    "bbox": qs_location["bbox"],
                    "location_name": qs_location["location"],
                },
            },
            "fast_path": True,
            "fast_path_elapsed_ms": round(_qs_elapsed, 1),
        },
    }


def quickstart_datetime_range(temporal: Optional[str]) -> Optional[str]:
    """Expand a cached YYYY or YYYY-MM temporal value into a STAC range."""
    datetime_range = None
    if temporal:
        if len(temporal) == 4:  # Year: "2017"
            datetime_range = f"{temporal}-01-01/{temporal}-12-31"
        elif len(temporal) == 7:  # Month: "2025-06"
            import calendar as _cal

            _y, _m = int(temporal[:4]), int(temporal[5:7])
            _last = _cal.monthrange(_y, _m)[1]
            datetime_range = f"{temporal}-01/{temporal}-{_last:02d}"
        else:
            datetime_range = temporal
    return datetime_range


def quickstart_message(
    qs_location: Dict[str, Any], qs_features: List[Dict[str, Any]]
) -> str:
    """Template chat message for a quickstart result (no LLM call)."""
    qs_dataset = qs_location.get("dataset", qs_location["collections"][0])
    qs_desc = qs_location.get("description", "")
    if qs_features:
        qs_first = qs_features[0]
        qs_props = qs_first.get("properties", {})
        qs_date = (
            qs_props.get("datetime")
            or qs_props.get("start_datetime")
            or qs_props.get("end_datetime")
            or ""
        )
        if qs_date and len(qs_date) >= 10:
            qs_date = qs_date[:10]
        if qs_date:
            qs_response_msg = (
                f"Loaded {len(qs_features)} {qs_dataset} tiles for {qs_location['location']}. "
                f"{qs_desc}. Most recent tile: {qs_date}."
            )
        else:
            qs_response_msg = (
                f"Loaded {len(qs_features)} {qs_dataset} tiles for {qs_location['location']}. "
                f"{qs_desc}."
            )
    else:
        qs_response_msg = (
            f"Searched {qs_dataset} for {qs_location['location']} but no tiles were found "
            f"matching the current filters. Try adjusting the time range."
        )
    return qs_response_msg
