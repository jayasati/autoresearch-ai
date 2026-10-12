"""
The research endpoint: run the whole pipeline for one topic.

Synchronous on purpose, for now: the caller waits (roughly 15-90 seconds depending
on depth) and receives the finished report, its sources and the receipts in one
response. A job queue with polling is the obvious next shape once runs get longer
than a request should be — the response already carries `research_id`, so moving to
"202 + poll" later will not change what callers key on.

Errors arrive as the API's standard error envelope via the handlers in
`core/errors.py`: a provider outage is a 502 `external_service_error`, a topic with
no usable evidence is a 422 `no_evidence`, a missing key is a 500
`configuration_error`. Every failure also marks the run `failed` in the database
with the same code, so the API answer and the stored run never tell two stories.
"""

import uuid

from fastapi import APIRouter

from app.schemas.evidence import (
    ClaimExtractionResponse,
    FinalReportResponse,
    VerificationRunResponse,
)
from app.schemas.research import ResearchStartRequest, ResearchStartResponse
from app.services.claims import ClaimExtractor
from app.services.orchestration import ReportFinalizer, ResearchOrchestrator
from app.services.verification import ClaimVerifier

router = APIRouter()


@router.post("/start", response_model=ResearchStartResponse)
async def start_research(request: ResearchStartRequest) -> ResearchStartResponse:
    """Run research on a topic under the requested mode and return the report."""
    return await ResearchOrchestrator().run(request)


@router.post("/{research_id}/claims", response_model=ClaimExtractionResponse)
async def extract_claims(research_id: uuid.UUID) -> ClaimExtractionResponse:
    """Extract, type and store the factual claims of a completed run's report.

    One extraction per report: re-posting returns a 409 rather than re-rolling a
    claim set a benchmark may already have measured. Parsing failures are retried
    once, then fail with `claim_extraction_failed` and store nothing.
    """
    return await ClaimExtractor().extract(research_id)


@router.post("/{research_id}/verify", response_model=VerificationRunResponse)
async def verify_claims(research_id: uuid.UUID) -> VerificationRunResponse:
    """Verify the run's extracted claims against its own retrieved evidence.

    Per claim: evidence is retrieved from the run's vector collection, judged by the
    verifier (quotes enforced verbatim), citations validated structurally (valid
    only when a supporting passage belongs to the cited source), and conflicts
    stored between passages that actually disagree. Claims whose verification fails
    twice are skipped; re-POSTing verifies exactly the stragglers.
    """
    return await ClaimVerifier().verify(research_id)


@router.post("/{research_id}/finalize", response_model=FinalReportResponse)
async def finalize_report(research_id: uuid.UUID) -> FinalReportResponse:
    """Produce the verification-aware final report and its evidence audit.

    Requires every claim to be verified. The body presents supported findings and
    qualifies partial ones; the conflicting-evidence, unverified-claims and
    references sections are built by code from the stored rows, so no unsupported
    claim is silently deleted and no reference can be invented. The audit's numbers
    are counted from the database.
    """
    return await ReportFinalizer().finalize(research_id)
