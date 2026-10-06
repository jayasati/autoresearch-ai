"""
The evidence layer: Claim, Evidence, Citation, VerificationResult, Conflict.

This is the part of the schema the project exists for. Four separate tables where
a naive design would use one, because four different questions must be answerable
independently:

    Claim              what the report asserts
    Citation           what the report *said* its source was
    Evidence           what passages we actually found
    VerificationResult what we concluded, and on the strength of which passage

Keeping citation separate from evidence is the whole trick. A fabricated citation
is a citation with no resolvable source and no evidence behind it. If citations
and evidence were one table, that case could not be represented at all -- and it is
precisely the case this project is built to count.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import (
    CitationStatus,
    ConflictType,
    EvidenceRelation,
    VerificationVerdict,
)
from app.db.base import Base, UUIDPrimaryKey, enum_column

if TYPE_CHECKING:
    from app.models.report import Report, ReportSection
    from app.models.research import ResearchRun
    from app.models.source import DocumentChunk, Source


class Claim(UUIDPrimaryKey, Base):
    """One atomic, individually checkable assertion extracted from a report.

    "Atomic" is the requirement that makes verification meaningful: a sentence
    asserting three things cannot receive one verdict honestly. Extraction splits
    them so each gets its own.

    `start_char`/`end_char` locate the claim in `Report.markdown`, which is what
    lets the UI highlight the exact sentence a verdict applies to.
    """

    __tablename__ = "claim"

    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("report.id", ondelete="CASCADE"), nullable=False
    )
    report_section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("report_section.id", ondelete="SET NULL")
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # The sentence in the report this claim was extracted from, and where it sits.
    source_sentence: Mapped[str | None] = mapped_column(Text)
    start_char: Mapped[int | None] = mapped_column(Integer)
    end_char: Mapped[int | None] = mapped_column(Integer)

    # Not every sentence needs a citation -- definitions and framing do not. The
    # citation-recall metric divides by the claims where this is true, so getting
    # it wrong would distort the result in the project's favour.
    requires_citation: Mapped[bool] = mapped_column(nullable=False, default=True)

    extractor_model: Mapped[str | None] = mapped_column(String(100))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    report: Mapped["Report"] = relationship(back_populates="claims")
    report_section: Mapped["ReportSection | None"] = relationship(back_populates="claims")

    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", order_by="Evidence.rank"
    )
    citations: Mapped[list["Citation"]] = relationship(
        back_populates="claim", cascade="all, delete-orphan"
    )
    verification_result: Mapped["VerificationResult | None"] = relationship(
        back_populates="claim", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("report_id", "position", name="uq_claim_position_per_report"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(
            "(start_char IS NULL AND end_char IS NULL) "
            "OR (start_char >= 0 AND end_char > start_char)",
            name="span_is_null_or_non_empty",
        ),
    )

    @property
    def supporting_evidence(self) -> list["Evidence"]:
        return [e for e in self.evidence if e.relation == EvidenceRelation.SUPPORTS]

    @property
    def contradicting_evidence(self) -> list["Evidence"]:
        return [e for e in self.evidence if e.relation == EvidenceRelation.CONTRADICTS]

    def __repr__(self) -> str:
        return f"<Claim {self.position}: {self.text[:50]!r}>"


class Evidence(UUIDPrimaryKey, Base):
    """A retrieved passage linked to a claim, with its stance recorded.

    The join between the claim graph and the source graph:
    `Claim -> Evidence -> DocumentChunk -> Document -> Source`.

    `relation` is per-passage, not per-claim, which is deliberate: a claim can
    have supporting *and* contradicting evidence simultaneously, and that is
    exactly the input conflict detection needs. A single "is it supported" boolean
    on the claim would erase it.

    `quote_start_char`/`quote_end_char` are offsets into the chunk, narrowing the
    pointer from a passage to the specific words relied on.
    """

    __tablename__ = "evidence"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claim.id", ondelete="CASCADE"), nullable=False
    )
    document_chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_chunk.id", ondelete="CASCADE"), nullable=False
    )

    relation: Mapped[EvidenceRelation] = mapped_column(
        enum_column(EvidenceRelation), nullable=False, default=EvidenceRelation.NEUTRAL
    )

    # Retrieval similarity, and the rank the chunk was returned at.
    similarity: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)

    # The exact words relied on. A verdict without one of these is not evidence of
    # anything -- see VerificationResult.
    quoted_text: Mapped[str | None] = mapped_column(Text)
    quote_start_char: Mapped[int | None] = mapped_column(Integer)
    quote_end_char: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    claim: Mapped[Claim] = relationship(back_populates="evidence")
    document_chunk: Mapped["DocumentChunk"] = relationship(back_populates="evidence")

    __table_args__ = (
        UniqueConstraint("claim_id", "document_chunk_id", name="uq_evidence_chunk_per_claim"),
        CheckConstraint(
            "similarity IS NULL OR (similarity >= 0 AND similarity <= 1)",
            name="similarity_is_a_fraction",
        ),
        CheckConstraint("rank IS NULL OR rank >= 0", name="rank_non_negative"),
        CheckConstraint(
            "(quote_start_char IS NULL AND quote_end_char IS NULL) "
            "OR (quote_start_char >= 0 AND quote_end_char > quote_start_char)",
            name="quote_span_is_null_or_non_empty",
        ),
        Index("ix_evidence_claim_relation", "claim_id", "relation"),
    )

    def __repr__(self) -> str:
        return f"<Evidence {self.relation} similarity={self.similarity}>"


class Citation(UUIDPrimaryKey, Base):
    """A source the *report* claimed to be relying on, and whether it holds up.

    Kept separate from `Evidence` because the two answer different questions:
    evidence is what we found, a citation is what the model said. The gap between
    them is the project's main result.

    `source_id` is nullable, and that is the central modelling decision here: a
    **fabricated** citation points at a source that does not exist, so there is no
    row to reference. The check constraint below enforces the correspondence, which
    makes "fabricated" a structural fact rather than a label someone remembered to
    set.
    """

    __tablename__ = "citation"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claim.id", ondelete="CASCADE"), nullable=False
    )

    # NULL when the cited source does not exist (fabricated).
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT")
    )

    # What the report actually wrote: the marker and the raw reference text. Kept
    # verbatim so a fabricated citation can be reported as the model phrased it.
    marker: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_reference: Mapped[str | None] = mapped_column(Text)

    status: Mapped[CitationStatus] = mapped_column(enum_column(CitationStatus), nullable=False)

    # The passage that justifies calling this citation valid. Present for valid
    # citations, absent for the three failure modes.
    supporting_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_chunk.id", ondelete="SET NULL")
    )

    http_status: Mapped[int | None] = mapped_column(Integer)  # for `broken`
    failure_reason: Mapped[str | None] = mapped_column(Text)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    claim: Mapped[Claim] = relationship(back_populates="citations")
    source: Mapped["Source | None"] = relationship(back_populates="citations")

    __table_args__ = (
        UniqueConstraint("claim_id", "marker", name="uq_citation_marker_per_claim"),
        # A fabricated citation has no source; every other status must name one.
        CheckConstraint(
            "(status = 'fabricated' AND source_id IS NULL) "
            "OR (status <> 'fabricated' AND source_id IS NOT NULL)",
            name="only_fabricated_citations_lack_a_source",
        ),
        # Claiming a citation is valid requires saying why.
        CheckConstraint(
            "status <> 'valid' OR supporting_chunk_id IS NOT NULL",
            name="valid_citations_name_a_passage",
        ),
        # Every failure must be explained.
        CheckConstraint(
            "status = 'valid' OR failure_reason IS NOT NULL",
            name="failed_citations_have_a_reason",
        ),
        Index("ix_citation_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<Citation {self.marker} {self.status}>"


class VerificationResult(UUIDPrimaryKey, Base):
    """The verdict on one claim, and the passage it rests on.

    One per claim, completing `Claim -> Evidence -> VerificationResult`.

    `supporting_evidence_id` is enforced, not advisory. ARCHITECTURE.md lists "the
    verifier itself hallucinates" as a known risk, with the mitigation: *require it
    to quote the supporting span; a verdict without a quote is
    not_enough_evidence*. The check constraint below is that mitigation, written
    into the schema so it cannot be forgotten in application code.
    """

    __tablename__ = "verification_result"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claim.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    verdict: Mapped[VerificationVerdict] = mapped_column(
        enum_column(VerificationVerdict), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Float)

    # The one piece of evidence the verdict was based on.
    supporting_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="SET NULL")
    )
    rationale: Mapped[str | None] = mapped_column(Text)

    # True when the verifier asserted a stronger verdict but produced no quote, so
    # it was downgraded. Recorded rather than silently applied: how often the
    # verifier overclaims is itself a finding worth reporting.
    downgraded: Mapped[bool] = mapped_column(nullable=False, default=False)

    verifier_model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    claim: Mapped[Claim] = relationship(back_populates="verification_result")
    supporting_evidence: Mapped[Evidence | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_is_a_fraction",
        ),
        # The ARCHITECTURE mitigation, enforced: an evidence-bearing verdict must
        # point at the evidence.
        CheckConstraint(
            "verdict IN ('unsupported', 'not_enough_evidence') "
            "OR supporting_evidence_id IS NOT NULL",
            name="evidence_bearing_verdicts_cite_evidence",
        ),
        Index("ix_verification_result_verdict", "verdict"),
    )

    @property
    def is_hallucination(self) -> bool:
        """Counts toward the hallucination rate."""
        return self.verdict in {
            VerificationVerdict.UNSUPPORTED,
            VerificationVerdict.CONTRADICTED,
        }

    def __repr__(self) -> str:
        return f"<VerificationResult {self.verdict} confidence={self.confidence}>"


class Conflict(UUIDPrimaryKey, Base):
    """Two pieces of evidence that disagree.

    Recorded between `Evidence` rows rather than between sources, because "these
    two papers disagree" is not actionable while "these two passages disagree about
    this claim" is -- it names the claim, both passages, and through them both
    sources, so the UI can show the disagreement rather than assert it.
    """

    __tablename__ = "conflict"

    research_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_run.id", ondelete="CASCADE"), nullable=False
    )

    # The claim they disagree about, when the conflict was found while verifying
    # one. Null for disagreements found between sources independently of a claim.
    claim_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("claim.id", ondelete="CASCADE"))

    evidence_a_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"), nullable=False
    )
    evidence_b_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"), nullable=False
    )

    conflict_type: Mapped[ConflictType] = mapped_column(enum_column(ConflictType), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)

    detector_model: Mapped[str | None] = mapped_column(String(100))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    research_run: Mapped["ResearchRun"] = relationship(back_populates="conflicts")
    evidence_a: Mapped[Evidence] = relationship(foreign_keys=[evidence_a_id])
    evidence_b: Mapped[Evidence] = relationship(foreign_keys=[evidence_b_id])

    __table_args__ = (
        # A passage cannot conflict with itself.
        CheckConstraint("evidence_a_id <> evidence_b_id", name="conflict_needs_two_passages"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_is_a_fraction",
        ),
        UniqueConstraint(
            "evidence_a_id",
            "evidence_b_id",
            "conflict_type",
            name="uq_conflict_evidence_pair_and_type",
        ),
        Index("ix_conflict_run_type", "research_run_id", "conflict_type"),
    )

    def __repr__(self) -> str:
        return f"<Conflict {self.conflict_type}>"
