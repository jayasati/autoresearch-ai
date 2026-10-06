"""Aggregates all v1 route modules into a single router."""

from fastapi import APIRouter

from app.api.v1.routes import system

api_router = APIRouter()
api_router.include_router(system.router, prefix="/system", tags=["system"])

# Stage 2+: research runs, sources, claims, evidence, metrics, benchmarks.
# api_router.include_router(research.router, prefix="/research", tags=["research"])
# api_router.include_router(claims.router, prefix="/claims", tags=["claims"])
# api_router.include_router(evaluation.router, prefix="/evaluation", tags=["evaluation"])
