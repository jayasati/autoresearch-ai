"""System / capability routes. Used by the frontend to show what is wired up."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.constants import ResearchMode

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/ping")
async def ping() -> dict[str, str]:
    return {"ping": "pong"}


@router.get("/capabilities")
async def capabilities(settings: SettingsDep) -> dict:
    """Report which integrations have credentials configured.

    Does not call any external service -- it only reports configuration
    presence, so it is safe to hit before any API keys are set.
    """
    return {
        "modes": [m.value for m in ResearchMode],
        "integrations": settings.integration_status,
        "missing_credentials": settings.missing_credentials,
        "implemented": [],  # research capabilities; filled in as stages land
    }
