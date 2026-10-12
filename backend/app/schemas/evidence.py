"""
Schemas for the evidence layer.

The validators here are not formalities. Three of them encode decisions recorded
in ARCHITECTURE.md and ADR 0001, so a future change to application code cannot
quietly abandon them:

1. A verdict that asserts support must quote the passage supporting it. Without a
   quote it is downgraded to `not_enough_evidence` -- the documented mitigation for
   "the verifier itself hallucinates".
2. A fabricated citation has no source, and every other status has one.
3. A conflict needs two different passages.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.core.constants import (
    CitationStatus,
    ClaimImportance,
    ClaimType,
    ConflictType,
    EvidenceRelation,
    VerificationVerdict,
)
from app.schemas.base import IdentifiedModel, ReadModel, WriteModel

# Verdicts that assert something about the evidence, and therefore must cite it.
EVIDENCE_BEARING_VERDICTS = frozenset(
    {
        VerificationVerdict.SUPPORTED,
        VerificationVerdict.PARTIALLY_SUPPORTED,
        VerificationVerdict.CONTRADICTED,
    }
)


class ClaimCreate(WriteModel):
    report_id: uuid.UUID
    report_section_id: uuid.UUID | None = None
    position: int = Field(ge=0)
    text: str = Field(min_length=1)
    claim_type: ClaimType
    importance: ClaimImportance
    source_sentence: str | None = None
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, ge=0)
    requires_citation: bool = True
    extractor_model: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def span_is_either_absent_or_valid(self) -> "ClaimCreate":
        """Both offsets or neither. One alone cannot locate anything."""
        start, end = self.start_char, self.end_char
        if (start is None) != (end is None):
            raise ValueError("start_char and end_char must be given together")
        if start is not None and end is not None and end <= start:
            raise ValueError("end_char must be greater than start_char")
        return self


class EvidenceCreate(WriteModel):
    """A passage linked to a claim, with its stance."""

    claim_id: uuid.UUID
    document_chunk_id: uuid.UUID
    relation: EvidenceRelation = EvidenceRelation.NEUTRAL
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    rank: int | None = Field(default=None, ge=0)
    quoted_text: str | None = None
    quote_start_char: int | None = Field(default=None, ge=0)
    quote_end_char: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def quote_offsets_match_the_quote(self) -> "EvidenceCreate":
        offsets_given = self.quote_start_char is not None or self.quote_end_char is not None
        if offsets_given:
            if self.quote_start_char is None or self.quote_end_char is None:
                raise ValueError("quote_start_char and quote_end_char must be given together")
            if self.quote_end_char <= self.quote_start_char:
                raise ValueError("quote_end_char must be greater than quote_start_char")
            if self.quoted_text is not None:
                span = self.quote_end_char - self.quote_start_char
                if span != len(self.quoted_text):
                    raise ValueError(
                        f"quote span is {span} characters but quoted_text is "
                        f"{len(self.quoted_text)}; the offsets must address exactly "
                        "this quote within the chunk"
                    )
        # A passage asserted to support or contradict a claim must say which words
        # do so. Otherwise the relation is an opinion with nothing behind it.
        if self.relation is not EvidenceRelation.NEUTRAL and not self.quoted_text:
            raise ValueError(
                f"relation '{self.relation.value}' requires quoted_text naming the "
                "words that support or contradict the claim"
            )
        return self


class EvidenceRead(IdentifiedModel):
    claim_id: uuid.UUID
    document_chunk_id: uuid.UUID
    relation: EvidenceRelation
    similarity: float | None
    rank: int | None
    quoted_text: str | None
    quote_start_char: int | None
    quote_end_char: int | None


class CitationCreate(WriteModel):
    """What the report said its source was, and the outcome of checking it.

    The validator enforces the four-way distinction the whole project rests on.
    `fabricated` and `misattributed` are different defects: the first means no such
    source exists, the second means a real source was cited for a claim it does not
    make. Collapsing them would hide the more interesting failure.
    """

    claim_id: uuid.UUID
    source_id: uuid.UUID | None = None
    marker: str = Field(min_length=1, max_length=20)
    raw_reference: str | None = None
    status: CitationStatus
    supporting_chunk_id: uuid.UUID | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    failure_reason: str | None = None

    @model_validator(mode="after")
    def status_and_evidence_must_agree(self) -> "CitationCreate":
        if self.status is CitationStatus.FABRICATED:
            if self.source_id is not None:
                raise ValueError(
                    "a fabricated citation names a source that does not exist, so "
                    "source_id must be null; use 'misattributed' when the source is "
                    "real but does not support the claim"
                )
        elif self.source_id is None:
            raise ValueError(f"status '{self.status.value}' requires a source_id")

        if self.status is CitationStatus.VALID:
            if self.supporting_chunk_id is None:
                raise ValueError(
                    "a valid citation must name the passage that supports the claim"
                )
            if self.failure_reason is not None:
                raise ValueError("a valid citation has no failure_reason")
        elif self.status is CitationStatus.UNVALIDATED:
            # Recorded at extraction, not yet checked: no failure to explain, and no
            # supporting passage to claim.
            if self.failure_reason is not None:
                raise ValueError("an unvalidated citation has no failure_reason yet")
            if self.supporting_chunk_id is not None:
                raise ValueError(
                    "an unvalidated citation cannot already name a supporting passage"
                )
        else:
            if not self.failure_reason:
                raise ValueError(f"status '{self.status.value}' requires a failure_reason")

        if self.http_status is not None and self.status is not CitationStatus.BROKEN:
            raise ValueError("http_status applies only to a broken citation")
        return self


class CitationRead(IdentifiedModel):
    claim_id: uuid.UUID
    source_id: uuid.UUID | None
    marker: str
    raw_reference: str | None
    status: CitationStatus
    supporting_chunk_id: uuid.UUID | None
    http_status: int | None
    failure_reason: str | None
    validated_at: datetime | None


class VerificationResultCreate(WriteModel):
    """A verdict on one claim.

    The downgrade rule lives here. ARCHITECTURE.md §7 records the risk -- the
    verifier can hallucinate its own verdict -- and the mitigation: *require it to
    quote the supporting span; a verdict without a quote is not_enough_evidence*.

    This is implemented as a **downgrade, not a rejection**, because the verifier's
    output is data about the verifier. Refusing it would discard the observation;
    downgrading it and setting `downgraded=True` keeps it, so "how often did the
    verifier overclaim" becomes a measurable number rather than a lost error.
    """

    claim_id: uuid.UUID
    verdict: VerificationVerdict
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supporting_evidence_id: uuid.UUID | None = None
    rationale: str | None = None
    verifier_model: str | None = Field(default=None, max_length=100)
    prompt_version: str | None = Field(default=None, max_length=40)
    downgraded: bool = False

    @model_validator(mode="after")
    def unquoted_verdicts_are_downgraded(self) -> "VerificationResultCreate":
        if self.verdict in EVIDENCE_BEARING_VERDICTS and self.supporting_evidence_id is None:
            object.__setattr__(self, "verdict", VerificationVerdict.NOT_ENOUGH_EVIDENCE)
            object.__setattr__(self, "downgraded", True)
        return self

    @property
    def is_hallucination(self) -> bool:
        return self.verdict in {
            VerificationVerdict.UNSUPPORTED,
            VerificationVerdict.CONTRADICTED,
        }


class VerificationResultRead(IdentifiedModel):
    claim_id: uuid.UUID
    verdict: VerificationVerdict
    confidence: float | None
    supporting_evidence_id: uuid.UUID | None
    rationale: str | None
    downgraded: bool
    verifier_model: str | None
    prompt_version: str | None
    verified_at: datetime


class ConflictCreate(WriteModel):
    research_run_id: uuid.UUID
    claim_id: uuid.UUID | None = None
    evidence_a_id: uuid.UUID
    evidence_b_id: uuid.UUID
    conflict_type: ConflictType
    description: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    detector_model: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def a_conflict_needs_two_passages(self) -> "ConflictCreate":
        if self.evidence_a_id == self.evidence_b_id:
            raise ValueError("a passage cannot conflict with itself")
        return self


class ConflictRead(IdentifiedModel):
    research_run_id: uuid.UUID
    claim_id: uuid.UUID | None
    evidence_a_id: uuid.UUID
    evidence_b_id: uuid.UUID
    conflict_type: ConflictType
    description: str
    confidence: float | None
    detected_at: datetime


class ClaimRead(IdentifiedModel):
    report_id: uuid.UUID
    report_section_id: uuid.UUID | None
    position: int
    text: str
    claim_type: ClaimType
    importance: ClaimImportance
    source_sentence: str | None
    start_char: int | None
    end_char: int | None
    requires_citation: bool
    extractor_model: str | None = None
    extracted_at: datetime
    verification_result: VerificationResultRead | None = None
    citations: list[CitationRead] = Field(default_factory=list)
    evidence: list[EvidenceRead] = Field(default_factory=list)


class ExtractedClaimRead(IdentifiedModel):
    """One stored claim in the shape the extraction endpoint reports.

    The spec's required fields, under the spec's names (`claim_id` is the row id,
    `claim_text` the text), plus what makes the row auditable: the markers as the
    report wrote them, and which of them resolved to a real retrieved source.
    """

    claim_id: uuid.UUID = Field(description="The claim row's id; same value as `id`.")
    research_id: uuid.UUID
    claim_text: str
    claim_type: ClaimType
    importance: ClaimImportance
    section: str | None = Field(description="Heading of the report section it sits under.")
    citation_ids: list[uuid.UUID] = Field(
        default_factory=list, description="The stored citation rows for this claim."
    )

    position: int
    requires_citation: bool
    markers: list[str] = Field(
        default_factory=list, description="Citation markers as written, e.g. ['S1', 'S3']."
    )
    cited_source_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Source rows the markers resolved to; a marker that resolved to "
        "nothing is recorded as a fabricated citation instead.",
    )


#: The spec's five-way citation assessment, derived per claim from stored
#: primitives (citation statuses, the verdict, and whether a conflict was stored) —
#: never stored itself, so it can always be recomputed from the facts.
ClaimAssessment = Literal[
    "valid", "partially_valid", "invalid", "missing", "conflicting", "not_required"
]


class ClaimVerificationRead(ReadModel):
    """One claim's verification outcome, with the derived citation assessment."""

    claim_id: uuid.UUID
    claim_text: str
    verdict: VerificationVerdict
    confidence: float | None
    downgraded: bool = Field(
        description="True when the verifier asserted support without a verbatim "
        "quote and the verdict was downgraded to not_enough_evidence."
    )
    rationale: str | None
    citation_assessment: ClaimAssessment
    citations: dict[str, str] = Field(
        default_factory=dict, description="Marker → citation status after validation."
    )
    evidence_count: int = Field(ge=0)
    conflict_id: uuid.UUID | None


class DetectedConflictRead(ReadModel):
    """A stored conflict, with the verifier's direct/nuanced reading.

    `kind` lives only in the response: the row stores the conflict type and the
    factual description; whether a disagreement is 'direct' or 'nuanced' is the
    verifier's framing of the same facts.
    """

    conflict_id: uuid.UUID
    claim_id: uuid.UUID
    kind: Literal["direct", "nuanced"] | None
    conflict_type: ConflictType
    description: str


class VerificationRunResponse(ReadModel):
    """What one verification pass did for a run."""

    research_id: uuid.UUID
    report_id: uuid.UUID
    claims_verified: int = Field(ge=0)
    claims_skipped: int = Field(
        ge=0, description="Claims whose verification failed twice; re-POST retries them."
    )
    verdicts: dict[str, int] = Field(default_factory=dict)
    citation_statuses: dict[str, int] = Field(default_factory=dict)
    assessments: dict[str, int] = Field(default_factory=dict)
    conflicts_detected: int = Field(ge=0)
    conflicts: list[DetectedConflictRead] = Field(default_factory=list)
    results: list[ClaimVerificationRead] = Field(default_factory=list)
    verifier_model: str
    prompt_version: str
    llm_calls: int = Field(ge=0)
    token_usage: dict[str, int] | None = None
    duration_seconds: float = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class ClaimExtractionResponse(IdentifiedModel):
    """What claim extraction did for one run. `id` is the report's id."""

    research_id: uuid.UUID
    claim_count: int = Field(ge=0)
    claims: list[ExtractedClaimRead]
    extractor_model: str
    prompt_version: str
    llm_calls: int = Field(ge=1)
    retried: bool = Field(description="True when the first output failed validation.")
    token_usage: dict[str, int] | None = None
    duration_seconds: float = Field(ge=0)
