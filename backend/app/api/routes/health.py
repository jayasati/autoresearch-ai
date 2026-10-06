"""
Health endpoints: liveness and readiness.

Both unversioned, at `/api/health` and `/api/health/ready`. Health is a property of
the process, not of the API contract, so these must not move when the API goes from
v1 to v2 — monitoring and container probes should never need updating because of an
API version bump.

**The two are deliberately different checks**, and conflating them is a real
operational mistake:

- **Liveness** answers "is this process working?" It does no I/O. A liveness probe
  that fails because the database is slow causes the orchestrator to restart a
  perfectly healthy process, which fixes nothing and removes capacity exactly when
  the system is already struggling.
- **Readiness** answers "can this process serve traffic?" It checks the database,
  because a request that needs one cannot be served without it. A failing readiness
  probe takes the instance out of the load balancer without killing it, so it
  recovers on its own when the dependency does.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app import __version__
from app.core.config import Settings, get_settings
from app.db.session import check_connection
from app.schemas.common import HealthResponse, ReadinessResponse, ServiceDependency

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check",
    tags=["system"],
)
async def health(settings: SettingsDep) -> HealthResponse:
    """Is the process alive? No database, no network, no credentials needed."""
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.APP_ENV,
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness check (verifies the database)",
    tags=["system"],
    responses={
        503: {
            "model": ReadinessResponse,
            "description": "A dependency is unavailable; the body says which.",
        }
    },
)
async def readiness(settings: SettingsDep, response: Response) -> ReadinessResponse:
    """Can the process serve traffic? Verifies the database with `SELECT 1`.

    Returns **503** when a required dependency is down, and still returns the full
    body — a probe that says only "not ready" forces whoever is paged to go and find
    out why, so the reason is in the response.

    The connection string appears here with its password removed, because knowing
    *which* database an instance is pointed at is most of the diagnosis when a
    deployment is misconfigured.
    """
    reachable, latency_ms, error = check_connection()

    database = ServiceDependency(
        name=settings.database_backend,
        healthy=reachable,
        latency_ms=round(latency_ms, 2),
        detail=error,
        target=settings.database_url_safe,
    )

    ready = database.healthy
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        version=__version__,
        environment=settings.APP_ENV,
        dependencies=[database],
    )


@router.get(
    "/health/database",
    response_model=ServiceDependency,
    summary="Database connectivity only",
    tags=["system"],
    responses={503: {"model": ServiceDependency, "description": "Database unreachable."}},
)
async def database_health(settings: SettingsDep, response: Response) -> ServiceDependency:
    """Just the database, for a dashboard panel or a targeted alert.

    Separate from readiness so a monitor can distinguish "this instance is not taking
    traffic" from "the database is down" — which will not be the same thing once
    there are other dependencies to be unready for.
    """
    reachable, latency_ms, error = check_connection()
    if not reachable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ServiceDependency(
        name=settings.database_backend,
        healthy=reachable,
        latency_ms=round(latency_ms, 2),
        detail=error,
        target=settings.database_url_safe,
    )
