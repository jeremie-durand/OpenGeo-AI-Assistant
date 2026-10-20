"""STAC endpoint config, direct search, query building and alternative retries."""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
from services.pc_metadata import check_collection_coverage
from services.tile_dedup import deduplicate_tiles_by_grid

logger = logging.getLogger(__name__)


_stac_base = os.getenv("STAC_API_URL", "http://localhost:8081").strip().rstrip("/")


_local_stac_url = f"{_stac_base}/search"


STAC_ENDPOINTS = {
    "local_stac": _local_stac_url,
    "planetary_computer": "https://planetarycomputer.microsoft.com/api/stac/v1/search",
    "veda": "https://openveda.cloud/api/stac/search",
}


async def execute_direct_stac_search(
    stac_query: Dict[str, Any],
    stac_endpoint: str = "local_stac",
    original_query: str = None,
) -> Dict[str, Any]:
    """Execute STAC search against specified endpoint (Local STAC or VEDA)

    Args:
        stac_query: STAC API query parameters
        stac_endpoint: Which STAC endpoint to use
        original_query: Original user query (for smart deduplication)
    """
    try:
        stac_url = STAC_ENDPOINTS.get(stac_endpoint, STAC_ENDPOINTS["local_stac"])
        logger.info(
            f"[SEARCH] STAC SEARCH: {stac_endpoint} | collections={stac_query.get('collections', [])} | bbox={stac_query.get('bbox', 'NONE')} | datetime={stac_query.get('datetime', 'NONE')} | limit={stac_query.get('limit', 'default')}"
        )

        # NOTE: We do NOT validate coverage proactively because STAC collection extents
        # represent "data exists somewhere in this region" not "complete coverage".
        # Instead, we let the search run and handle empty results with helpful messages.

        timeout = aiohttp.ClientTimeout(total=60)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(stac_url, json=stac_query) as response:
                logger.info(f"[SEARCH] STAC SEARCH: HTTP {response.status}")

                if response.status == 200:
                    stac_response = await response.json()
                    features = stac_response.get("features", [])

                    # Summarize results in one line
                    if features:
                        collections_returned = list(
                            set(f.get("collection", "?") for f in features)
                        )
                        logger.info(
                            f"[OK] STAC RESULTS: {len(features)} items from {collections_returned}"
                        )
                    else:
                        logger.warning("[WARN] STAC RESULTS: 0 items returned")

                    # If no results, add helpful coverage information
                    coverage_info = None
                    if len(features) == 0:
                        collections = stac_query.get("collections", [])
                        bbox = stac_query.get("bbox")

                        if collections and bbox:
                            # Check if location is likely outside collection coverage
                            for collection_id in collections:
                                coverage_check = check_collection_coverage(
                                    collection_id, bbox
                                )
                                if not coverage_check["covered"]:
                                    coverage_info = {
                                        "message": coverage_check["message"],
                                        "alternatives": coverage_check.get(
                                            "alternatives", []
                                        ),
                                    }
                                    logger.info(
                                        f"ℹ️ No results - likely due to coverage: {coverage_info['message']}"
                                    )
                                    break

                    # Basic feature enhancement (simplified for now)
                    enhanced_features = features

                    # [TARGET] TILE SELECTION: Select best tiles with temporal consistency
                    # IMPORTANT: TileSelector runs FIRST on all features so it can group by
                    # acquisition date and pick a single consistent date. Grid deduplication
                    # runs AFTER to remove duplicate grid cells within the selected date.
                    # This ensures all tiles on the map come from the same time period.
                    from tile_selector import TileSelector

                    bbox = stac_query.get("bbox")
                    collections = stac_query.get("collections", [])

                    # Determine optimal tile limit based on ACTUAL area coverage needed
                    # HLS/Sentinel-2 tiles are ~100km × 100km (~1° × 1° at mid-latitudes)
                    max_tiles = 10  # Default for city-scale areas
                    if bbox and len(bbox) == 4:
                        # Calculate area in square degrees
                        width_deg = bbox[2] - bbox[0]
                        height_deg = bbox[3] - bbox[1]
                        area_degrees = width_deg * height_deg

                        # Calculate tiles needed for full spatial coverage
                        # Assuming ~1° × 1° tiles (HLS/Sentinel-2), add 50% margin for overlap
                        import math

                        tiles_wide = math.ceil(width_deg / 1.0)
                        tiles_tall = math.ceil(height_deg / 1.0)
                        tiles_for_coverage = int(
                            tiles_wide * tiles_tall * 1.5
                        )  # 50% overlap margin

                        if area_degrees > 25:  # Country-scale (e.g., Greece ~71 sq deg)
                            max_tiles = min(
                                100, max(50, tiles_for_coverage)
                            )  # 50-100 tiles for countries
                            logger.info(
                                f"[GLOBE] Country-scale area ({area_degrees:.1f} sq deg) -> max_tiles={max_tiles}"
                            )
                        elif (
                            area_degrees > 5
                        ):  # Large region (e.g., California, large state)
                            max_tiles = min(
                                60, max(30, tiles_for_coverage)
                            )  # 30-60 tiles
                            logger.info(
                                f"[MAP] Large region ({area_degrees:.1f} sq deg) -> max_tiles={max_tiles}"
                            )
                        elif area_degrees > 1.0:  # Medium region (multi-city area)
                            max_tiles = min(
                                30, max(15, tiles_for_coverage)
                            )  # 15-30 tiles
                        elif area_degrees > 0.1:  # Small region (single city area)
                            max_tiles = 15
                        else:  # Point/small area
                            max_tiles = 10

                    # STEP 1: Intelligent tile selection with date grouping on ALL candidates
                    # TileSelector groups by acquisition date and picks the best single date
                    selected_features = TileSelector.select_best_tiles(
                        features=enhanced_features,
                        query_bbox=bbox,
                        collections=collections,
                        max_tiles=max_tiles,
                        query=original_query,
                    )

                    # STEP 2: Grid deduplication within the selected date
                    # Removes duplicate grid cells (keeps most recent per location)
                    selected_features = deduplicate_tiles_by_grid(
                        selected_features, original_query
                    )

                    logger.info(
                        f"[TARGET] TILE PIPELINE: {len(features)} raw -> {len(selected_features)} final tiles (max={max_tiles})"
                    )

                    result = {
                        "success": True,
                        "results": {
                            "type": "FeatureCollection",
                            "features": selected_features,
                        },
                        "search_metadata": {
                            "total_found": len(features),
                            "total_selected": len(selected_features),
                            "query_used": stac_query,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    }

                    # Add coverage info if no results and likely coverage issue
                    if coverage_info:
                        result["coverage_info"] = coverage_info

                    return result
                else:
                    error_text = await response.text()
                    logger.error(
                        f"[FAIL] STAC API error {response.status}: {error_text}"
                    )
                    return {
                        "success": False,
                        "error": f"STAC API returned {response.status}: {error_text}",
                        "results": {"type": "FeatureCollection", "features": []},
                    }

    except asyncio.TimeoutError:
        logger.error("[FAIL] STAC search timed out")
        return {
            "success": False,
            "error": "STAC search timed out",
            "results": {"type": "FeatureCollection", "features": []},
        }
    except aiohttp.ClientError as e:
        logger.error(f"[FAIL] STAC network error: {e}")
        return {
            "success": False,
            "error": "STAC service unavailable",
            "results": {"type": "FeatureCollection", "features": []},
        }
    except Exception as e:
        logger.error(f"[FAIL] STAC search error: {e}")
        return {
            "success": False,
            "error": "STAC search failed",
            "results": {"type": "FeatureCollection", "features": []},
        }


async def try_alternative_queries(
    original_query: str,
    original_stac_query: Dict[str, Any],
    original_stac_params: Dict[str, Any],
    translator: Any,
    stac_endpoint: str,
    requested_bbox: Optional[List[float]],
) -> Dict[str, Any]:
    """
    [NEW] Automatically try progressively relaxed queries to find available alternatives.

    Strategy:
    1. Keep location FIXED (highest priority)
    2. Try relaxing filters in order:
       - Cloud cover (10% -> 30% -> 50%)
       - Date range (expand backwards)
       - Collections (try related alternatives)

    Returns:
        {
            "success": bool,
            "features": List[Dict],
            "relaxation_applied": str,  # Description of what was changed
            "original_filters": dict,   # What was originally requested
            "alternative_filters": dict # What was actually used
        }
    """
    logger.info("[SYNC] Attempting to find alternative results with relaxed filters...")

    original_filters = {
        "datetime": original_stac_query.get("datetime"),
        "cloud_cover": None,
        "collections": original_stac_query.get("collections", []),
    }

    # Extract original cloud cover threshold
    original_filter = original_stac_query.get("filter", {})
    if "eo:cloud_cover" in str(original_filter):
        filter_str = str(original_filter)
        if "10" in filter_str:
            original_filters["cloud_cover"] = 10
        elif "20" in filter_str:
            original_filters["cloud_cover"] = 20

    # RELAXATION 1: Try relaxing cloud cover
    if original_filters["cloud_cover"] and original_filters["cloud_cover"] < 50:
        for cloud_threshold in [30, 50]:
            if cloud_threshold <= original_filters["cloud_cover"]:
                continue

            logger.info(
                f"[WEATHER] Trying alternative with cloud cover <{cloud_threshold}%..."
            )

            # Modify query
            alt_query = original_stac_query.copy()
            # Update cloud filter (simplified - adjust based on your filter structure)
            alt_query["filter"] = {"eo:cloud_cover": {"lt": cloud_threshold}}

            # Try search
            try:
                alt_response = await execute_direct_stac_search(
                    alt_query, stac_endpoint
                )

                if alt_response.get("success"):
                    alt_features = alt_response.get("results", {}).get("features", [])

                    if alt_features:
                        # Apply spatial filtering
                        if translator and requested_bbox:
                            filtered_results = (
                                translator._filter_stac_results_by_spatial_overlap(
                                    {"features": alt_features},
                                    requested_bbox,
                                    min_overlap=0.1,
                                )
                            )
                            alt_features = filtered_results.get("features", [])

                        # Apply cloud cover filtering (client-side safety net)
                        if translator and cloud_threshold:
                            # Get collection for property name lookup
                            collections = alt_query.get("collections", [])
                            primary_collection = collections[0] if collections else None

                            cloud_filtered = translator._filter_stac_results_by_cloud_cover(
                                {"features": alt_features},
                                max_cloud_cover=cloud_threshold,
                                collection_id=primary_collection,  # Pass collection for property lookup
                            )
                            alt_features = cloud_filtered.get("features", [])

                        # Apply tile selection
                        if alt_features:
                            from tile_selector import TileSelector

                            # Use same dynamic tile limit as main query
                            fallback_max_tiles = 50  # Default for fallback queries
                            if requested_bbox and len(requested_bbox) == 4:
                                fb_area = (requested_bbox[2] - requested_bbox[0]) * (
                                    requested_bbox[3] - requested_bbox[1]
                                )
                                if fb_area > 25:
                                    fallback_max_tiles = 100
                                elif fb_area > 5:
                                    fallback_max_tiles = 60
                            selected_features = TileSelector.select_best_tiles(
                                features=alt_features,
                                query_bbox=requested_bbox,
                                collections=alt_query.get("collections"),
                                max_tiles=fallback_max_tiles,
                                query=original_query,
                            )

                            if selected_features:
                                logger.info(
                                    f"[OK] Found {len(selected_features)} results with cloud cover <{cloud_threshold}%"
                                )
                                return {
                                    "success": True,
                                    "features": selected_features,
                                    "relaxation_applied": f"cloud_cover_relaxed_to_{cloud_threshold}",
                                    "original_filters": original_filters,
                                    "alternative_filters": {
                                        **original_filters,
                                        "cloud_cover": cloud_threshold,
                                    },
                                    "explanation": f"Relaxed cloud cover from <{original_filters['cloud_cover']}% to <{cloud_threshold}%",
                                }
            except Exception as e:
                logger.warning(f"[WARN] Alternative query failed: {e}")
                continue

    # RELAXATION 2: Try expanding date range
    datetime_str = original_stac_query.get("datetime", "")
    logger.info(
        f"[DATE] RELAXATION 2: Checking date range expansion for datetime='{datetime_str}'"
    )
    if datetime_str and "/" in datetime_str:
        try:
            from datetime import datetime as dt
            from datetime import timedelta

            start, end = datetime_str.split("/")
            logger.info(f"[DATE] Parsed date range: start='{start}', end='{end}'")
            start_dt = dt.fromisoformat(start.replace("Z", ""))
            end_dt = dt.fromisoformat(end.replace("Z", ""))
            current_days = (end_dt - start_dt).days
            logger.info(f"[DATE] Current range is {current_days} days")

            # Try expanding backwards
            for expand_days in [30, 60, 90]:
                if current_days >= expand_days:
                    continue

                logger.info(
                    f"[DATE] Trying alternative with {expand_days}-day date range..."
                )

                new_start = end_dt - timedelta(days=expand_days)
                new_datetime = f"{new_start.isoformat()}Z/{end_dt.isoformat()}Z"
                logger.info(f"[DATE] Expanded datetime: {new_datetime}")

                alt_query = original_stac_query.copy()
                alt_query["datetime"] = new_datetime
                logger.info(
                    f"[DATE] Alt query collections: {alt_query.get('collections')}, bbox: {alt_query.get('bbox')}"
                )

                try:
                    alt_response = await execute_direct_stac_search(
                        alt_query, stac_endpoint
                    )
                    logger.info(
                        f"[DATE] Alt response success: {alt_response.get('success')}, features: {len(alt_response.get('results', {}).get('features', []))}"
                    )

                    if alt_response.get("success"):
                        alt_features = alt_response.get("results", {}).get(
                            "features", []
                        )

                        if alt_features:
                            # Apply filtering
                            if translator and requested_bbox:
                                filtered_results = (
                                    translator._filter_stac_results_by_spatial_overlap(
                                        {"features": alt_features},
                                        requested_bbox,
                                        min_overlap=0.1,
                                    )
                                )
                                alt_features = filtered_results.get("features", [])

                            if alt_features:
                                from tile_selector import TileSelector

                                # Use same dynamic tile limit as main query
                                fallback_max_tiles = 50
                                if requested_bbox and len(requested_bbox) == 4:
                                    fb_area = (
                                        requested_bbox[2] - requested_bbox[0]
                                    ) * (requested_bbox[3] - requested_bbox[1])
                                    if fb_area > 25:
                                        fallback_max_tiles = 100
                                    elif fb_area > 5:
                                        fallback_max_tiles = 60
                                selected_features = TileSelector.select_best_tiles(
                                    features=alt_features,
                                    query_bbox=requested_bbox,
                                    collections=alt_query.get("collections"),
                                    max_tiles=fallback_max_tiles,
                                    query=original_query,
                                )

                                if selected_features:
                                    logger.info(
                                        f"[OK] Found {len(selected_features)} results with {expand_days}-day range"
                                    )
                                    return {
                                        "success": True,
                                        "features": selected_features,
                                        "relaxation_applied": f"date_range_expanded_to_{expand_days}_days",
                                        "original_filters": original_filters,
                                        "alternative_filters": {
                                            **original_filters,
                                            "datetime": new_datetime,
                                            "days_expanded": expand_days,
                                        },
                                        "explanation": f"Expanded date range from {current_days} to {expand_days} days",
                                    }
                except Exception as e:
                    logger.warning(
                        f"[WARN] Date expansion failed for {expand_days} days: {type(e).__name__}: {e}"
                    )
                    import traceback

                    logger.warning(f"[WARN] Traceback: {traceback.format_exc()}")
                    continue
        except Exception as e:
            logger.warning(f"[WARN] Date parsing failed: {type(e).__name__}: {e}")
            import traceback

            logger.warning(f"[WARN] Traceback: {traceback.format_exc()}")

    # RELAXATION 3: Try related collections (if applicable)
    # NOTE: Removed HLS -> Landsat fallback - when user explicitly requests HLS,
    # they should get HLS only, not mixed results with Landsat
    # The 500 errors on some HLS tiles are a Planetary Computer issue, not a collection issue
    original_collections = original_stac_query.get("collections", [])
    alternative_collection_sets = []

    # Only fall back for Sentinel -> Landsat (same optical imagery category)
    # Do NOT fall back for HLS - user explicitly requested harmonized data
    if any("sentinel" in c.lower() for c in original_collections) and not any(
        "hls" in c.lower() for c in original_collections
    ):
        alternative_collection_sets.append(
            {"collections": ["landsat-c2-l2"], "name": "Landsat"}
        )

    for alt_collections in alternative_collection_sets:
        logger.info(
            f"[SAT] Trying alternative collections: {alt_collections['name']}..."
        )

        alt_query = original_stac_query.copy()
        alt_query["collections"] = alt_collections["collections"]

        try:
            alt_response = await execute_direct_stac_search(alt_query, stac_endpoint)

            if alt_response.get("success"):
                alt_features = alt_response.get("results", {}).get("features", [])

                if alt_features:
                    # Apply filtering
                    if translator and requested_bbox:
                        filtered_results = (
                            translator._filter_stac_results_by_spatial_overlap(
                                {"features": alt_features},
                                requested_bbox,
                                min_overlap=0.1,
                            )
                        )
                        alt_features = filtered_results.get("features", [])

                    if alt_features:
                        from tile_selector import TileSelector

                        # Use same dynamic tile limit as main query
                        fallback_max_tiles = 50
                        if requested_bbox and len(requested_bbox) == 4:
                            fb_area = (requested_bbox[2] - requested_bbox[0]) * (
                                requested_bbox[3] - requested_bbox[1]
                            )
                            if fb_area > 25:
                                fallback_max_tiles = 100
                            elif fb_area > 5:
                                fallback_max_tiles = 60
                        selected_features = TileSelector.select_best_tiles(
                            features=alt_features,
                            query_bbox=requested_bbox,
                            collections=alt_query.get("collections"),
                            max_tiles=fallback_max_tiles,
                            query=original_query,
                        )

                        if selected_features:
                            logger.info(
                                f"[OK] Found {len(selected_features)} results with {alt_collections['name']}"
                            )
                            return {
                                "success": True,
                                "features": selected_features,
                                "relaxation_applied": f"collections_changed_to_{alt_collections['name'].lower()}",
                                "original_filters": original_filters,
                                "alternative_filters": {
                                    **original_filters,
                                    "collections": alt_collections["collections"],
                                },
                                "explanation": f"Used {alt_collections['name']} instead of {', '.join(original_collections)}",
                            }
        except Exception as e:
            logger.warning(f"[WARN] Alternative collection search failed: {e}")
            continue

    # No alternatives found
    logger.info("[FAIL] No alternatives found with relaxed filters")
    return {
        "success": False,
        "features": [],
        "relaxation_applied": "none",
        "original_filters": original_filters,
        "alternative_filters": original_filters,
        "explanation": "No alternatives found",
    }


def build_stac_query(stac_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build STAC search query from semantic parameters (ported from Router Function App)

    NOTE: MODIS items use start_datetime/end_datetime instead of datetime,
    but the STAC API still correctly filters MODIS by datetime range.
    We DO apply datetime filter for MODIS collections.
    """
    query = {}

    # Add collections
    if stac_params.get("collections"):
        query["collections"] = stac_params["collections"]

    # Add temporal filter for ALL collections (including MODIS)
    # MODIS items use start_datetime/end_datetime but STAC API filters correctly
    if stac_params.get("datetime"):
        query["datetime"] = stac_params["datetime"]
        logger.info(
            f"[DATE] Adding datetime filter to STAC query: {stac_params['datetime']}"
        )

    # Add spatial filter (bbox) - required for MODIS!
    if stac_params.get("bbox"):
        query["bbox"] = stac_params["bbox"]

    # Add limit - uses dynamic value from semantic_translator (based on bbox size)
    # Default 50 is fallback, but semantic_translator calculates optimal limit from bbox area
    query["limit"] = stac_params.get("limit", 50)

    # Add sortby - CRITICAL for getting most recent imagery when no datetime filter
    # Without this, STAC API returns results in undefined order (often oldest first)
    if stac_params.get("sortby"):
        query["sortby"] = stac_params["sortby"]
        logger.info(f"[CHART] Adding sortby to STAC query: {stac_params['sortby']}")
    else:
        # Default: sort by datetime descending to get most recent imagery first
        query["sortby"] = [{"field": "datetime", "direction": "desc"}]
        logger.info(
            "[CHART] Adding default sortby (datetime desc) to get most recent imagery"
        )

    # Add query parameters (CQL2 extension)
    if stac_params.get("query"):
        query["query"] = stac_params["query"]

    # Add cloud cover filter — server-side for APIs that support it (PC, stac-fastapi)
    # Client-side filtering in execute_direct_stac_search also uses this value as a safety net
    if stac_params.get("cloud_cover") is not None:
        cloud_val = int(stac_params["cloud_cover"])
        q = query.setdefault("query", {})
        q["eo:cloud_cover"] = {"lte": cloud_val}  # Sentinel-2, Landsat
        q["cloud_cover"] = {"lte": cloud_val}  # HLS / other providers
        logger.info(f"[CLOUD] Adding cloud cover filter to STAC query: ≤{cloud_val}%")

    return query
