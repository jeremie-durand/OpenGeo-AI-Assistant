"""FastAPI application: startup lifespan, middleware and router registration."""

import asyncio
import logging
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import app_state
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from quickstart_cache import get_quickstart_stats  # [LAUNCH] Pre-computed cache
from rate_limit_middleware import RateLimitMiddleware
from routers import (
    dashboard,
    geoint_analysis,
    geoint_climate,
    geoint_terrain,
    geoint_vision,
    query,
    search,
    static_files,
    system,
)
from services.dashboard_catalog import warm_catalog

logging.info("[OK] GEOINT endpoints available (lazy import mode)")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down application components."""

    logger.info("[LAUNCH] OpenGeo AI Assistant CONTAINER STARTING UP")

    # ----------------------------------------------------------------
    # Initialise provider-agnostic GenericQueryTranslator
    # ----------------------------------------------------------------
    app_state.semantic_translator = None
    app_state.global_translator = None
    app_state.SEMANTIC_KERNEL_AVAILABLE = False

    try:
        from llm_client import get_llm_client

        client = get_llm_client()
        logger.info(
            f"[OK] LLM client configured (provider={client.provider}, model={client.model})"
        )

        from generic_query_translator import GenericQueryTranslator

        _translator = GenericQueryTranslator()
        app_state.semantic_translator = _translator
        app_state.global_translator = _translator
        app_state.SEMANTIC_KERNEL_AVAILABLE = True
        logger.info(
            "[OK] GenericQueryTranslator initialised — AI query translation ENABLED"
        )
    except ImportError as e:
        logger.warning(f"[WARN] llm_client / GenericQueryTranslator not available: {e}")
    except Exception as e:
        logger.warning(
            f"[WARN] LLM client not fully configured — falling back to keyword mode: {e}"
        )

    # GEOINT endpoints use lazy imports - no initialization needed here
    logger.info("[OK] GEOINT endpoints ready (lazy import mode)")

    # Initialize RouterAgent for intelligent query classification, if available.
    try:
        from geoint.router_agent import get_router_agent

        app_state.router_agent = get_router_agent()
        logger.info("[OK] RouterAgent initialised for intelligent query routing")
    except Exception as e:
        logger.warning(
            f"[WARN] RouterAgent initialisation failed: {e} - will use fallback classification"
        )
        app_state.router_agent = None

    # Log quick start cache status
    qs_stats = get_quickstart_stats()
    logger.info(
        f"[LAUNCH] Quick Start Cache: {qs_stats['total_queries']} queries, {len(qs_stats['collections_covered'])} collections"
    )

    logger.info(
        f"[OK] OpenGeo AI Assistant CONTAINER READY (AI={'ENABLED' if app_state.SEMANTIC_KERNEL_AVAILABLE else 'DISABLED — keyword fallback'})"
    )

    catalog_warmup = asyncio.create_task(warm_catalog())

    yield

    catalog_warmup.cancel()


# Initialize FastAPI app
app = FastAPI(title="OpenGeo AI Assistant API", version="0.1.0", lifespan=lifespan)

# Rate limiting (registered first so it runs innermost — after CORS on the way in)
app.add_middleware(RateLimitMiddleware)

# Configure CORS origins from environment variable
cors_origins_str = os.environ.get("CORS_ORIGINS", "*")
cors_origins = (
    [origin.strip() for origin in cors_origins_str.split(",")]
    if cors_origins_str != "*"
    else ["*"]
)
cors_allow_credentials = cors_origins != ["*"]
if not cors_allow_credentials:
    logger.warning(
        "[CORS] allow_origins is wildcard — allow_credentials disabled "
        "(CORS spec violation). Set CORS_ORIGINS to specific origins in production."
    )
else:
    logger.info(f"[LOCK] CORS configured for origins: {cors_origins}")

# Add CORS middleware (must be outermost — runs first on requests, last on responses)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add API key auth middleware (validates X-Api-Key header on protected routes)
# Registered AFTER CORSMiddleware so CORS headers are always added, even on 401.
# Open paths (/api/health, /api/config, /) are excluded from auth.
ENABLE_AUTH = os.environ.get("ENABLE_AUTH", "false").lower() == "true"
if ENABLE_AUTH:
    from auth_middleware import ApiKeyAuthMiddleware

    app.add_middleware(ApiKeyAuthMiddleware)
    logger.info("[AUTH] API key auth middleware registered")
else:
    logger.warning(
        "[AUTH] ENABLE_AUTH is false — all routes are open (no authentication checks)"
    )


app.include_router(static_files.router)
app.include_router(system.router)
app.include_router(search.router)
app.include_router(geoint_terrain.router)
app.include_router(geoint_vision.router)
app.include_router(geoint_climate.router)
app.include_router(geoint_analysis.router)
app.include_router(query.router)
app.include_router(dashboard.router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {str(exc)}")
    logger.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "message": str(exc),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


# Note: Container startup uses Dockerfile CMD: uvicorn fastapi_app:app --host 0.0.0.0 --port 8000
# The if __name__ == "__main__" block has been removed to prevent port conflicts
