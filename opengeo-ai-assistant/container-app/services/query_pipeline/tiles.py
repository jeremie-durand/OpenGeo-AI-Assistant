"""Pure helpers for tile URLs, bboxes and mosaic eligibility."""

from typing import Any, List, Optional, Sequence

OPTICAL_MOSAIC_COLLECTIONS = (
    "sentinel-2-l2a",
    "landsat-c2-l2",
    "landsat-8-c2-l2",
    "landsat-9-c2-l2",
    "hls",
    "hls2-l30",
    "hls2-s30",
)
REPLACED_TILE_PARAMS = (
    "assets=",
    "asset_bidx=",
    "colormap_name=",
    "rescale=",
    "resampling=",
    "bidx=",
    "format=",
    "nodata=",
    "color_formula=",
    "expression=",
    "asset_as_band=",
)


def needs_optical_mosaic(collection_id: str) -> bool:
    """Return True for optical collections whose granules need a mosaic."""
    return any(
        collection_id.startswith(oc) or collection_id == oc
        for oc in OPTICAL_MOSAIC_COLLECTIONS
    )


def has_numeric_bbox(bbox: Optional[Sequence[Any]]) -> bool:
    """Return True for a 4-value bbox whose values are all numbers."""
    if not bbox or len(bbox) != 4:
        return False
    return not any(v is None or not isinstance(v, (int, float)) for v in bbox)


def optimize_tilejson_url(original_url: str, quality_params: Optional[str]) -> str:
    """Swap the URL's rendering params for the collection's quality params.

    Stripping nodata= avoids grey Sentinel-2 tiles; stripping the duplicated
    assets=, asset_bidx=, color_formula= and expression= avoids 404s and SAR
    conflicts.
    """
    if not quality_params:
        return original_url
    if "?" not in original_url:
        return f"{original_url}?{quality_params}"
    base_url, existing_params = original_url.split("?", 1)
    param_list = [
        p
        for p in existing_params.split("&")
        if not any(p.startswith(k) for k in REPLACED_TILE_PARAMS)
    ]
    if param_list:
        return f"{base_url}?{'&'.join(param_list)}&{quality_params}"
    return f"{base_url}?{quality_params}"


def union_of_bboxes(bboxes: Sequence[Sequence[float]]) -> List[float]:
    """Return the smallest bbox covering every bbox in the list."""
    return [
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    ]
