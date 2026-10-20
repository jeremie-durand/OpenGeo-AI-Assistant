"""Optical tile de-duplication so overlapping acquisitions show one tile per grid."""

import logging
from typing import Any, Dict, List, Optional

OPTICAL_COLLECTIONS = [
    "sentinel-2-l2a",
    "sentinel-2-l1c",
    "landsat-c2-l2",
    "landsat-c2-l1",
    "landsat-8-c2-l2",
    "landsat-9-c2-l2",
    "hls-l30",
    "hls-s30",
    "hls2-l30",
    "hls2-s30",
    "naip",
]


TEMPORAL_KEYWORDS = [
    "change",
    "time series",
    "progression",
    "over time",
    "compare",
    "before and after",
    "between",
    "from",
    "to",
    "trend",
    "evolution",
    "history",
    "historical",
    "monthly",
    "weekly",
    "daily",
]


SKIP_DEDUP_COLLECTIONS = [
    "modis-14A1-061",
    "modis-14A2-061",  # Fire detection
    "sentinel-1-rtc",
    "sentinel-1-grd",  # SAR (different polarizations)
]


def should_deduplicate_tiles(
    collections: List[str], original_query: str = None
) -> bool:
    """
    Determine if tile deduplication should be applied.

    Returns True only for optical imagery queries without explicit temporal intent.
    """
    if not collections:
        return False

    # Check if any collection is optical
    is_optical = any(
        any(opt in coll.lower() for opt in OPTICAL_COLLECTIONS) for coll in collections
    )

    if not is_optical:
        logging.debug(f"[SYNC] Dedup: Skipping - not optical collection: {collections}")
        return False

    # Check if any collection should never be deduplicated
    should_skip = any(
        any(skip in coll.lower() for skip in SKIP_DEDUP_COLLECTIONS)
        for coll in collections
    )

    if should_skip:
        logging.debug(f"[SYNC] Dedup: Skipping - fire/SAR collection: {collections}")
        return False

    # Check for temporal keywords in query
    if original_query:
        query_lower = original_query.lower()
        has_temporal_intent = any(kw in query_lower for kw in TEMPORAL_KEYWORDS)
        if has_temporal_intent:
            logging.info(
                f"[SYNC] Dedup: Skipping - temporal query detected: '{original_query[:50]}...'"
            )
            return False

    logging.info(f"[OK] Dedup: Will deduplicate optical tiles for: {collections}")
    return True


def extract_tile_grid_id(feature: Dict[str, Any]) -> Optional[str]:
    """
    Extract the grid/tile ID from a STAC feature.

    Works for:
    - Sentinel-2: MGRS tile ID (e.g., T37VDC)
    - Landsat: WRS-2 path/row (e.g., path_168_row_37)
    - HLS: MGRS tile ID
    """
    feature_id = feature.get("id", "")
    collection = feature.get("collection", "")
    properties = feature.get("properties", {})

    # Sentinel-2 / HLS: Extract MGRS tile from ID
    # Format: S2B_MSIL2A_20251229T085259_R107_T37VDC_20251229T110511
    # The MGRS tile is the part starting with 'T' followed by numbers+letters
    if "sentinel-2" in collection.lower() or "hls" in collection.lower():
        import re

        mgrs_match = re.search(r"_T(\d{2}[A-Z]{3})_", feature_id)
        if mgrs_match:
            return f"T{mgrs_match.group(1)}"
        # Also check properties
        if "s2:mgrs_tile" in properties:
            return properties["s2:mgrs_tile"]

    # Landsat: Extract path/row
    # Format: LC09_L2SP_168037_20251225_... or properties contain path/row
    if "landsat" in collection.lower():
        # Check properties first
        if "landsat:wrs_path" in properties and "landsat:wrs_row" in properties:
            return f"p{properties['landsat:wrs_path']}_r{properties['landsat:wrs_row']}"
        # Extract from ID
        import re

        pathrow_match = re.search(r"_(\d{6})_", feature_id)
        if pathrow_match:
            return pathrow_match.group(1)

    # Fallback: Use bbox as a rough grid ID (round to 1 degree)
    bbox = feature.get("bbox")
    if bbox and len(bbox) >= 4:
        # Round to nearest degree for grid cell
        return f"grid_{int(bbox[0])}_{int(bbox[1])}"

    return None


def deduplicate_tiles_by_grid(
    features: List[Dict[str, Any]], original_query: str = None
) -> List[Dict[str, Any]]:
    """
    Deduplicate STAC features by grid location, keeping only the most recent tile per grid cell.

    This prevents multiple tiles from different dates overlapping on the same location.
    Features are assumed to be sorted by datetime descending (most recent first).

    Args:
        features: List of STAC features (sorted by datetime desc)
        original_query: Original user query (to check for temporal intent)

    Returns:
        Deduplicated list of features (one per grid cell)
    """
    if not features:
        return features

    # Get collection from first feature
    collections = list(
        set(f.get("collection", "") for f in features if f.get("collection"))
    )

    # Check if we should deduplicate
    if not should_deduplicate_tiles(collections, original_query):
        return features

    original_count = len(features)
    seen_grids = set()
    deduplicated = []

    for feature in features:
        grid_id = extract_tile_grid_id(feature)

        if grid_id is None:
            # Can't determine grid - keep the feature
            deduplicated.append(feature)
            continue

        if grid_id not in seen_grids:
            seen_grids.add(grid_id)
            deduplicated.append(feature)
        else:
            # Skip - we already have a more recent tile for this grid
            logging.debug(
                f"[SYNC] Dedup: Skipping duplicate grid {grid_id} (feature: {feature.get('id', 'unknown')[:50]})"
            )

    if len(deduplicated) < original_count:
        logging.info(
            f"[TARGET] TILE DEDUPLICATION: {original_count} -> {len(deduplicated)} tiles (removed {original_count - len(deduplicated)} duplicates)"
        )
        logging.info(
            f"[TARGET] Unique grid cells: {list(seen_grids)[:10]}{'...' if len(seen_grids) > 10 else ''}"
        )

    return deduplicated
