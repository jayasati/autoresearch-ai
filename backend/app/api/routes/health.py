"""
Liveness endpoint.

Deliberately **unversioned** and mounted at ``/api/health``. Health is a property
of the process, not of the API contract, so it must not move when the API goes
from v1 to v2 -- monitoring and container probes should never need updating
because of an API version bump.

It does no I/O: no database, no network. A liveness check that can fail because
an upstream is slow is worse than no check at all, since it causes restarts that
fix nothing. A *readiness* check that does verify dependencies will be added in
stage 2, when there is a database to verify.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app import __version__
from app.core.config import Settings, get_settings
from app.schemas.common import HealthResponse

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check",
    tags=["system"],
)
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.APP_ENV,
    )
