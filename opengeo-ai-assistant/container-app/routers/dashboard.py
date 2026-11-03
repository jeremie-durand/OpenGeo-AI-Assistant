"""Dashboard data: renderable collection catalog and focused collection stats."""

import asyncio
import logging
from typing import Any, Dict

import aiohttp
from fastapi import APIRouter, HTTPException
from request_models import DashboardStatsRequest
from services.dashboard_catalog import CatalogUnavailableError, get_catalog
from services.dashboard_stats import StatsUpstreamError, collection_stats

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/dashboard/collections")
async def dashboard_collections() -> Dict[str, Any]:
    """Return id, title, category and bbox for every renderable collection."""
    try:
        return await get_catalog()
    except CatalogUnavailableError as e:
        logger.error(f"[FAIL] Dashboard catalog unavailable: {e}")
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/api/dashboard/stats")
async def dashboard_stats(body: DashboardStatsRequest) -> Dict[str, Any]:
    """Return scene count, monthly scenes, cloud cover and latest date for a bbox."""
    try:
        return await collection_stats(body.collection, body.bbox)
    except asyncio.TimeoutError:
        logger.error(f"[FAIL] Dashboard stats timed out: {body.collection}")
        raise HTTPException(status_code=504, detail="STAC search timed out")
    except (StatsUpstreamError, aiohttp.ClientError) as e:
        logger.error(f"[FAIL] Dashboard stats upstream error: {e}")
        raise HTTPException(status_code=502, detail="STAC search failed")
