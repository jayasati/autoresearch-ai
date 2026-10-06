"""
FastAPI application entrypoint.

Stage 2 scope: a complete, correct application *foundation* -- configuration,
CORS, logging, correlation ids, uniform error handling, versioned routing and a
health endpoint. No research functionality.

The app is built by a factory rather than at import time so tests can construct
an isolated instance with their own settings.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.schemas.common import ErrorResponse, ServiceInfoResponse

logger = logging.getLogger(__name__)

STAGE = "stage-2: backend foundation (no research functionality)"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    Everything expensive and shared is acquired here, once, rather than per
    request: later stages open the database pool, load the embedding model and
    open the Chroma client in this block.
    """
    settings: Settings = app.state.settings
    configure_logging(settings.LOG_LEVEL)

    logger.info("Starting %s v%s (env=%s)", settings.APP_NAME, __version__, settings.APP_ENV)
    logger.info("CORS origins: %s", ", ".join(settings.cors_origin_list) or "(none)")

    # Report missing credentials at boot as a warning rather than an error: the
    # service is still useful without them at this stage, and failing to start
    # would hide the much more informative /api/v1/system/capabilities response.
    missing = settings.missing_credentials
    if missing:
        logger.warning(
            "Not configured (expected until stage 4): %s -- see /api/v1/system/capabilities",
            ", ".join(missing),
        )

    yield

    logger.info("Shutting down %s", settings.APP_NAME)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Pass `settings` to override configuration in tests."""
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        description=(
            "Evidence-grounded agentic research system.\n\n"
            "**Research functionality is not implemented yet.** This build serves "
            "the application foundation only."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        # Document the shared error envelope once, on every route, instead of
        # repeating it per endpoint.
        responses={
            422: {"model": ErrorResponse, "description": "Validation failed"},
            500: {"model": ErrorResponse, "description": "Unexpected server error"},
        },
    )

    app.state.settings = settings

    # Order matters: middleware is applied outermost-last, so the request-context
    # middleware is added after CORS to ensure it wraps the handler and a request
    # id exists before any application code runs.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-ms"],
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.API_PREFIX)

    SettingsDep = Annotated[Settings, Depends(get_settings)]

    @app.get(
        "/",
        response_model=ServiceInfoResponse,
        summary="Service information",
        tags=["system"],
    )
    async def root(settings: SettingsDep) -> ServiceInfoResponse:
        """What this service is, and where to find the things that matter."""
        return ServiceInfoResponse(
            name=settings.APP_NAME,
            version=__version__,
            environment=settings.APP_ENV,
            description="Evidence-grounded agentic research system.",
            stage=STAGE,
            docs_url="/docs",
            health_url=settings.HEALTH_URL,
            api_version_prefix=settings.API_V1_PREFIX,
        )

    return app


app = create_app()
