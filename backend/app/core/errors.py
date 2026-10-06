"""
Exception handlers.

One place decides how a failure becomes an HTTP response, so every error the API
emits has the same shape:

    {"error": {"code": ..., "message": ..., "details": {...}, "request_id": ...}}

Uniformity is the point. A frontend that can rely on one error shape needs one
error path, not one per endpoint.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.utils.request_context import get_request_id

logger = logging.getLogger(__name__)

# Starlette's HTTPException carries only a status code, so map the ones we
# actually emit to our stable code vocabulary.
_STATUS_CODE_NAMES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "external_service_error",
    503: "service_unavailable",
}


def _resolve_request_id(request: Request) -> str:
    """Find the correlation id for this request.

    ``request.state`` is checked before the context variable, and that order
    matters. Starlette's ServerErrorMiddleware sits *outside* our own middleware,
    so for an unhandled exception the context variable has already been reset by
    the time the catch-all handler runs -- but ``request.state`` still holds the
    id. Without this fallback, precisely the errors that most need correlating
    would come back with no id.
    """
    from_state = getattr(request.state, "request_id", None)
    if from_state:
        return str(from_state)
    return get_request_id()


def _envelope(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    request_id = _resolve_request_id(request)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": request_id,
            }
        },
        headers={"X-Request-ID": request_id},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler to the application. Called from the app factory."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # Our own failures: 5xx is worth a stack trace, 4xx is just information.
        log = logger.error if exc.status_code >= 500 else logger.info
        log(
            "%s %s -> %s (%s)",
            request.method,
            request.url.path,
            exc.code,
            exc.message,
            exc_info=exc if exc.status_code >= 500 else None,
        )
        return _envelope(request, exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Flatten Pydantic's error list into something a UI can render per field."""
        fields = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body") or "body",
                "problem": err.get("msg", "invalid"),
                "type": err.get("type", "unknown"),
            }
            for err in exc.errors()
        ]
        logger.info(
            "%s %s -> validation_error (%d issue(s))",
            request.method,
            request.url.path,
            len(fields),
        )
        return _envelope(
            request,
            422,
            "validation_error",
            "The request failed validation.",
            {"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Covers 404s on unknown paths, 405s, and any raised HTTPException."""
        code = _STATUS_CODE_NAMES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return _envelope(request, exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Last resort: an exception nobody anticipated.

        The traceback goes to the log, never to the client. In development the
        exception text is echoed back because it saves real debugging time; in
        production it is withheld, since it can disclose internals.
        """
        logger.exception(
            "Unhandled %s on %s %s",
            type(exc).__name__,
            request.method,
            request.url.path,
        )
        details = (
            {"exception": type(exc).__name__, "args": [str(a) for a in exc.args]}
            if get_settings().DEBUG
            else {}
        )
        return _envelope(
            request,
            500,
            "internal_error",
            "An unexpected error occurred. The incident has been logged.",
            details,
        )
