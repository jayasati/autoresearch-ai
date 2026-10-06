"""Aggregates all v1 route modules into a single router, mounted at /api/v1."""

from fastapi import APIRouter

from app.api.v1.routes import system

router = APIRouter()
router.include_router(system.router, prefix="/system", tags=["system"])

# Stage 2+: research runs, sources, claims, evidence, metrics, benchmarks.
# router.include_router(research.router, prefix="/research", tags=["research"])
# router.include_router(claims.router, prefix="/claims", tags=["claims"])
# router.include_router(evaluation.router, prefix="/evaluation", tags=["evaluation"])
