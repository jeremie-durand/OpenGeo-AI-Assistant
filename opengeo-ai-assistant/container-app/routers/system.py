"""Health, config, session reset and debug endpoints."""

import logging
import os
import traceback
from datetime import datetime, timezone

import aiohttp
import app_state
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from request_models import SessionResetRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/health")
async def health_check():
    """Lightweight health check — no GPT calls, no verbose logging."""
    try:
        checks = {}
        all_healthy = True

        # 1. LLM Client — config check only (no billable test call)
        from llm_client import LLMConfigurationError, get_llm_client

        try:
            client = get_llm_client()
            checks["llm_client"] = {
                "status": "configured",
                "provider": client.provider,
                "model": client.model,
            }
        except LLMConfigurationError as e:
            checks["llm_client"] = {"status": "misconfigured", "error": str(e)}
            all_healthy = False
        except Exception as e:
            checks["llm_client"] = {"status": "error", "error": str(e)}
            all_healthy = False

        # 2. Planetary Computer — quick catalog GET
        try:
            pc_catalog = "https://planetarycomputer.microsoft.com/api/stac/v1"
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                async with session.get(pc_catalog) as resp:
                    checks["planetary_computer"] = {
                        "status": "connected" if resp.status == 200 else "degraded"
                    }
        except Exception:
            checks["planetary_computer"] = {"status": "disconnected"}

        # 3. Private STAC API — quick GET (only if STAC_API_URL is configured)
        stac_api_url = os.getenv("STAC_API_URL", "").strip()
        if stac_api_url:
            try:
                catalog_url = stac_api_url.rsplit("/search", 1)[0]
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as session:
                    async with session.get(catalog_url) as resp:
                        checks["private_stac_api"] = {
                            "status": "connected" if resp.status == 200 else "degraded",
                            "api_url": stac_api_url,
                        }
            except Exception:
                checks["private_stac_api"] = {
                    "status": "disconnected",
                    "api_url": stac_api_url,
                }
        else:
            checks["private_stac_api"] = {"status": "not_configured"}

        overall = "healthy" if all_healthy else "degraded"
        logger.info(
            f"[BLDG] Health: {overall} | llm={checks['llm_client']['status']} pc={checks['planetary_computer']['status']} private_stac={checks['private_stac_api']['status']}"
        )

        return JSONResponse(
            content={
                "status": overall,
                "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "checks": checks,
            },
            status_code=200 if all_healthy else 503,
        )
    except Exception as e:
        logger.error(f"[FAIL] Health check error: {e}")
        return JSONResponse(
            content={"status": "unhealthy", "error": str(e)}, status_code=500
        )


@router.get("/api/config")
async def get_config():
    """Configuration endpoint for frontend - provides backend settings"""
    return {"api": {"baseUrl": "/api"}}


@router.post("/api/session-reset")
async def session_reset(body: SessionResetRequest, request: Request):
    """Reset/clear conversation context for session restart (ported from Router Function App)"""
    try:
        # Prefer validated body fields; fall back to query params for legacy callers
        conversation_id = (
            body.session_id
            or request.query_params.get("session_id")
            or request.query_params.get("conversation_id")
        )

        if not conversation_id:
            raise HTTPException(
                status_code=400,
                detail="Missing session_id or conversation_id. Please provide a session_id to reset.",
            )

        # Reset conversation context if translator is available
        if app_state.SEMANTIC_KERNEL_AVAILABLE and app_state.global_translator:
            app_state.global_translator.reset_conversation_context(conversation_id)
            message = f"Session {conversation_id} reset successfully"
        else:
            message = f"Session reset requested for {conversation_id} (semantic translator not available)"

        logger.info(f"[SYNC] {message}")

        return {
            "status": "success",
            "message": message,
            "session_id": conversation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FAIL] Session reset failed: {e}")
        raise HTTPException(status_code=500, detail="Session reset failed")


@router.get("/api/debug/location/{location}")
async def debug_location_resolver(location: str):
    """Debug endpoint to test location resolver"""
    try:
        from location_resolver import EnhancedLocationResolver

        resolver = EnhancedLocationResolver()

        # Test preprocessing
        preprocessed = resolver._preprocess_location_query(location)

        # Test resolution
        bbox = await resolver.resolve_location_to_bbox(location, "region")

        return {
            "original_query": location,
            "preprocessed_query": preprocessed,
            "resolved_bbox": bbox,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"Location resolver debug failed: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500, detail=f"Location resolver debug failed: {str(e)}"
        )
