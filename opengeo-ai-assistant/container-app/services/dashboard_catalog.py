"""Slim catalog of renderable collections with spatial extents, cached in memory."""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp
from pc_tasks_config_loader import (
    COLLECTION_ID_ALIASES,
    get_all_collection_ids,
    get_collection_title,
    load_pc_metadata,
)

logger = logging.getLogger(__name__)

PC_COLLECTIONS_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/collections"
CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 15
UNCATEGORIZED = "Uncategorized"
WORLD_BBOX = [-180.0, -90.0, 180.0, 90.0]
GLOBAL_MIN_WIDTH = 350.0
GLOBAL_MIN_HEIGHT = 160.0

_cache: Optional[Dict[str, Any]] = None
_lock = asyncio.Lock()


class CatalogUnavailableError(RuntimeError):
    """Raised when no catalog can be built and none is cached."""


def _stac_id_by_config_id() -> Dict[str, str]:
    """Map rendering-config ids to STAC ids, preferring the first alias listed."""
    mapping = {config_id: config_id for config_id in get_all_collection_ids()}
    for stac_id, config_id in reversed(list(COLLECTION_ID_ALIASES.items())):
        if config_id in mapping:
            mapping[config_id] = stac_id
    return mapping


def renderable_collection_ids() -> List[str]:
    """Return the STAC ids of collections the map can render."""
    return sorted(_stac_id_by_config_id().values())


def _category_by_collection() -> Dict[str, str]:
    """Map each collection id to its category name from the rendering config."""
    mapping: Dict[str, str] = {}
    for category in load_pc_metadata().get("categories", []):
        for collection in category.get("collections", []):
            mapping[collection.get("id")] = category.get("name", UNCATEGORIZED)
    return mapping


def _overall_bbox(collection: Dict[str, Any]) -> List[float]:
    """Return the collection's overall STAC extent, or the whole world."""
    bboxes = collection.get("extent", {}).get("spatial", {}).get("bbox") or []
    if bboxes and len(bboxes[0]) == 4:
        return [float(value) for value in bboxes[0]]
    return list(WORLD_BBOX)


def is_global_bbox(bbox: List[float]) -> bool:
    """Return True when a bbox spans (nearly) the whole globe."""
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    return width >= GLOBAL_MIN_WIDTH and height >= GLOBAL_MIN_HEIGHT


def build_catalog(upstream: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep renderable collections and slim them to the fields the dashboard uses."""
    stac_ids = _stac_id_by_config_id()
    categories = _category_by_collection()
    extents = {c.get("id"): c for c in upstream}
    catalog = []
    for config_id, stac_id in sorted(stac_ids.items(), key=lambda item: item[1]):
        bbox = _overall_bbox(extents.get(stac_id, {}))
        catalog.append(
            {
                "id": stac_id,
                "title": get_collection_title(config_id) or stac_id,
                "category": categories.get(config_id) or UNCATEGORIZED,
                "bbox": bbox,
                "is_global": is_global_bbox(bbox),
            }
        )
    return catalog


async def fetch_upstream_collections() -> List[Dict[str, Any]]:
    """Fetch the full collection list from Planetary Computer."""
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(PC_COLLECTIONS_URL) as response:
            response.raise_for_status()
            data = await response.json()
    return data.get("collections", [])


def _is_fresh(cache: Optional[Dict[str, Any]]) -> bool:
    """Return True when the cached catalog is within its TTL."""
    return bool(cache) and time.time() - cache["fetched_at"] < CATALOG_TTL_SECONDS


async def get_catalog() -> Dict[str, Any]:
    """Return the cached catalog, refreshing it when stale.

    A failed refresh keeps serving the stale catalog; with nothing cached it
    raises CatalogUnavailableError.
    """
    global _cache
    if _is_fresh(_cache):
        return _cache
    async with _lock:
        if _is_fresh(_cache):
            return _cache
        try:
            upstream = await fetch_upstream_collections()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if _cache:
                logger.warning(f"[WARN] Dashboard catalog refresh failed: {e}")
                return _cache
            raise CatalogUnavailableError(
                "Dataset catalog is unavailable right now"
            ) from e
        _cache = {"collections": build_catalog(upstream), "fetched_at": time.time()}
        logger.info(
            f"[OK] Dashboard catalog built: {len(_cache['collections'])} collections"
        )
        return _cache


async def warm_catalog() -> None:
    """Build the catalog at startup; failures are logged, never raised."""
    try:
        await get_catalog()
    except Exception as e:
        logger.warning(f"[WARN] Dashboard catalog warm-up failed: {e}")


def reset_cache() -> None:
    """Drop the cached catalog and its lock (used by tests)."""
    global _cache, _lock
    _cache = None
    _lock = asyncio.Lock()
