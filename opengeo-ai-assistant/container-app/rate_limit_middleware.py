"""Per-IP rate limiting middleware using the `limits` library."""

import os

from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_RATE_LIMIT_LLM = os.environ.get("RATE_LIMIT_LLM", "10/minute")
_RATE_LIMIT_SEARCH = os.environ.get("RATE_LIMIT_SEARCH", "30/minute")

# LLM-heavy paths (expensive: trigger one or more LLM API calls)
_LLM_PREFIXES = ("/api/geoint/",)
_LLM_EXACT = {"/api/query", "/api/process-comparison-query"}

# Search/utility paths (cheaper but still external calls)
_SEARCH_EXACT = {
    "/api/stac-search",
    "/api/veda-search",
    "/api/structured-search",
    "/api/sign-mosaic-url",
    "/api/session-reset",
}

_storage = MemoryStorage()
_strategy = FixedWindowRateLimiter(_storage)
_llm_limit = parse(_RATE_LIMIT_LLM)
_search_limit = parse(_RATE_LIMIT_SEARCH)


def _get_limit(path: str):
    """Return the RateLimitItem for this path, or None if the path is exempt."""
    if path in _LLM_EXACT or any(path.startswith(p) for p in _LLM_PREFIXES):
        return _llm_limit
    if path in _SEARCH_EXACT:
        return _search_limit
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-IP rate limiting for LLM and search endpoints.

    Exempt paths (health, config, static files, OPTIONS preflight) pass through
    unconditionally. Blocked requests receive a 429 with a JSON body and a
    Retry-After header.
    """

    async def dispatch(self, request: Request, call_next):
        # OPTIONS preflight and exempt paths bypass rate limiting entirely
        if request.method == "OPTIONS":
            return await call_next(request)

        limit_item = _get_limit(request.url.path)
        if limit_item is None:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        # Bucket key: per-IP, per-path so LLM and search counters are independent
        key = f"{client_ip}:{request.url.path}"

        if not _strategy.hit(limit_item, key):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "detail": f"Rate limit exceeded ({limit_item}). Try again later.",
                },
                headers={"Retry-After": "60"},
            )

        return await call_next(request)
