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

from fastapi import APIRouter, Query

from app.schemas.evidence import (
    ClaimExtractionResponse,
    EvidenceAuditResponse,
    FinalReportResponse,
    VerificationRunResponse,
)
from app.schemas.research import (
    ReportViewResponse,
    ResearchResponse,
    ResearchRunListItem,
    ResearchRunRead,
    ResearchStartRequest,
    ResearchStartResponse,
    RunSourceRead,
)
from app.services.claims import ClaimExtractor
from app.services.orchestration import ReportFinalizer, ResearchOrchestrator
from app.services.research_service import ResearchService
from app.services.verification import ClaimVerifier

router = APIRouter()


@router.post("/start", response_model=ResearchStartResponse)
async def start_research(request: ResearchStartRequest) -> ResearchStartResponse:
    """Run research on a topic under the requested mode and return the report."""
    return await ResearchOrchestrator().run(request)


# --- reading stored runs (stage 23) -------------------------------------------------
#
# Read-only views over what the pipeline persisted. Nothing here computes, judges
# or re-rolls anything: every value is a stored row read back, so these endpoints
# can be hit freely without spending a model call or changing a result.


@router.get("", response_model=list[ResearchRunListItem])
async def list_research(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[ResearchRunListItem]:
    """Recent research runs, newest first."""
    runs = ResearchService().list_runs(limit=limit, offset=offset)
    return [ResearchRunListItem.from_run(run) for run in runs]


@router.get("/{research_id}", response_model=ResearchResponse)
async def get_research(research_id: uuid.UUID) -> ResearchResponse:
    """One run with its configuration and plan, plus counts of what it produced."""
    service = ResearchService()
    run = service.get_run(research_id)
    counts = service.run_summary(research_id)
    return ResearchResponse(run=ResearchRunRead.model_validate(run), **counts)


@router.get("/{research_id}/report", response_model=ReportViewResponse)
async def get_report(research_id: uuid.UUID) -> ReportViewResponse:
    """The run's report texts: the immutable draft, and the final report if made."""
    return ReportViewResponse.model_validate(ResearchService().report_view(research_id))


@router.get("/{research_id}/sources", response_model=list[RunSourceRead])
async def get_sources(research_id: uuid.UUID) -> list[RunSourceRead]:
    """Every source the run retrieved, labelled or not, with its evidence depth."""
    return [RunSourceRead.model_validate(row) for row in ResearchService().run_sources(research_id)]


@router.get("/{research_id}/evidence", response_model=EvidenceAuditResponse)
async def get_evidence(research_id: uuid.UUID) -> EvidenceAuditResponse:
    """The run's stored claims, verdicts, citation outcomes, conflicts and sources."""
    summary = ResearchService().verification_summary(research_id)
    return EvidenceAuditResponse(
        research_id=research_id,
        report_id=summary["report_id"],
        topic=summary["topic"],
        finalized=summary["finalized"],
        claims=summary["claims"],
        conflicts=summary["conflicts"],
        sources=summary["sources"],
    )


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
