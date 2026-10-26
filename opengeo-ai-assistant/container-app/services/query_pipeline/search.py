"""Execute the STAC search, filter tiles and recover from empty results."""

import logging
import math
import traceback
from typing import Any, Dict, List, Optional, Tuple

import app_state
from services.collection_detection import detect_collections
from services.pipeline_trace import log_pipeline_step
from services.query_pipeline.state import QueryState
from services.stac_search import execute_direct_stac_search, try_alternative_queries

logger = logging.getLogger(__name__)


def resolve_cloud_cover_limit(state: QueryState) -> Optional[int]:
    """Pick the client-side cloud cover limit.

    The LLM-extracted value wins, then the STAC query filter, then 25% when
    the query asks for clear or low-cloud imagery.
    """
    cloud_cover_limit = None

    # Primary source: LLM-extracted cloud_cover from translate_query
    if state.stac_params and state.stac_params.get("cloud_cover") is not None:
        cloud_cover_limit = int(state.stac_params["cloud_cover"])
        logger.info(f"[CLOUD] Cloud cover limit from stac_params: {cloud_cover_limit}%")
    else:
        # Secondary: check STAC query filter (eo:cloud_cover / cloud_cover CQL2 extension)
        query_filter = state.stac_query.get("query", {})
        logger.info(
            f"[SEARCH] DEBUG: Client-side check - stac_query.get('query') = {query_filter}"
        )
        for prop_name in ["eo:cloud_cover", "cloud_cover"]:
            if prop_name in query_filter:
                cloud_cover_filter = query_filter[prop_name]
                logger.info(
                    f"[SEARCH] DEBUG: Found {prop_name} in query_filter: {cloud_cover_filter}"
                )
                if "lte" in cloud_cover_filter:
                    cloud_cover_limit = cloud_cover_filter["lte"]
                elif "lt" in cloud_cover_filter:
                    cloud_cover_limit = cloud_cover_filter["lt"]
                break
            else:
                logger.info(f"[SEARCH] DEBUG: {prop_name} NOT in query_filter")

    # [TOOL] FALLBACK: Keyword detection if no cloud filter in query parameters
    # This catches cases where the cloud filter wasn't propagated through the pipeline
    if cloud_cover_limit is None and state.natural_query:
        query_lower = state.natural_query.lower()
        low_cloud_keywords = [
            "low cloud",
            "no cloud",
            "clear",
            "cloudless",
            "minimal cloud",
            "cloud-free",
            "without clouds",
            "clear sky",
            "clear skies",
            "no clouds",
        ]
        if any(kw in query_lower for kw in low_cloud_keywords):
            cloud_cover_limit = 25
            logger.info(
                f"[TOOL] FALLBACK: Detected low cloud keywords in query, applying {cloud_cover_limit}% filter"
            )

    logger.info(f"[SEARCH] DEBUG: Final cloud_cover_limit = {cloud_cover_limit}")
    return cloud_cover_limit


def apply_cloud_cover_filter(
    state: QueryState,
    spatially_filtered_features: List[Dict[str, Any]],
    cloud_cover_limit: Optional[int],
) -> List[Dict[str, Any]]:
    """Filter by cloud cover, relaxing to 50/75/100% when nothing passes."""
    # Get collection ID for property name lookup
    collections = state.stac_query.get("collections", [])
    primary_collection = collections[0] if collections else None

    # Apply client-side cloud cover filtering with PROGRESSIVE RELAXATION
    # If strict threshold returns 0, progressively relax to give users SOME results
    if cloud_cover_limit is not None:
        original_cloud_limit = cloud_cover_limit
        relaxation_thresholds = [
            cloud_cover_limit,
            50,
            75,
            100,
        ]  # Progressive relaxation

        for threshold in relaxation_thresholds:
            cloud_filtered_results = (
                state.translator._filter_stac_results_by_cloud_cover(
                    {"features": spatially_filtered_features},
                    max_cloud_cover=threshold,
                    collection_id=primary_collection,
                )
            )
            filtered_features = cloud_filtered_results.get("features", [])

            if filtered_features or threshold == 100:
                spatially_filtered_features = filtered_features
                state.search_diagnostics["cloud_filtered_count"] = len(
                    filtered_features
                )

                if threshold > original_cloud_limit:
                    logger.info(
                        f"[CLOUD] Cloud filter relaxed: {original_cloud_limit}% -> {threshold}% (found {len(filtered_features)} tiles)"
                    )
                    state.search_diagnostics["cloud_relaxation"] = (
                        f"{original_cloud_limit}% -> {threshold}%"
                    )
                else:
                    logger.info(
                        f"[CLOUD] After cloud cover filtering (≤{threshold}%): {len(filtered_features)} features kept"
                    )
                break
    return spatially_filtered_features


def select_features(state: QueryState, raw_features: List[Dict[str, Any]]) -> None:
    """Keep every tile for coverage, filter clouds, keep STAC API ordering.

    No spatial pre-filter: dropping tiles by overlap could leave gaps. STAC
    results arrive newest first, matching the implicit "latest" intent.
    """
    # Pass ALL tiles to Agent 3 for intelligent selection
    spatially_filtered_features = raw_features
    state.search_diagnostics["spatial_filter_skipped"] = True
    state.search_diagnostics["spatial_filtered_count"] = len(raw_features)
    logger.info(
        f"[SKIP] Skipping spatial pre-filter: Agent 3 will ensure coverage from {len(raw_features)} tiles"
    )

    # [CLOUD] CLIENT-SIDE CLOUD COVER FILTERING: Safety net in case STAC API ignored the filter
    # Extract cloud cover limit from stac_params (LLM-extracted), then query filter, then keywords
    cloud_cover_limit = resolve_cloud_cover_limit(state)

    spatially_filtered_features = apply_cloud_cover_filter(
        state, spatially_filtered_features, cloud_cover_limit
    )

    # [TARGET] TILE SELECTION: Use STAC API ordering (sorted by datetime desc)
    # When no datetime is specified, STAC API returns results sorted by most recent first
    # This is more efficient than GPT-5 selection and respects user's implicit "latest" intent
    logger.info(
        "[LIST] Using STAC API ordering (results already sorted by datetime desc)"
    )

    # Use all spatially filtered features (already ordered by recency from STAC API)
    state.features = spatially_filtered_features
    state.search_diagnostics["final_count"] = len(state.features)
    state.search_diagnostics["tile_selection_method"] = "stac_api_ordering"
    logger.info(
        f"[OK] Using {len(state.features)} tiles from STAC API (pre-sorted by datetime desc)"
    )

    # Update stac_response with filtered results
    state.stac_response["results"]["features"] = state.features


def _contains(bbox: List[float], lon: float, lat: float) -> bool:
    """True when the point lies in a [west, south, east, north] bbox."""
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north


def _center(bbox: List[float]) -> Tuple[float, float]:
    """Return the (lon, lat) center of a [west, south, east, north] bbox."""
    west, south, east, north = bbox
    return (west + east) / 2, (south + north) / 2


def _tile_bbox(feature: Dict[str, Any]) -> Optional[List[float]]:
    """Return the feature's 2D bbox, or None when it has none."""
    bbox = feature.get("bbox")
    return list(bbox[:4]) if bbox and len(bbox) >= 4 else None


def _select_modis_tiles(
    tiles: List[Dict[str, Any]], requested_bbox: List[float]
) -> List[Dict[str, Any]]:
    """Keep the tiles centered in the bbox, else the closest tile holding its center.

    The fallback serves views smaller than a ~10 degree tile. Only one tile is
    kept there because neighbouring sinusoidal tile bboxes overlap.
    """
    centered = [t for t in tiles if _contains(requested_bbox, *_center(_tile_bbox(t)))]
    if centered:
        return centered
    lon, lat = _center(requested_bbox)
    holding = [t for t in tiles if _contains(_tile_bbox(t), lon, lat)]
    if not holding:
        return []
    closest = min(
        holding,
        key=lambda t: math.dist(_center(_tile_bbox(t)), (lon, lat)),
    )
    return [closest]


def filter_modis_tiles(
    state: QueryState,
    raw_features: List[Dict[str, Any]],
    requested_bbox: Optional[List[float]],
    is_modis: bool,
) -> List[Dict[str, Any]]:
    """Keep the MODIS tiles that cover the requested bbox (see _select_modis_tiles).

    The sinusoidal MODIS grid makes the STAC bbox filter loose (Arizona tiles
    for a California query).
    """
    if is_modis and requested_bbox and raw_features:
        logger.info(
            f"[MAP]  MODIS BBOX FILTER: Checking {len(raw_features)} tiles against requested bbox"
        )
        logger.info(
            f"   Requested bbox: [{requested_bbox[0]:.2f}, {requested_bbox[1]:.2f}, {requested_bbox[2]:.2f}, {requested_bbox[3]:.2f}]"
        )

        if requested_bbox[0] > requested_bbox[2]:
            logger.info("[MAP]  MODIS BBOX FILTER: skipped, bbox crosses 180")
            return raw_features

        with_bbox = [f for f in raw_features if _tile_bbox(f)]
        filtered_features = _select_modis_tiles(with_bbox, requested_bbox)

        logger.info(
            f"[OK] MODIS BBOX FILTER: Kept {len(filtered_features)}/{len(raw_features)} tiles within requested bbox"
        )
        raw_features = filtered_features
        state.search_diagnostics["raw_count"] = len(raw_features)
        state.search_diagnostics["modis_bbox_filtered"] = True
    return raw_features


async def recover_with_alternatives(
    state: QueryState, requested_bbox: Optional[List[float]]
) -> None:
    """Record which stage emptied the results, then retry with relaxed filters."""
    if state.search_diagnostics["raw_count"] == 0:
        state.search_diagnostics["failure_stage"] = "stac_api"
    elif state.search_diagnostics["spatial_filtered_count"] == 0:
        state.search_diagnostics["failure_stage"] = "spatial_filter"
    else:
        state.search_diagnostics["failure_stage"] = "tile_selection"

    # [NEW] TRY ALTERNATIVE QUERIES: Automatically find available alternatives
    logger.info(
        "[SYNC] No results found - attempting to find alternatives with relaxed filters..."
    )
    logger.info(
        f"[SYNC] STAC query datetime: {state.stac_query.get('datetime')}, collections: {state.stac_query.get('collections')}, bbox: {state.stac_query.get('bbox')}"
    )
    try:
        alternative_result = await try_alternative_queries(
            state.natural_query,
            state.stac_query,
            state.stac_params,
            state.translator,
            state.stac_endpoint,
            requested_bbox,
        )
        logger.info(
            f"[SYNC] Alternative result: success={alternative_result.get('success')}, features={len(alternative_result.get('features', []))}"
        )
    except Exception as alt_ex:
        logger.error(
            f"[SYNC] EXCEPTION in try_alternative_queries: {type(alt_ex).__name__}: {alt_ex}"
        )
        logger.error(f"[SYNC] Traceback: {traceback.format_exc()}")
        alternative_result = {"success": False, "features": []}

    if alternative_result.get("success") and alternative_result.get("features"):
        # Successfully found alternatives!
        state.features = alternative_result["features"]
        state.search_diagnostics["final_count"] = len(state.features)
        state.search_diagnostics["alternative_used"] = True
        state.search_diagnostics["relaxation_applied"] = alternative_result.get(
            "relaxation_applied"
        )
        state.search_diagnostics["alternative_explanation"] = alternative_result.get(
            "explanation"
        )
        state.search_diagnostics["original_filters"] = alternative_result.get(
            "original_filters"
        )
        state.search_diagnostics["alternative_filters"] = alternative_result.get(
            "alternative_filters"
        )

        # Update stac_response with alternative features
        state.stac_response["results"]["features"] = state.features

        logger.info(
            f"[OK] Found {len(state.features)} alternative results: {alternative_result.get('explanation')}"
        )
    else:
        logger.info(
            "[FAIL] No alternatives found - will generate empty result response"
        )


async def retry_empty_local_search_on_pc(state: QueryState) -> None:
    """Retry an empty local STAC search on Planetary Computer with PC ids."""
    if state.stac_endpoint != "planetary_computer":
        local_features = state.stac_response.get("results", {}).get("features", [])
        if not local_features:
            logger.info(
                "[SIGNAL] Local STAC returned 0 features — retrying against Planetary Computer"
            )
            pc_collections = detect_collections(state.natural_query)
            pc_stac_query = {
                **state.stac_query,
                "collections": pc_collections,
            }
            state.stac_response = await execute_direct_stac_search(
                pc_stac_query,
                "planetary_computer",
                original_query=state.natural_query,
            )
            state.stac_query = pc_stac_query


async def run_stac_search(state: QueryState) -> None:
    """Search STAC, select tiles and record diagnostics for empty results."""
    state.stac_response = {"results": {"type": "FeatureCollection", "features": []}}
    state.features = []

    # [CHART] DIAGNOSTICS: Track filtering stages for helpful error messages
    state.search_diagnostics = {
        "raw_count": 0,
        "spatial_filtered_count": 0,
        "final_count": 0,
        "stac_query": state.stac_query,
        "failure_stage": "unknown",
    }

    # Note: pipeline_session_id already set at start of query processing

    if state.stac_query:
        try:
            logger.info("[WEB] Executing STAC search...")

            # [SEARCH] PIPELINE LOG: STAC Search Input
            log_pipeline_step(
                state.pipeline_session_id,
                "STAC_SEARCH",
                "INPUT",
                {
                    "collections": state.stac_query.get("collections"),
                    "bbox": state.stac_query.get("bbox"),
                    "datetime": state.stac_query.get("datetime"),
                    "filter": state.stac_query.get("filter"),
                },
            )

            # Determine which STAC endpoint to use (intelligent routing from Router Function App)
            state.stac_endpoint = (
                state.translator.determine_stac_source(
                    state.natural_query, state.stac_params
                )
                if state.translator
                else "planetary_computer"
            )
            logger.info(f"[SIGNAL] STAC source determined: {state.stac_endpoint}")

            # Pass original_query for smart deduplication (removes duplicate grid cells)
            state.stac_response = await execute_direct_stac_search(
                state.stac_query,
                state.stac_endpoint,
                original_query=state.natural_query,
            )

            # If local STAC returned nothing, fall back to Planetary Computer.
            # Always substitute PC-compatible collection IDs in case local STAC used
            # non-standard names (e.g. "sentinel2_eo_products" instead of "sentinel-2-l2a").
            await retry_empty_local_search_on_pc(state)

            if state.stac_response.get("success"):
                raw_features = state.stac_response.get("results", {}).get(
                    "features", []
                )
                state.search_diagnostics["raw_count"] = len(raw_features)
                logger.info(
                    f"[OK] STAC search completed: {len(raw_features)} raw features found"
                )

                # [SEARCH] PIPELINE LOG: STAC Search Output
                log_pipeline_step(
                    state.pipeline_session_id,
                    "STAC_SEARCH",
                    "OUTPUT",
                    {
                        "feature_count": len(raw_features),
                        "first_feature_id": (
                            raw_features[0].get("id") if raw_features else None
                        ),
                        "first_feature_bbox": (
                            raw_features[0].get("bbox") if raw_features else None
                        ),
                    },
                )

                # [SEARCH] MODIS BBOX FILTERING: MODIS STAC API returns tiles outside requested bbox
                # MODIS uses sinusoidal grid (h/v tiles), so bbox filter isn't strict in PC STAC API
                # Filter out tiles whose center is outside requested bbox to prevent showing
                # Arizona/Texas tiles when user requested California
                collections = state.stac_query.get("collections", [])
                is_modis = any(col.startswith("modis-") for col in collections)
                requested_bbox = state.stac_query.get("bbox")

                raw_features = filter_modis_tiles(
                    state, raw_features, requested_bbox, is_modis
                )

                # [SEARCH] NO SPATIAL PRE-FILTERING: Agent 3 handles spatial coverage intelligently
                # Agent 3's GPT-5 ensures full coverage by:
                # 1. Understanding tile mosaicking and grid systems
                # 2. Ensuring "NO GAPS in spatial coverage" (explicit in GPT prompt)
                # 3. Using context-aware tile limits (5-50 based on AOI size)
                # Pre-filtering with geometric overlap could remove tiles Agent 3 needs for seamless coverage
                if (
                    app_state.SEMANTIC_KERNEL_AVAILABLE
                    and state.translator
                    and raw_features
                    and state.stac_query.get("bbox")
                ):
                    select_features(state, raw_features)
                else:
                    state.features = raw_features
                    state.search_diagnostics["spatial_filtered_count"] = len(
                        raw_features
                    )
                    state.search_diagnostics["final_count"] = len(state.features)
                    logger.info(
                        "[WARN] Skipping spatial filtering and tile ranking (no translator or bbox)"
                    )

                # Determine failure stage if no results
                if state.search_diagnostics["final_count"] == 0:
                    await recover_with_alternatives(state, requested_bbox)
            else:
                logger.error(
                    f"[FAIL] STAC search failed: {state.stac_response.get('error', 'Unknown error')}"
                )
                state.search_diagnostics["failure_stage"] = "stac_error"

        except Exception as e:
            logger.error(f"[FAIL] STAC search execution error: {e}")
            state.stac_response = {
                "results": {"type": "FeatureCollection", "features": []}
            }
            state.search_diagnostics["failure_stage"] = "exception"
