"""
Claim extraction: a stored report in, stored and typed claims out.

    report.markdown ──LLM (structured output)──▶ ClaimExtractionModel
                     ──resolve [S#] markers──▶ Claim + Citation rows (one transaction)

The LLM's output is validated by Pydantic twice over: once inside
`generate_structured_output` (strict schema + model validation) and once more by
this package's own `ExtractedClaimModel` field rules. **If parsing fails, the call
is retried once** with a schema reminder; a second failure raises
`ClaimExtractionFailed` and stores nothing — failing safely means the database
never holds half an extraction.

Markers are resolved against the run's own labelled sources, persisted by the
orchestrator at evidence selection. A marker that resolves becomes an `unvalidated`
citation (validation is a later stage); one that does not becomes `fabricated`,
structurally — the report cited a source the run never had. The extractor judges
nothing; it records what the report did.
"""

import logging
import time
import uuid

from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError
from app.schemas.evidence import ClaimExtractionResponse, ExtractedClaimRead
from app.services.claims.exceptions import ClaimExtractionFailed
from app.services.claims.models import (
    MAX_CLAIMS_PER_REPORT,
    ClaimExtractionModel,
)
from app.services.claims.prompts import (
    CLAIM_EXTRACTION_PROMPT_VERSION,
    CLAIM_EXTRACTION_SYSTEM,
    EXTRACTION_RETRY_REMINDER,
    build_claim_extraction_prompt,
)
from app.services.llm import LLMResponseInvalid, LLMService
from app.services.research_service import ResearchService

logger = logging.getLogger(__name__)

#: Generous bound for the structured output; ~50 claims with citations fit well
#: inside it, and an extraction larger than that is a prompt problem, not a budget one.
MAX_OUTPUT_TOKENS = 4000


class ClaimExtractor:
    """Extracts, resolves and stores the claims of one run's report."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        research_service: ResearchService | None = None,
        llm: LLMService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.research = research_service or ResearchService(settings=self.settings)
        self._llm = llm

    async def extract(self, research_id: uuid.UUID) -> ClaimExtractionResponse:
        """Run extraction for one research run and store the result.

        Idempotence is refusal, not overwrite: a report that already has claims
        raises a conflict, because re-extracting would silently re-roll the claim
        set a benchmark may already have measured.
        """
        started = time.perf_counter()
        run = self.research.get_run(research_id)
        if run.report is None:
            raise NotFoundError(
                f"Run {research_id} has no report to extract claims from.",
                details={"run_id": str(research_id)},
            )
        labels = sorted(
            {link.citation_label for link in run.research_sources if link.citation_label},
            key=lambda label: int(label[1:]) if label[1:].isdigit() else 0,
        )
        prompt = build_claim_extraction_prompt(run.report.markdown, labels)

        llm = self._llm or LLMService(settings=self.settings)
        retried = False
        try:
            try:
                result = await llm.generate_structured_output(
                    prompt,
                    ClaimExtractionModel,
                    system=CLAIM_EXTRACTION_SYSTEM,
                    max_tokens=MAX_OUTPUT_TOKENS,
                )
            except LLMResponseInvalid as first_failure:
                # The retry half of the policy: once, with a schema reminder.
                retried = True
                logger.warning(
                    "Extraction for run %s failed validation (%s); retrying once",
                    research_id,
                    first_failure.message,
                )
                try:
                    result = await llm.generate_structured_output(
                        prompt + EXTRACTION_RETRY_REMINDER,
                        ClaimExtractionModel,
                        system=CLAIM_EXTRACTION_SYSTEM,
                        max_tokens=MAX_OUTPUT_TOKENS,
                    )
                except LLMResponseInvalid as second_failure:
                    raise ClaimExtractionFailed(
                        details={"run_id": str(research_id), "attempts": 2}
                    ) from second_failure
        finally:
            if self._llm is None:
                await llm.aclose()

        extracted = result.data.claims[:MAX_CLAIMS_PER_REPORT]
        if len(result.data.claims) > MAX_CLAIMS_PER_REPORT:
            logger.warning(
                "Extraction for run %s produced %d claims; keeping the first %d",
                research_id,
                len(result.data.claims),
                MAX_CLAIMS_PER_REPORT,
            )

        report_id, rows = self.research.record_claims(
            research_id,
            claims=[claim.as_record() for claim in extracted],
            extractor_model=result.model,
        )

        logger.info(
            "Extracted %d claims for run %s (%d LLM calls%s)",
            len(rows),
            research_id,
            llm.calls_made,
            ", retried" if retried else "",
        )
        return ClaimExtractionResponse(
            id=report_id,
            research_id=research_id,
            claim_count=len(rows),
            claims=[ExtractedClaimRead(**row) for row in rows],
            extractor_model=result.model,
            prompt_version=CLAIM_EXTRACTION_PROMPT_VERSION,
            llm_calls=llm.calls_made,
            retried=retried,
            token_usage=(
                llm.total_usage.model_dump() if llm.total_usage.total_tokens else None
            ),
            duration_seconds=round(time.perf_counter() - started, 3),
        )
