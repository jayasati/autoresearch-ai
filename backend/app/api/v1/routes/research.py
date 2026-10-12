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

from fastapi import APIRouter

from app.schemas.research import ResearchStartRequest, ResearchStartResponse
from app.services.orchestration import ResearchOrchestrator

router = APIRouter()


@router.post("/start", response_model=ResearchStartResponse)
async def start_research(request: ResearchStartRequest) -> ResearchStartResponse:
    """Run SEARCH_GROUNDED research on a topic and return the cited report."""
    return await ResearchOrchestrator().run(request)
