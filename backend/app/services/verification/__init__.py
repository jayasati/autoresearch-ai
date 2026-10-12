"""
Verification: claims judged against the run's own evidence, citations validated
structurally, conflicts recorded between the passages that actually disagree.

| Module | Contents |
|---|---|
| `verifier.py` | `ClaimVerifier` — retrieve per claim, judge, enforce, persist |
| `models.py` | the Pydantic schemas the verifier's structured output must satisfy |
| `prompts.py` | the versioned verification prompt |
| `exceptions.py` | `VerificationUnavailable`, `NothingToVerify` |

The three enforcement rules (quotes verbatim or neutral; citations valid only via a
supporting passage from the cited source; conflicts only between passages whose
enforced relations disagree) live in code, not in the prompt alone — the verifier
hallucinating is a documented project risk.
"""

from app.services.verification.exceptions import NothingToVerify, VerificationUnavailable
from app.services.verification.models import (
    ClaimVerificationModel,
    ConflictReport,
    PassageAssessment,
)
from app.services.verification.prompts import VERIFICATION_PROMPT_VERSION
from app.services.verification.verifier import (
    MAX_CONCURRENCY,
    TOP_K_PER_CLAIM,
    ClaimVerifier,
)

__all__ = [
    "ClaimVerificationModel",
    "ClaimVerifier",
    "ConflictReport",
    "MAX_CONCURRENCY",
    "NothingToVerify",
    "PassageAssessment",
    "TOP_K_PER_CLAIM",
    "VERIFICATION_PROMPT_VERSION",
    "VerificationUnavailable",
]
