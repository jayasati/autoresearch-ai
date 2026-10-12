"""
Orchestration: the pipeline that turns a topic into a grounded, cited report.

| Module | Contents |
|---|---|
| `orchestrator.py` | `ResearchOrchestrator` — retrieval → processing → vectorstore → LLM |
| `prompts.py` | the versioned synthesis prompt and evidence formatting |
| `exceptions.py` | `NoEvidenceError`, `UncitedReportError` — the honest stops |

SEARCH_GROUNDED only. No sub-question planning, no full-text fetching, no claim
verification yet — each is a later stage, and the orchestrator's docstring says
exactly what today's citations do and do not guarantee.
"""

from app.services.orchestration.exceptions import NoEvidenceError, UncitedReportError
from app.services.orchestration.orchestrator import (
    DEPTH_PROFILES,
    DepthProfile,
    ResearchOrchestrator,
)
from app.services.orchestration.prompts import SYNTHESIS_PROMPT_VERSION

__all__ = [
    "DEPTH_PROFILES",
    "DepthProfile",
    "NoEvidenceError",
    "ResearchOrchestrator",
    "SYNTHESIS_PROMPT_VERSION",
    "UncitedReportError",
]
