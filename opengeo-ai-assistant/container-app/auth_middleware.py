from starlette.middleware.base import BaseHTTPMiddleware

# Cloud-agnostic middleware: all routes are open, no authentication enforced.
class NoAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await call_next(request)
