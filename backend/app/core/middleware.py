"""
HTTP middleware.

Correlation and access logging. Deliberately thin: middleware runs on every
single request, so anything expensive belongs in a dependency instead.
"""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.utils.request_context import reset_request_id, set_request_id

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns each request an id, logs it, and reports how long it took.

    An inbound ``X-Request-ID`` is honoured rather than replaced, so a trace can
    be followed across the frontend and the backend. The id is always echoed in
    the response, which is what lets a user paste it into a bug report.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming or uuid.uuid4().hex[:12]
        token = set_request_id(request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers produce the response; this only records the
            # timing of a request that failed, then re-raises untouched.
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "%s %s -> raised after %.1fms",
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise
        else:
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
            logger.info(
                "%s %s -> %d in %.1fms",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
            return response
        finally:
            reset_request_id(token)
