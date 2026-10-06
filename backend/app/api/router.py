"""
The ``/api`` router.

This is where the versioning structure is expressed, in one readable place:

    /api/health          unversioned -- process liveness, must never move
    /api/v1/...          version 1 of the research API
    /api/v2/...          a future breaking revision mounts here alongside v1

Mounting versions as sibling routers (rather than branching inside handlers)
means v1 can keep working untouched while v2 is developed, which is what makes a
version bump a non-event for existing clients.
"""

from fastapi import APIRouter

from app.api.routes import health
from app.api.v1.router import router as v1_router

api_router = APIRouter()

# Unversioned, infrastructure-level.
api_router.include_router(health.router)

# Versioned application API.
api_router.include_router(v1_router, prefix="/v1")
