"""
Persistence for report-quality-v1 (stage 29, S2; design §8; run, plan and source
links added in stage 30, S3).

The one transaction boundary for the four new tables and the configuration's
pipeline fields. Every write checks, before anything is stored, the invariants a
CHECK constraint cannot express (each also has a database CHECK where one can):

- **A claim audit agrees with the stored verdict.** A claim with a
  `verification_result` is audited with exactly that verdict; a claim without one
  can only be `not_evaluated`, with a reason. Nothing is marked evaluated that was
  not.
- **A citation relation is quoted verbatim** from the cited chunk, as the verifier
  itself requires. A relation established by *pool verification* must be backed by
  an `evidence` row of verification-v2 on that exact chunk for that claim — the
  shortcut cannot be asserted, only reused.
- **A rewritten sentence is supported only through what it cites.** A
  `final_claim_check` may say `supported_by_cited_source` only when a verification
  actually ran (CHECK) and at least one supporting chunk is among the sentence's
  cited chunks (here). A sentence awaiting re-verification is stored as
  `not_evaluated / awaiting_reverification`, and a decided verdict is never
  overwritten.

No model, provider or retrieval call happens here. Nothing in the legacy pipeline
imports this module.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.constants import (
    CitationOutcome,
    ClaimStatus,
    EstablishedBy,
    EvidenceRelation,
    NotEvaluatedReason,
    PipelineVersion,
    Placement,
    PoolStatus,
    ReportQualityStatus,
)
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.base import utcnow
from app.db.session import unit_of_work
from app.models import (
    BenchmarkRun,
    Citation,
    CitationCheck,
    Claim,
    ClaimAudit,
    DocumentChunk,
    Evidence,
    FinalClaimCheck,
    Report,
    ReportQualityAudit,
    ResearchRun,
    SubQuestion,
    VerificationResult,
)
from app.repositories import (
    ResearchRunRepository,
    RunConfigurationRepository,
    SourceRepository,
)
from app.repositories.quality import (
    CitationCheckRepository,
    ClaimAuditRepository,
    FinalClaimCheckRepository,
    ReportQualityAuditRepository,
)

TERMINAL = frozenset(
    {
        ReportQualityStatus.COMPLETE,
        ReportQualityStatus.DEGRADED,
        ReportQualityStatus.FINALIZATION_FAILED,
    }
)
_RELATIONS = {
    CitationOutcome.SUPPORTS: EvidenceRelation.SUPPORTS,
    CitationOutcome.PARTIALLY_SUPPORTS: EvidenceRelation.SUPPORTS,
    CitationOutcome.CONTRADICTS: EvidenceRelation.CONTRADICTS,
}
_SUPPORTED_POOL = {PoolStatus.SUPPORTED, PoolStatus.PARTIALLY_SUPPORTED}


def quality_configuration_values(base: Mapping, settings: Settings) -> dict:
    """A legacy configuration's values plus the opt-in pipeline fields.

    `options` snapshots every knob that changes a report-quality-v1 run, so two runs
    under different budgets or thresholds get different fingerprints. The gate
    (`REPORT_QUALITY_ENABLED`) is not a knob of the run and is left out.
    """
    options = {
        name: getattr(settings, name)
        for name in sorted(type(settings).model_fields)
        if name.startswith("REPORT_QUALITY_") and name != "REPORT_QUALITY_ENABLED"
    }
    options["EVIDENCE_SELECTION"] = settings.EVIDENCE_SELECTION
    return {**base, "pipeline_version": PipelineVersion.REPORT_QUALITY_V1, "options": options}


# --- write records -------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimAuditRecord:
    claim_id: uuid.UUID
    pool_status: PoolStatus
    not_evaluated_reason: NotEvaluatedReason | None = None
    claim_status: ClaimStatus | None = None
    alignment_status: str | None = None  # "aligned" | "unaligned"
    aligned_sentence_index: int | None = None
    alignment_score: float | None = None
    priority_rank: int | None = None
    final_placement: Placement | None = None


@dataclass(frozen=True)
class CitationCheckRecord:
    claim_id: uuid.UUID
    passage_alias: str
    cited_chunk_id: uuid.UUID | None
    outcome: CitationOutcome
    procedure_version: str
    citation_id: uuid.UUID | None = None
    not_evaluated_reason: NotEvaluatedReason | None = None
    established_by: EstablishedBy | None = None
    quoted_text: str | None = None
    rationale: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class FinalClaimCheckRecord:
    section_index: int
    sentence_index: int
    text: str
    change_kinds: Sequence[str]
    cited_chunk_ids: Sequence[uuid.UUID]
    pool_status: PoolStatus
    claim_status: ClaimStatus
    derived_from_claim_id: uuid.UUID | None = None
    not_evaluated_reason: NotEvaluatedReason | None = None
    supported_chunk_ids: Sequence[uuid.UUID] = field(default_factory=tuple)
    evidence: dict | None = None
    model: str | None = None
    prompt_version: str | None = None


def _invalid(message: str, **details) -> ValidationError:
    return ValidationError(message, details={k: str(v) for k, v in details.items()})


class QualityStore:
    """Writes and reads report-quality-v1 records, one transaction per call."""

    def __init__(self, session: Session | None = None) -> None:
        self._session = session

    # --- the run and its plan (stage 30, S3) -----------------------------------

    def create_run(
        self,
        topic: str,
        configuration_values: Mapping,
        *,
        benchmark_run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """A pending run under a report-quality-v1 configuration (never a legacy one)."""
        if configuration_values.get("pipeline_version") is not PipelineVersion.REPORT_QUALITY_V1:
            raise _invalid("A report-quality run needs a report-quality configuration.")
        with unit_of_work(self._session) as session:
            configuration, _ = RunConfigurationRepository(session).get_or_create(
                **configuration_values
            )
            benchmark = None
            if benchmark_run_id is not None:
                benchmark = session.get(BenchmarkRun, benchmark_run_id)
                if benchmark is None:
                    raise NotFoundError(f"No benchmark_run with id {benchmark_run_id}.")
            run = ResearchRunRepository(session).create(
                topic=topic, configuration=configuration, benchmark_run=benchmark
            )
            session.flush()
            return run.id

    def record_plan(
        self, run_id: uuid.UUID, subquestions: Sequence[tuple[str, str]]
    ) -> list[uuid.UUID]:
        """Persist the plan, all of it or none: `(text, search query)` per subquestion,
        in order. Returns the sub_question ids in the same order."""
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_or_raise(run_id)
            if run.subquestions:
                raise ConflictError(f"Run {run_id} already has a plan.")
            rows = [
                SubQuestion(
                    research_run=run,
                    position=index,
                    text=text,
                    search_query=query,
                    created_at=utcnow(),
                )
                for index, (text, query) in enumerate(subquestions)
            ]
            session.add_all(rows)
            session.flush()
            return [row.id for row in rows]

    def attach_sources(
        self, run_id: uuid.UUID, sources: Sequence[tuple[dict, uuid.UUID | None]]
    ) -> dict[str, uuid.UUID]:
        """Deduplicate and link each source to the run, recording the subquestion that
        first found it. `sources` holds `(attach values, subquestion id)`, where the
        values are what `SourceMetadata.to_source_create()` plus the link facts give.
        Returns fingerprint → source id. One transaction for all of them."""
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_or_raise(run_id)
            repository = SourceRepository(session)
            ids: dict[str, uuid.UUID] = {}
            for values, subquestion_id in sources:
                values = dict(values)
                link = {
                    key: values.pop(key)
                    for key in ("evidence_depth", "retriever", "retrieval_query", "rank")
                }
                fingerprint = values.pop("fingerprint")
                source, _ = repository.get_or_create(fingerprint, **values)
                repository.link_to_run(run, source, subquestion_id=subquestion_id, **link)
                ids[fingerprint] = source.id
            session.flush()
            return ids

    def mark_gaps(self, run_id: uuid.UUID, subquestion_ids: Sequence[uuid.UUID]) -> None:
        """Subquestions with no selected evidence are not addressed — recorded now, so
        coverage can never count them as answered."""
        with unit_of_work(self._session) as session:
            for subquestion_id in subquestion_ids:
                row = session.get(SubQuestion, subquestion_id)
                if row is None or row.research_run_id != run_id:
                    raise _invalid(
                        "The subquestion does not belong to this run.",
                        subquestion_id=subquestion_id,
                    )
                row.addressed = False

    def link_sections(
        self,
        report_id: uuid.UUID,
        subquestion_ids: Sequence[uuid.UUID | None],
        expected_headings: Sequence[str],
    ) -> None:
        """Attach the report's sections, by position, to the subquestions they answer
        (None for an overview). Sections beyond the list (the code-built Sources list)
        stay unlinked. Each subquestion must belong to the report's run (stage 31, S4).

        Linking is by position, so it is refused unless the stored sections' headings
        are exactly `expected_headings` in order: a stray markdown heading in the text
        would otherwise shift every link after it, silently."""
        if len(expected_headings) != len(subquestion_ids):
            raise _invalid("One expected heading per section link is required.")
        with unit_of_work(self._session) as session:
            report = session.get(Report, report_id)
            if report is None:
                raise NotFoundError(f"No report with id {report_id}.")
            sections = sorted(report.sections, key=lambda s: s.position)
            if len(subquestion_ids) > len(sections):
                raise _invalid("More section links than report sections.", report_id=report_id)
            stored = [section.heading for section in sections[: len(expected_headings)]]
            if stored != list(expected_headings):
                raise _invalid(
                    "The stored report sections do not match the draft's sections; "
                    "refusing to link by position.",
                    report_id=report_id,
                    stored=stored,
                    expected=list(expected_headings),
                )
            for section, subquestion_id in zip(sections, subquestion_ids, strict=False):
                if subquestion_id is not None:
                    row = session.get(SubQuestion, subquestion_id)
                    if row is None or row.research_run_id != report.research_run_id:
                        raise _invalid(
                            "The subquestion does not belong to this run.",
                            subquestion_id=subquestion_id,
                        )
                section.subquestion_id = subquestion_id

    # --- the audit row ---------------------------------------------------------

    def start_audit(self, report_id: uuid.UUID) -> uuid.UUID:
        with unit_of_work(self._session) as session:
            if session.get(Report, report_id) is None:
                raise NotFoundError(f"No report with id {report_id}.")
            audits = ReportQualityAuditRepository(session)
            if audits.for_report(report_id) is not None:
                raise ConflictError(f"Report {report_id} already has a quality audit.")
            audit = ReportQualityAudit(
                report_id=report_id,
                pipeline_version=PipelineVersion.REPORT_QUALITY_V1,
                quality_status=ReportQualityStatus.RUNNING,
                stage=None,
                payload={},
            )
            audits.add(audit)
            audits.flush()
            return audit.id

    def update_audit(
        self,
        report_id: uuid.UUID,
        *,
        stage: str | None = None,
        payload: Mapping | None = None,
        quality_status: ReportQualityStatus | None = None,
    ) -> None:
        """Merge `payload` keys into the audit, move the stage, set the status.
        A terminal audit is immutable: its outcome has been reported."""
        with unit_of_work(self._session) as session:
            audit = ReportQualityAuditRepository(session).for_report(report_id)
            if audit is None:
                raise NotFoundError(f"Report {report_id} has no quality audit.")
            if audit.quality_status in TERMINAL:
                raise ConflictError(
                    f"The quality audit of report {report_id} is final "
                    f"({audit.quality_status.value}); it cannot change."
                )
            if stage is not None:
                audit.stage = stage
            if payload:
                audit.payload = {**audit.payload, **payload}
            if quality_status is not None:
                audit.quality_status = quality_status

    # --- claims ----------------------------------------------------------------

    def record_claim_audit(self, record: ClaimAuditRecord) -> uuid.UUID:
        """Insert or update a claim's audit; refused if it contradicts the stored verdict."""
        with unit_of_work(self._session) as session:
            if session.get(Claim, record.claim_id) is None:
                raise NotFoundError(f"No claim with id {record.claim_id}.")
            verdict = session.execute(
                select(VerificationResult.verdict).where(
                    VerificationResult.claim_id == record.claim_id
                )
            ).scalar_one_or_none()
            if verdict is None and record.pool_status is not PoolStatus.NOT_EVALUATED:
                raise _invalid(
                    "A claim with no stored verification can only be audited as not_evaluated.",
                    claim_id=record.claim_id,
                    pool_status=record.pool_status.value,
                )
            if verdict is not None and record.pool_status.value != verdict.value:
                raise _invalid(
                    "A claim audit must carry the stored verification verdict.",
                    claim_id=record.claim_id,
                    stored=verdict.value,
                    given=record.pool_status.value,
                )
            audits = ClaimAuditRepository(session)
            audit = audits.for_claim(record.claim_id)
            values = {
                key: getattr(record, key)
                for key in record.__dataclass_fields__
                if key != "claim_id"
            }
            if audit is None:
                audit = ClaimAudit(claim_id=record.claim_id, **values)
                audits.add(audit)
            else:
                for key, value in values.items():
                    setattr(audit, key, value)
            audits.flush()
            return audit.id

    # --- citations ---------------------------------------------------------------

    def record_citation_check(self, record: CitationCheckRecord) -> uuid.UUID:
        with unit_of_work(self._session) as session:
            if session.get(Claim, record.claim_id) is None:
                raise NotFoundError(f"No claim with id {record.claim_id}.")
            if record.citation_id is not None:
                citation = session.get(Citation, record.citation_id)
                if citation is None or citation.claim_id != record.claim_id:
                    raise _invalid(
                        "The citation does not belong to this claim.",
                        citation_id=record.citation_id,
                    )
            chunk = None
            if record.cited_chunk_id is not None:
                chunk = session.get(DocumentChunk, record.cited_chunk_id)
                if chunk is None:
                    raise NotFoundError(f"No chunk with id {record.cited_chunk_id}.")
            relation = _RELATIONS.get(record.outcome)
            if relation is not None:
                quote = (record.quoted_text or "").strip()
                if chunk is None or not quote or quote not in chunk.text:
                    raise _invalid(
                        "An established relation needs a verbatim quote from the cited chunk.",
                        claim_id=record.claim_id,
                        passage=record.passage_alias,
                    )
                if record.established_by is EstablishedBy.POOL_VERIFICATION:
                    backed = session.execute(
                        select(Evidence.id).where(
                            Evidence.claim_id == record.claim_id,
                            Evidence.document_chunk_id == record.cited_chunk_id,
                            Evidence.relation == relation,
                            Evidence.quoted_text.is_not(None),
                        )
                    ).first()
                    if backed is None:
                        raise _invalid(
                            "A pool-verification relation must reuse a stored evidence row "
                            "on this exact chunk.",
                            claim_id=record.claim_id,
                            chunk_id=record.cited_chunk_id,
                        )
            checks = CitationCheckRepository(session)
            if checks.find(record.claim_id, record.passage_alias) is not None:
                raise ConflictError(
                    f"Claim {record.claim_id} already has a check for {record.passage_alias}."
                )
            check = CitationCheck(
                **{key: getattr(record, key) for key in record.__dataclass_fields__},
                created_at=utcnow(),
            )
            checks.add(check)
            checks.flush()
            return check.id

    # --- re-verification of rewritten sentences ------------------------------------

    def record_final_claim_check(
        self, report_id: uuid.UUID, record: FinalClaimCheckRecord
    ) -> uuid.UUID:
        """Store a flagged sentence (awaiting re-verification) or its re-verified result.

        An awaiting row may be replaced by its result; a decided result never changes.
        """
        with unit_of_work(self._session) as session:
            if session.get(Report, report_id) is None:
                raise NotFoundError(f"No report with id {report_id}.")
            if not record.change_kinds:
                raise _invalid("A final claim check records why the sentence changed.")
            if record.derived_from_claim_id is not None:
                claim = session.get(Claim, record.derived_from_claim_id)
                if claim is None or claim.report_id != report_id:
                    raise _invalid(
                        "The original claim does not belong to this report.",
                        claim_id=record.derived_from_claim_id,
                    )
            mentioned = {*record.cited_chunk_ids, *record.supported_chunk_ids}
            found = (
                set(
                    session.execute(
                        select(DocumentChunk.id).where(DocumentChunk.id.in_(mentioned))
                    ).scalars()
                )
                if mentioned
                else set()
            )
            if missing := mentioned - found:
                raise NotFoundError(f"Unknown chunk ids: {sorted(map(str, missing))}.")
            if record.claim_status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE:
                if record.pool_status not in _SUPPORTED_POOL:
                    raise _invalid("Support needs a verification verdict of support.")
                if not set(record.supported_chunk_ids) & set(record.cited_chunk_ids):
                    raise _invalid(
                        "A rewritten sentence is supported by its cited source only when a "
                        "supporting chunk is one it cites.",
                        section=record.section_index,
                        sentence=record.sentence_index,
                    )

            checks = FinalClaimCheckRepository(session)
            existing = checks.find(report_id, record.section_index, record.sentence_index)
            if existing is not None:
                if existing.pool_status is not PoolStatus.NOT_EVALUATED:
                    raise ConflictError("This sentence already has a re-verified result.")
                session.delete(existing)
                session.flush()
            check = FinalClaimCheck(
                report_id=report_id,
                derived_from_claim_id=record.derived_from_claim_id,
                section_index=record.section_index,
                sentence_index=record.sentence_index,
                text=record.text,
                change_kinds=sorted(record.change_kinds),
                cited_chunk_ids=[str(c) for c in record.cited_chunk_ids],
                pool_status=record.pool_status,
                not_evaluated_reason=record.not_evaluated_reason,
                claim_status=record.claim_status,
                supported_chunk_ids=[str(c) for c in record.supported_chunk_ids],
                evidence=record.evidence,
                model=record.model,
                prompt_version=record.prompt_version,
                created_at=utcnow(),
            )
            checks.add(check)
            checks.flush()
            return check.id

    # --- read ----------------------------------------------------------------------

    def quality_record(self, research_id: uuid.UUID) -> dict:
        """Everything report-quality-v1 stored for one run, as plain data. A legacy
        run returns its pipeline fields as None and empty collections."""
        with unit_of_work(self._session) as session:
            run = session.get(ResearchRun, research_id)
            if run is None:
                raise NotFoundError(f"No research run with id {research_id}.")
            report = run.report
            audit = ReportQualityAuditRepository(session).for_report(report.id) if report else None
            claim_ids = [claim.id for claim in report.claims] if report else []
            return {
                "research_id": research_id,
                "pipeline_version": (
                    run.configuration.pipeline_version.value
                    if run.configuration.pipeline_version
                    else None
                ),
                "options": run.configuration.options,
                "audit": None
                if audit is None
                else {
                    "quality_status": audit.quality_status.value,
                    "stage": audit.stage,
                    "payload": audit.payload,
                },
                "claim_audits": [
                    {
                        "claim_id": a.claim_id,
                        "pool_status": a.pool_status.value,
                        "not_evaluated_reason": a.not_evaluated_reason.value
                        if a.not_evaluated_reason
                        else None,
                        "claim_status": a.claim_status.value if a.claim_status else None,
                        "final_placement": a.final_placement.value if a.final_placement else None,
                    }
                    for a in ClaimAuditRepository(session).for_claims(claim_ids)
                ],
                "citation_checks": [
                    {
                        "claim_id": c.claim_id,
                        "passage_alias": c.passage_alias,
                        "cited_chunk_id": c.cited_chunk_id,
                        "outcome": c.outcome.value,
                        "established_by": c.established_by.value if c.established_by else None,
                        "procedure_version": c.procedure_version,
                    }
                    for c in CitationCheckRepository(session).for_claims(claim_ids)
                ],
                "final_claim_checks": [
                    {
                        "section_index": f.section_index,
                        "sentence_index": f.sentence_index,
                        "text": f.text,
                        "change_kinds": f.change_kinds,
                        "pool_status": f.pool_status.value,
                        "claim_status": f.claim_status.value,
                        "not_evaluated_reason": f.not_evaluated_reason.value
                        if f.not_evaluated_reason
                        else None,
                    }
                    for f in (
                        FinalClaimCheckRepository(session).for_report(report.id) if report else []
                    )
                ],
            }
