"""Keyword-based collection detection used when the LLM mapping is unavailable."""

import logging
from typing import List

logger = logging.getLogger(__name__)


def detect_collections(query: str) -> List[str]:
    """
    Enhanced collection detection from Router Function App - detects relevant collections based on query keywords
    """
    query = query.lower()
    detected_collections = []

    # Priority mapping for common use cases (matches Router Function App logic)
    if any(term in query for term in ["fire", "wildfire", "burn", "thermal", "heat"]):
        # Fire detection - HIGH RESOLUTION FIRST for detailed fire perimeter analysis
        # Landsat (30m thermal) + Sentinel-2 (10m optical smoke) show detailed fire boundaries
        # MODIS (1km) included last for wide-area context only
        detected_collections.extend(
            ["landsat-c2-l2", "sentinel-2-l2a", "modis-14A1-061"]
        )

    elif any(
        term in query
        for term in ["elevation", "dem", "topography", "terrain", "height"]
    ):
        detected_collections.extend(["cop-dem-glo-30", "cop-dem-glo-90", "alos-dem"])

    elif any(term in query for term in ["radar", "sar", "interferometry"]):
        detected_collections.extend(["sentinel-1-grd", "sentinel-1-rtc"])

    elif any(term in query for term in ["optical", "rgb", "visible", "true color"]):
        detected_collections.extend(["sentinel-2-l2a", "landsat-c2-l2"])

    elif any(term in query for term in ["vegetation", "ndvi", "agriculture", "crop"]):
        # Vegetation/NDVI - HIGH RESOLUTION FIRST for detailed crop analysis
        # Sentinel-2 (10m) and Landsat (30m) provide field-level detail
        # MODIS (250m-500m) included last for large-scale regional trends only
        detected_collections.extend(
            ["sentinel-2-l2a", "landsat-c2-l2", "modis-13A1-061"]
        )

    elif any(term in query for term in ["water", "ocean", "sea", "lake", "river"]):
        detected_collections.extend(
            ["sentinel-2-l2a", "landsat-c2-l2", "sentinel-1-grd"]
        )

    else:
        # Default collections for general queries - HIGH RESOLUTION PRIORITY
        # Only Sentinel-2 (10m) and Landsat (30m) to ensure crisp imagery for cities/detailed areas
        # MODIS (500m) excluded from default as it's too coarse for most use cases (4 pixels per city block)
        detected_collections.extend(["sentinel-2-l2a", "landsat-c2-l2"])

    # Remove duplicates while preserving order.
    # NOTE: PC metadata is incomplete (doesn't list all PC collections like sentinel-2-l2a),
    # so we skip the metadata validation filter to avoid silently dropping valid collections.
    seen = set()
    unique_collections = []
    for collection in detected_collections:
        if collection not in seen:
            seen.add(collection)
            unique_collections.append(collection)

    logger.info(f"[SEARCH] Detected collections for '{query}': {unique_collections}")
    return unique_collections
