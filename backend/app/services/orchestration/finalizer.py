"""
The final report: the draft rewritten after verification, plus the evidence audit.

    draft + verdicts ──LLM──▶ findings body (supported & qualified content only)
                     ──code──▶ ## Conflicting evidence      (stored descriptions)
                     ──code──▶ ## Unverified and contested statements (every one, with why)
                     ──code──▶ ## References                 (sources with ≥1 valid citation)
                     ──code──▶ EVIDENCE AUDIT                (counted from database rows)

The division of labour is the guarantee. The model writes prose for the content that
survived verification; everything the spec requires to be *reliable* — that no
unsupported claim is silently deleted, that conflicts are mentioned, that references
are real, that the audit's numbers are true — is appended or computed by code from
the stored rows, so it holds however the model behaves. A model body that cites a
marker with no valid citation behind it is caught and warned about, not trusted.

The draft is never modified: claim offsets point into it, and the final report is a
second text on the same report row.
"""

import logging
import re
import time
import uuid
from collections import Counter

from app.core.config import Settings, get_settings
from app.core.exceptions import ConflictError
from app.schemas.evidence import DowngradedClaimRead, EvidenceAudit, FinalReportResponse
from app.services.llm import LLMService
from app.services.orchestration.prompts import (
    FINAL_REPORT_PROMPT_VERSION,
    FINAL_REPORT_SYSTEM,
    build_final_report_prompt,
)
from app.services.research_service import ResearchService

logger = logging.getLogger(__name__)

CITATION_PATTERN = re.compile(r"\[(S\d+)\]")

#: Verdicts whose claims may appear in the final report's body.
PRESENTABLE = {"supported", "partially_supported"}

VERDICT_LABELS = {
    "unsupported": "Unsupported",
    "contradicted": "Contradicted by evidence",
    "not_enough_evidence": "Insufficient evidence",
}


class ReportFinalizer:
    """Produces and stores one run's verification-aware final report and audit."""

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

    async def finalize(self, research_id: uuid.UUID) -> FinalReportResponse:
        started = time.perf_counter()
        summary = self.research.verification_summary(research_id)
        if summary["finalized"]:
            raise ConflictError(
                f"Run {research_id} already has a final report.",
                details={"run_id": str(research_id)},
            )
        claims = summary["claims"]
        if not claims:
            raise ConflictError(
                "No claims have been extracted yet; extract and verify first.",
                details={"run_id": str(research_id)},
            )
        unverified = [claim for claim in claims if claim["verdict"] is None]
        if unverified:
            raise ConflictError(
                f"{len(unverified)} claims are not verified yet; run verification first.",
                details={"run_id": str(research_id), "unverified": len(unverified)},
            )

        warnings: list[str] = []
        valid_markers = {
            marker
            for claim in claims
            for marker, status in claim["citations"].items()
            if status == "valid"
        }

        llm = self._llm or LLMService(settings=self.settings)
        try:
            response = await llm.generate_text(
                build_final_report_prompt(
                    summary["topic"],
                    summary["draft_markdown"],
                    [self._claim_line(claim) for claim in claims],
                    [f"- {conflict['description']}" for conflict in summary["conflicts"]],
                ),
                system=FINAL_REPORT_SYSTEM,
                max_tokens=2500,
            )
        finally:
            if self._llm is None:
                await llm.aclose()

        body = response.text.rstrip()
        rogue = set(CITATION_PATTERN.findall(body)) - valid_markers
        if rogue:
            warnings.append(
                f"The final report's body cites markers with no valid citation behind "
                f"them: {sorted(rogue)}. The references section lists only validated "
                "sources."
            )
        if response.finish_reason == "length":
            warnings.append("The final report was truncated by the token limit.")

        downgraded_claims = [
            claim for claim in claims if claim["verdict"] not in PRESENTABLE
        ]
        final_markdown = (
            body
            + self._conflicts_section(summary["conflicts"])
            + self._unverified_section(downgraded_claims)
            + self._references_section(summary["sources"], valid_markers)
        )

        report_id = self.research.record_final_report(
            research_id, final_markdown=final_markdown, model=response.model
        )
        audit = self._audit(claims, summary["conflicts"])

        logger.info(
            "Finalized run %s: %d claims in, %d presentable, %d contested, %d conflicts",
            research_id,
            len(claims),
            len(claims) - len(downgraded_claims),
            len(downgraded_claims),
            len(summary["conflicts"]),
        )
        return FinalReportResponse(
            research_id=research_id,
            report_id=report_id,
            final_report=final_markdown,
            evidence_audit=audit,
            unverified_claims=[
                DowngradedClaimRead(
                    claim_id=claim["claim_id"],
                    claim_text=claim["text"],
                    verdict=claim["verdict"],
                    downgraded_by_verifier=claim["downgraded"],
                    reason=claim["rationale"] or "No rationale was recorded.",
                    citations=claim["citations"],
                )
                for claim in downgraded_claims
            ],
            model=response.model,
            prompt_version=FINAL_REPORT_PROMPT_VERSION,
            llm_calls=llm.calls_made,
            token_usage=(
                llm.total_usage.model_dump() if llm.total_usage.total_tokens else None
            ),
            duration_seconds=round(time.perf_counter() - started, 3),
            warnings=warnings,
        )

    # --- the code-built sections -------------------------------------------------

    @staticmethod
    def _claim_line(claim: dict) -> str:
        citations = (
            ", ".join(f"{marker}:{status}" for marker, status in claim["citations"].items())
            or "none"
        )
        return (
            f"- verdict={claim['verdict']} | citations: {citations} | "
            f"claim: {claim['text']}"
        )

    @staticmethod
    def _conflicts_section(conflicts: list[dict]) -> str:
        if not conflicts:
            return ""
        lines = ["", "---", "", "## Conflicting evidence", ""]
        lines.append(
            "The retrieved sources disagree on the following points. The statements "
            "record what each passage says; judging between them needs evidence this "
            "run does not have."
        )
        lines.append("")
        for conflict in conflicts:
            kind = conflict["conflict_type"].replace("_", " ")
            lines.append(f"- ({kind}) {conflict['description']}")
        return "\n" + "\n".join(lines)

    @staticmethod
    def _unverified_section(downgraded: list[dict]) -> str:
        """Every claim that did not survive verification, with its reason.

        Built from the stored rows, so nothing can be silently deleted: a claim the
        model dropped from the body still appears here, verdict and rationale
        attached, which is how a reader understands what was downgraded and why.
        """
        if not downgraded:
            return ""
        lines = ["", "---", "", "## Unverified and contested statements", ""]
        lines.append(
            "The draft asserted the following claims, which verification could not "
            "uphold. They are listed rather than silently removed."
        )
        lines.append("")
        for claim in downgraded:
            label = VERDICT_LABELS.get(claim["verdict"], claim["verdict"])
            lines.append(f"- **{label}:** {claim['text']}")
            if claim["rationale"]:
                lines.append(f"  - Why: {claim['rationale']}")
            if claim["downgraded"]:
                lines.append(
                    "  - Note: the verifier asserted support without a verbatim "
                    "quote, so the verdict was downgraded."
                )
        return "\n" + "\n".join(lines)

    @staticmethod
    def _references_section(sources: list[dict], valid_markers: set[str]) -> str:
        """Only sources that back at least one valid citation. A reference list
        padded with sources nothing valid points at would dress the report up."""
        referenced = [source for source in sources if source["label"] in valid_markers]
        if not referenced:
            return ""
        lines = ["", "---", "", "## References", ""]
        for source in sorted(referenced, key=lambda s: int(s["label"][1:])):
            descriptor = source["title"] or source["url"] or source["doi"] or "untitled source"
            provenance = [p for p in (source["venue"], str(source["year"] or "") or None) if p]
            locator = source["url"] or (
                f"https://doi.org/{source['doi']}" if source["doi"] else None
            )
            entry = f"- **[{source['label']}]** {descriptor}"
            if provenance:
                entry += f" ({', '.join(provenance)})"
            if locator:
                entry += f" — {locator}"
            lines.append(entry)
        return "\n" + "\n".join(lines)

    # --- the audit, counted from rows ------------------------------------------------

    @staticmethod
    def _audit(claims: list[dict], conflicts: list[dict]) -> EvidenceAudit:
        verdicts = Counter(claim["verdict"] for claim in claims)
        requiring = [claim for claim in claims if claim["requires_citation"]]
        with_citations = [claim for claim in requiring if claim["citations"]]
        all_citations = Counter(
            status for claim in claims for status in claim["citations"].values()
        )
        total_citations = sum(all_citations.values())
        return EvidenceAudit(
            total_claims=len(claims),
            supported=verdicts.get("supported", 0),
            partially_supported=verdicts.get("partially_supported", 0),
            unsupported=verdicts.get("unsupported", 0),
            contradicted=verdicts.get("contradicted", 0),
            insufficient_evidence=verdicts.get("not_enough_evidence", 0),
            downgraded_verdicts=sum(1 for claim in claims if claim["downgraded"]),
            claims_requiring_citation=len(requiring),
            claims_with_citations=len(with_citations),
            citation_coverage=(
                round(len(with_citations) / len(requiring), 3) if requiring else None
            ),
            citations_total=total_citations,
            citations_by_status=dict(all_citations),
            citation_validity=(
                round(all_citations.get("valid", 0) / total_citations, 3)
                if total_citations
                else None
            ),
            conflicts_detected=len(conflicts),
        )
