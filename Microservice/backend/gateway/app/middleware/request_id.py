"""
Nerve IDP — Request ID Middleware

Tags every request with a UUID. The ID is:
  1. Added to request.state.request_id
  2. Returned in the X-Request-ID response header
  3. Used in the global exception handler for correlation
  4. Picked up by the OTel auto-instrumentation as a span attribute
"""

import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp


class RequestIdMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
