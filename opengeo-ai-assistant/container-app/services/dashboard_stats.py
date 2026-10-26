"""Availability statistics of one collection over a bbox from one trimmed search."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
from pc_tasks_config_loader import (
    get_cloud_cover_property,
    supports_cloud_filtering,
    supports_temporal_filtering,
)
from services.stac_search import STAC_ENDPOINTS

logger = logging.getLogger(__name__)

ITEM_LIMIT = 200
MONTHS = 12
CLOUD_BUCKET_WIDTH = 20
CLOUD_BUCKETS = 100 // CLOUD_BUCKET_WIDTH
CLOUD_BUCKET_LABELS = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]
SEARCH_TIMEOUT_SECONDS = 10


class StatsUpstreamError(RuntimeError):
    """Raised when the STAC search fails or returns a non-200 status."""


def _month_keys(now: datetime) -> List[str]:
    """Return the last MONTHS months as "YYYY-MM", oldest first, ending at now."""
    keys = []
    year, month = now.year, now.month
    for _ in range(MONTHS):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return keys[::-1]


def _window_start(now: datetime) -> datetime:
    """Return the first instant of the oldest month in the window."""
    oldest = _month_keys(now)[0]
    return datetime(int(oldest[:4]), int(oldest[5:]), 1, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_search_body(
    collection: str, bbox: List[float], now: datetime
) -> Dict[str, Any]:
    """Build the trimmed STAC search body; static collections get no datetime."""
    cloud_property = get_cloud_cover_property(collection)
    body: Dict[str, Any] = {
        "collections": [collection],
        "bbox": bbox,
        "limit": ITEM_LIMIT,
        "fields": {
            "include": [
                "id",
                "properties.datetime",
                "properties.start_datetime",
                f"properties.{cloud_property}",
            ],
            "exclude": ["assets", "links", "geometry"],
        },
    }
    if supports_temporal_filtering(collection):
        body["datetime"] = f"{_iso(_window_start(now))}/{_iso(now)}"
    return body


def _item_datetime(feature: Dict[str, Any]) -> Optional[str]:
    properties = feature.get("properties") or {}
    return properties.get("datetime") or properties.get("start_datetime")


def _monthly(datetimes: List[str], now: datetime) -> List[Dict[str, Any]]:
    counts = {key: 0 for key in _month_keys(now)}
    for value in datetimes:
        if value[:7] in counts:
            counts[value[:7]] += 1
    return [{"month": month, "count": count} for month, count in counts.items()]


def _cloud_cover(
    features: List[Dict[str, Any]], cloud_property: str
) -> Optional[List[int]]:
    """Histogram of cloud cover in 20-point buckets, None when no item has it."""
    values = [
        (f.get("properties") or {}).get(cloud_property)
        for f in features
        if isinstance((f.get("properties") or {}).get(cloud_property), (int, float))
    ]
    if not values:
        return None
    buckets = [0] * CLOUD_BUCKETS
    for value in values:
        index = min(int(max(value, 0) // CLOUD_BUCKET_WIDTH), CLOUD_BUCKETS - 1)
        buckets[index] += 1
    return buckets


def summarize(
    collection: str,
    bbox: List[float],
    features: List[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    """Turn search features into the dashboard stats response."""
    static = not supports_temporal_filtering(collection)
    datetimes = [d for d in (_item_datetime(f) for f in features) if d]
    stats: Dict[str, Any] = {
        "collection": collection,
        "bbox": bbox,
        "scene_count": len(features),
        "capped": len(features) >= ITEM_LIMIT,
        "static": static,
        "latest_datetime": max(datetimes) if datetimes else None,
    }
    if not static:
        stats["monthly"] = _monthly(datetimes, now)
    cloud = (
        _cloud_cover(features, get_cloud_cover_property(collection))
        if supports_cloud_filtering(collection)
        else None
    )
    if cloud is not None:
        stats["cloud_cover"] = cloud
    return stats


async def search_features(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run one search against Planetary Computer and return its features.

    Always queries Planetary Computer, even when STAC_API_URL points
    elsewhere, because the catalog's collections come from Planetary Computer.
    """
    timeout = aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            STAC_ENDPOINTS["planetary_computer"], json=body
        ) as response:
            if response.status != 200:
                raise StatsUpstreamError(f"STAC search returned {response.status}")
            data = await response.json()
    return data.get("features", [])


async def collection_stats(
    collection: str, bbox: List[float], now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Return availability stats for a renderable collection over a bbox."""
    now = now or datetime.now(timezone.utc)
    features = await search_features(build_search_body(collection, bbox, now))
    logger.info(
        f"[OK] Dashboard stats: {collection} bbox={bbox} -> {len(features)} items"
    )
    return summarize(collection, bbox, features, now)
