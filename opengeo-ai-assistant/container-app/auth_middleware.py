import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_OPEN_PATHS = {"/api/health", "/api/config", "/"}


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Validates X-Api-Key header against API_KEY env var on all protected routes."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._api_key = os.environ.get("API_KEY", "").strip()
        if not self._api_key:
            logger.warning(
                "[AUTH] API_KEY is not set — auth middleware will reject all requests"
            )

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in _OPEN_PATHS:
            return await call_next(request)

        provided = request.headers.get("X-Api-Key", "")
        if not self._api_key or provided != self._api_key:
            return JSONResponse(
                {
                    "error": "unauthorized",
                    "detail": "Missing or invalid X-Api-Key header",
                },
                status_code=401,
            )
        return await call_next(request)


class NoAuthMiddleware(BaseHTTPMiddleware):
    """Pass-through middleware used when ENABLE_AUTH=false."""

    async def dispatch(self, request: Request, call_next):
        return await call_next(request)
