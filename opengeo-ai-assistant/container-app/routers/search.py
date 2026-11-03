"""STAC/VEDA search, structured search, mosaic signing and collection listing."""

import asyncio
import json
import logging
import os
import traceback
from datetime import datetime, timezone

import aiohttp
import app_state
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from request_models import (
    SignMosaicUrlRequest,
    StacSearchRequest,
    StructuredSearchRequest,
    VedaSearchRequest,
)
from services.stac_search import build_stac_query, execute_direct_stac_search
from services.tile_urls import clean_tilejson_urls

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/stac/collections")
async def get_stac_collections():
    """Return the list of collections available in the configured private STAC API."""
    stac_api_url = os.getenv("STAC_API_URL", "").strip()
    if not stac_api_url:
        return JSONResponse(content={"configured": False, "collections": []})
    catalog_url = stac_api_url.rsplit("/search", 1)[0].rstrip("/")
    collections_url = f"{catalog_url}/collections"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.get(collections_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    cols = [
                        {
                            "id": c["id"],
                            "title": c.get("title", c["id"]),
                            "description": c.get("description", ""),
                        }
                        for c in data.get("collections", [])
                        if "id" in c
                    ]
                    return JSONResponse(
                        content={"configured": True, "collections": cols}
                    )
                return JSONResponse(
                    content={
                        "configured": True,
                        "collections": [],
                        "error": f"HTTP {resp.status}",
                    }
                )
    except Exception as e:
        logger.error(f"[FAIL] /api/stac/collections error: {e}")
        return JSONResponse(
            content={"configured": True, "collections": [], "error": str(e)}
        )


@router.post("/api/sign-mosaic-url")
async def sign_mosaic_url(body: SignMosaicUrlRequest):
    """Sign a Planetary Computer URL with SAS token for authenticated tile access."""
    try:
        url = body.url

        try:
            import planetary_computer

            signed_url = planetary_computer.sign_url(url)
            authenticated = signed_url != url
        except Exception as e:
            logger.warning(f"[WARN] PC URL signing failed: {e}, returning original URL")
            signed_url = url
            authenticated = False

        return JSONResponse(
            content={"signed_url": signed_url, "authenticated": authenticated}
        )

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.error("[FAIL] sign-mosaic-url timed out")
        raise HTTPException(status_code=504, detail="URL signing timed out")
    except Exception as e:
        logger.error(f"[FAIL] sign-mosaic-url error: {e}")
        raise HTTPException(status_code=500, detail="URL signing failed")


@router.post("/api/stac-search")
async def stac_search(body: StacSearchRequest):
    """Direct STAC search endpoint for backwards compatibility (ported from Router Function App)"""
    try:
        logger.info("[SEARCH] Direct STAC search endpoint called")

        # Execute STAC search
        stac_response = await execute_direct_stac_search(
            body.model_dump(exclude_none=True)
        )

        # Clean tilejson URLs in the response
        if stac_response.get("success") and "results" in stac_response:
            stac_response["results"] = clean_tilejson_urls(stac_response["results"])

        return JSONResponse(
            content=stac_response,
            status_code=200 if stac_response.get("success") else 500,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"STAC search endpoint error: {e}")
        raise HTTPException(status_code=500, detail="STAC search failed")


@router.post("/api/veda-search")
async def veda_search(body: VedaSearchRequest):
    """Direct VEDA STAC search endpoint for NASA Earth data (ported from Router Function App)"""
    try:
        logger.info("[SAT] VEDA STAC search endpoint called")

        # Execute VEDA STAC search
        stac_response = await execute_direct_stac_search(
            body.model_dump(exclude_none=True), stac_endpoint="veda"
        )

        # Clean tilejson URLs in the response
        if stac_response.get("success") and "results" in stac_response:
            stac_response["results"] = clean_tilejson_urls(stac_response["results"])

        return JSONResponse(
            content=stac_response,
            status_code=200 if stac_response.get("success") else 500,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VEDA search endpoint error: {e}")
        raise HTTPException(status_code=500, detail="VEDA search failed")


@router.post("/api/structured-search")
async def structured_search(body: StructuredSearchRequest):
    """
    Structured STAC search using explicit parameters (collection, location, datetime)
    Leverages the same agents as natural language queries for consistency
    """
    try:
        logger.info("[TOOL] Structured search endpoint called")
        logger.info("=" * 100)
        logger.info(
            "[TOOL][TOOL][TOOL] POST /api/structured-search ENDPOINT HIT [TOOL][TOOL][TOOL]"
        )
        logger.info("=" * 100)

        collection = body.collection
        location = body.location
        datetime_single = body.datetime
        datetime_start = body.datetime_start
        datetime_end = body.datetime_end

        logger.info("[PKG] Structured search params:")
        logger.info(f"   Collection: {collection}")
        logger.info(f"   Location: {location}")
        logger.info(f"   Datetime (single): {datetime_single}")
        logger.info(f"   Datetime range: {datetime_start} to {datetime_end}")

        if (
            not collection or not location
        ):  # belt-and-suspenders (Pydantic already enforces these)
            raise HTTPException(
                status_code=400,
                detail="Both 'collection' and 'location' parameters are required",
            )

        # Ensure semantic translator is initialized
        if not app_state.global_translator:
            logger.error("[FAIL] Semantic translator not initialized")
            raise HTTPException(
                status_code=500,
                detail="Semantic translator not initialized. Please try again.",
            )

        # ========================================================================
        # [BOT] USE FULL MULTI-AGENT PIPELINE FOR STRUCTURED SEARCH
        # ========================================================================
        # Build a natural language query from structured parameters to leverage
        # the full agent pipeline (collection validation, datetime parsing,
        # spatial coverage, rendering config, etc.)
        # ========================================================================

        logger.info(
            "[BOT] Converting structured parameters to natural language for agent pipeline"
        )

        # Build natural language query
        nl_parts = []
        if collection:
            nl_parts.append(f"{collection} data")
        if location:
            nl_parts.append(f"for {location}")
        if datetime_start and datetime_end:
            nl_parts.append(f"from {datetime_start} to {datetime_end}")
        elif datetime_single:
            nl_parts.append(f"on {datetime_single}")

        natural_query = " ".join(nl_parts)
        logger.info(f"[NOTE] Constructed natural language query: '{natural_query}'")

        # Use the semantic translator's translate_query method (FULL AGENT PIPELINE)
        logger.info("[SYNC] Running full multi-agent translation pipeline...")
        stac_params = await app_state.global_translator.translate_query(
            natural_query, pin_location=None
        )

        # Check if translation was successful
        if not stac_params or not stac_params.get("collections"):
            logger.error(
                "[FAIL] Agent pipeline failed to produce valid STAC parameters"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Could not translate parameters to STAC query. Collection '{collection}' may not exist or location '{location}' could not be resolved.",
            )

        # Build STAC query from agent-validated parameters
        stac_query = build_stac_query(stac_params)
        logger.info(f"� Agent-validated STAC Query: {json.dumps(stac_query, indent=2)}")

        # Get location info for response
        location_name = stac_params.get("location_name", location)
        bbox = stac_query.get("bbox")
        datetime_param = stac_query.get("datetime")

        # Execute STAC search with agent-optimized parameters
        stac_response = await execute_direct_stac_search(stac_query)

        if not stac_response.get("success"):
            return JSONResponse(
                content={
                    "success": False,
                    "response": f"No data found for {collection} in {location_name}",
                    "user_response": f"No data found for {collection} in {location_name}. Try adjusting the date range or location.",
                    "data": stac_response,
                },
                status_code=200,
            )

        # Clean tilejson URLs
        if "results" in stac_response:
            stac_response["results"] = clean_tilejson_urls(stac_response["results"])

        # Build response similar to /api/query format
        num_items = len(stac_response.get("results", {}).get("features", []))
        response_text = f"Found {num_items} {collection} items for {location_name}"
        if datetime_param:
            response_text += f" ({datetime_start or datetime_single}"
            if datetime_end:
                response_text += f" to {datetime_end}"
            response_text += ")"

        logger.info(f"[OK] Structured search complete: {num_items} items found")

        return JSONResponse(
            content={
                "success": True,
                "response": response_text,
                "user_response": response_text,
                "data": {
                    "stac_results": stac_response.get("results", {}),
                    "search_metadata": {
                        "total_items": num_items,
                        "collections_searched": [collection],
                        "spatial_extent": bbox,
                        "temporal_range": datetime_param,
                        "location_name": location_name,
                        "search_timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    "query_classification": {
                        "intent_type": "stac",
                        "needs_satellite_data": True,
                        "confidence": 1.0,
                    },
                },
                "translation_metadata": {
                    "original_query": f"{collection} for {location}",
                    "translated_params": stac_query,
                    "stac_query": stac_query,
                    "translation_method": "structured_input",
                },
            },
            status_code=200,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Structured search error: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Structured search failed")
