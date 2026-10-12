"""
Claim extraction: a stored report in, typed and stored claims out.

| Module | Contents |
|---|---|
| `extractor.py` | `ClaimExtractor` — extract, resolve markers, store, one transaction |
| `models.py` | the Pydantic schemas the LLM's structured output must satisfy |
| `prompts.py` | the versioned extraction prompt |
| `exceptions.py` | `ClaimExtractionFailed` — the fail-safe stop after one retry |

Extraction records what the report asserted and what it cited; it judges nothing.
Verification — deciding whether the evidence actually supports each claim — is the
next stage, which is why every resolvable citation is stored `unvalidated` here.
"""

from app.services.claims.exceptions import ClaimExtractionFailed
from app.services.claims.extractor import ClaimExtractor
from app.services.claims.models import (
    MAX_CLAIMS_PER_REPORT,
    ClaimExtractionModel,
    ExtractedClaimModel,
)
from app.services.claims.prompts import CLAIM_EXTRACTION_PROMPT_VERSION

__all__ = [
    "CLAIM_EXTRACTION_PROMPT_VERSION",
    "ClaimExtractionFailed",
    "ClaimExtractionModel",
    "ClaimExtractor",
    "ExtractedClaimModel",
    "MAX_CLAIMS_PER_REPORT",
]
