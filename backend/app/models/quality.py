"""
report-quality-v1 records (stage 29, S2; docs/REPORT_QUALITY_PIPELINE.md §8).

Four additive tables, kept apart from the legacy evidence layer on purpose:

    ReportQualityAudit  one per finalized report: state, stage, the internal audit
    ClaimAudit          one per claim: the pipeline's view of it, including
                        "not evaluated, and why" — which `verification_result`
                        cannot express without changing verification-v2's meaning
    CitationCheck       citation-check-v1: one row per (claim, cited passage)
    FinalClaimCheck     re-verification of a sentence whose meaning changed in
                        revision — stored here, NOT as claim rows, so the historical
                        draft-claim metrics of a run are never altered

Every invariant that can be a CHECK is one. The central safety property — a
rewritten sentence is never "supported by its cited source" unless a verification
actually ran and found support in a passage the sentence cites — is enforced in
two layers: the CHECKs below (a verdict-bearing row names a model and prompt
version; support needs a pool verdict of support), and the store
(`app/services/quality/store.py`), which also checks that a supporting chunk is
among the cited chunks and that every quote is verbatim.

Nothing in the legacy pipeline writes or reads these tables.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import (
    CitationOutcome,
    ClaimStatus,
    EstablishedBy,
    NotEvaluatedReason,
    PipelineVersion,
    Placement,
    PoolStatus,
    ReportQualityStatus,
)
from app.db.base import Base, Timestamped, UUIDPrimaryKey, enum_column

_REASON_IFF_NOT_EVALUATED = (
    "(pool_status = 'not_evaluated' AND not_evaluated_reason IS NOT NULL) "
    "OR (pool_status <> 'not_evaluated' AND not_evaluated_reason IS NULL)"
)


class ReportQualityAudit(UUIDPrimaryKey, Timestamped, Base):
    """The internal audit of one report-quality-v1 finalization.

    Separate from the report text by design: alias tables, repairs, findings, stage
    accounts and metrics live in `payload`, never in `report.final_markdown`.
    """

    __tablename__ = "report_quality_audit"

    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("report.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    pipeline_version: Mapped[PipelineVersion] = mapped_column(
        enum_column(PipelineVersion), nullable=False
    )
    quality_status: Mapped[ReportQualityStatus] = mapped_column(
        enum_column(ReportQualityStatus), nullable=False
    )
    stage: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        # A legacy report has no quality audit.
        CheckConstraint("pipeline_version <> 'legacy'", name="audit_is_not_legacy"),
    )

    def __repr__(self) -> str:
        return f"<ReportQualityAudit {self.report_id} {self.quality_status}>"


class ClaimAudit(UUIDPrimaryKey, Timestamped, Base):
    """The pipeline's record of one claim, including when it was NOT evaluated."""

    __tablename__ = "claim_audit"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claim.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    pool_status: Mapped[PoolStatus] = mapped_column(enum_column(PoolStatus), nullable=False)
    not_evaluated_reason: Mapped[NotEvaluatedReason | None] = mapped_column(
        enum_column(NotEvaluatedReason)
    )
    claim_status: Mapped[ClaimStatus | None] = mapped_column(enum_column(ClaimStatus))
    alignment_status: Mapped[str | None] = mapped_column(String(20))
    aligned_sentence_index: Mapped[int | None] = mapped_column(Integer)
    alignment_score: Mapped[float | None] = mapped_column(Float)
    priority_rank: Mapped[int | None] = mapped_column(Integer)
    final_placement: Mapped[Placement | None] = mapped_column(enum_column(Placement))

    __table_args__ = (
        CheckConstraint(_REASON_IFF_NOT_EVALUATED, name="not_evaluated_has_a_reason"),
        CheckConstraint(
            "pool_status <> 'not_evaluated' OR claim_status IS NULL "
            "OR claim_status = 'not_evaluated'",
            name="unevaluated_claims_are_not_presented",
        ),
        CheckConstraint(
            "alignment_status IS NULL OR alignment_status IN ('aligned', 'unaligned')",
            name="alignment_status_vocabulary",
        ),
        CheckConstraint(
            "(alignment_status = 'aligned') = (aligned_sentence_index IS NOT NULL) "
            "OR alignment_status IS NULL",
            name="aligned_claims_name_a_sentence",
        ),
    )

    def __repr__(self) -> str:
        return f"<ClaimAudit {self.claim_id} {self.pool_status}>"


class CitationCheck(UUIDPrimaryKey, Base):
    """citation-check-v1: does THIS cited passage establish the claim it accompanies?

    Kept apart from `citation.status`, whose legacy meaning (some passage of the
    cited source, found by similarity to the claim, supports it) must not change.
    """

    __tablename__ = "citation_check"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claim.id", ondelete="CASCADE"), nullable=False
    )
    citation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("citation.id", ondelete="SET NULL")
    )
    passage_alias: Mapped[str] = mapped_column(String(20), nullable=False)
    # NULL only for an invalid reference: an alias that names no passage.
    cited_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_chunk.id", ondelete="CASCADE")
    )
    outcome: Mapped[CitationOutcome] = mapped_column(enum_column(CitationOutcome), nullable=False)
    not_evaluated_reason: Mapped[NotEvaluatedReason | None] = mapped_column(
        enum_column(NotEvaluatedReason)
    )
    established_by: Mapped[EstablishedBy | None] = mapped_column(enum_column(EstablishedBy))
    quoted_text: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))
    procedure_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("claim_id", "passage_alias", name="uq_citation_check_claim_passage"),
        CheckConstraint(
            "(outcome = 'not_evaluated' AND not_evaluated_reason IS NOT NULL) "
            "OR (outcome <> 'not_evaluated' AND not_evaluated_reason IS NULL)",
            name="not_evaluated_has_a_reason",
        ),
        CheckConstraint(
            "(outcome = 'invalid_reference') = (cited_chunk_id IS NULL)",
            name="only_invalid_references_lack_a_chunk",
        ),
        # A relation is only established with a verbatim quote and a named procedure.
        CheckConstraint(
            "outcome NOT IN ('supports', 'partially_supports', 'contradicts') "
            "OR (quoted_text IS NOT NULL AND established_by IS NOT NULL)",
            name="established_relations_are_quoted",
        ),
        CheckConstraint(
            "outcome IN ('supports', 'partially_supports', 'contradicts') "
            "OR established_by IS NULL",
            name="only_relations_are_established",
        ),
        # A citation-check verdict means a model actually judged it.
        CheckConstraint(
            "established_by IS NULL OR established_by <> 'citation_check' OR model IS NOT NULL",
            name="checked_relations_name_a_model",
        ),
        Index("ix_citation_check_outcome", "outcome"),
    )

    def __repr__(self) -> str:
        return f"<CitationCheck {self.passage_alias} {self.outcome}>"


class FinalClaimCheck(UUIDPrimaryKey, Base):
    """Re-verification of one final-report sentence whose meaning changed in revision.

    A sentence flagged for re-verification is recorded here with `not_evaluated`
    (reason `awaiting_reverification`) until a verification has actually run; only a
    row with a real verdict, a model and a prompt version can carry support.
    """

    __tablename__ = "final_claim_check"

    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("report.id", ondelete="CASCADE"), nullable=False
    )
    derived_from_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("claim.id", ondelete="SET NULL")
    )
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sentence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    change_kinds: Mapped[list] = mapped_column(JSON, nullable=False)
    cited_chunk_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    pool_status: Mapped[PoolStatus] = mapped_column(enum_column(PoolStatus), nullable=False)
    not_evaluated_reason: Mapped[NotEvaluatedReason | None] = mapped_column(
        enum_column(NotEvaluatedReason)
    )
    claim_status: Mapped[ClaimStatus] = mapped_column(enum_column(ClaimStatus), nullable=False)
    supported_chunk_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "report_id", "section_index", "sentence_index", name="uq_final_claim_check_sentence"
        ),
        CheckConstraint("section_index >= 0 AND sentence_index >= 0", name="indices_non_negative"),
        CheckConstraint(_REASON_IFF_NOT_EVALUATED, name="not_evaluated_has_a_reason"),
        CheckConstraint(
            "pool_status <> 'not_evaluated' OR claim_status = 'not_evaluated'",
            name="unevaluated_sentences_are_not_presented",
        ),
        # Support requires that a verification actually ran and found support.
        CheckConstraint(
            "claim_status <> 'supported_by_cited_source' OR "
            "(pool_status IN ('supported', 'partially_supported') "
            "AND model IS NOT NULL AND prompt_version IS NOT NULL)",
            name="support_requires_a_real_verification",
        ),
        # Every verdict names the procedure; a model may be absent only for
        # not_enough_evidence reached with no passages (the verifier's no-call path).
        CheckConstraint(
            "pool_status = 'not_evaluated' OR prompt_version IS NOT NULL",
            name="verdicts_name_their_procedure",
        ),
        CheckConstraint(
            "pool_status IN ('not_evaluated', 'not_enough_evidence') OR model IS NOT NULL",
            name="judged_verdicts_name_a_model",
        ),
    )

    def __repr__(self) -> str:
        return f"<FinalClaimCheck {self.section_index}.{self.sentence_index} {self.claim_status}>"
