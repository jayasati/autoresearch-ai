"""
Shapes for report-quality-v1 (docs/REPORT_QUALITY_PIPELINE.md).

Two kinds live here, kept apart on purpose:

- **Model-filled** (`ReportSentence`, `ReportSectionDraft`, `StructuredReport`): what
  a later stage will ask the LLM to return. Nothing in them is trusted; every alias
  is checked against the code-owned `AliasTable`. They have no field that can carry
  a title, URL, DOI, year, venue or author — the model cannot write metadata.
- **Code-produced** (everything else): deterministic results of the S1 modules.

Stage S1 defines these and the pure functions over them; nothing in the legacy
pipeline imports this package.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# --- model-filled (validated, never trusted) ------------------------------------

SentenceKind = Literal["finding", "qualification", "uncertainty", "transition"]

_ALIAS_CHARS = "[] \t"


def _normalise_aliases(values: list[str]) -> list[str]:
    cleaned = [value.strip(_ALIAS_CHARS).upper() for value in values]
    return list(dict.fromkeys(value for value in cleaned if value))


class ReportSentence(BaseModel):
    text: str = Field(min_length=1)
    kind: SentenceKind
    claim_ids: list[str] = Field(description='Claim aliases, e.g. "C7". Empty in a draft.')
    evidence_ids: list[str] = Field(description='Passage aliases, e.g. "E3".')
    finding_refs: list[str] = Field(description='Finding aliases, e.g. "F2".')

    @field_validator("text")
    @classmethod
    def single_line(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("claim_ids", "evidence_ids", "finding_refs")
    @classmethod
    def normalise(cls, values: list[str]) -> list[str]:
        return _normalise_aliases(values)


class ReportSectionDraft(BaseModel):
    subquestion: str = Field(description='"Q2", or "SYNTHESIS" for an overview section.')
    heading: str = Field(min_length=1)
    sentences: list[ReportSentence]

    @field_validator("subquestion")
    @classmethod
    def normalise_subquestion(cls, value: str) -> str:
        return value.strip(_ALIAS_CHARS).upper()

    @field_validator("heading")
    @classmethod
    def plain_heading(cls, value: str) -> str:
        """Headings are prose; markdown markers and line breaks are not."""
        return " ".join(value.replace("#", " ").split()) or "Findings"


class StructuredReport(BaseModel):
    title: str = Field(min_length=1)
    sections: list[ReportSectionDraft]

    @field_validator("title")
    @classmethod
    def plain_title(cls, value: str) -> str:
        return " ".join(value.replace("#", " ").split()) or "Research report"


# --- the code-owned alias table ------------------------------------------------


@dataclass(frozen=True)
class PassageRef:
    """What an `E#` alias stands for: one chunk, belonging to one labelled source."""

    chunk_id: uuid.UUID
    source_label: str  # the frozen S# of the chunk's source


@dataclass(frozen=True)
class AliasTable:
    """Short aliases the models see → stable identities. Owned by code, stored in
    the audit; an alias not in here is a validation error, never a lookup miss."""

    sources: dict[str, uuid.UUID]  # "S3" → source.id
    passages: dict[str, PassageRef]  # "E5" → chunk + its source label
    claims: dict[str, uuid.UUID] = field(default_factory=dict)  # "C7" → claim.id
    subquestions: dict[str, uuid.UUID] = field(default_factory=dict)  # "Q2" → sub_question.id

    def as_record(self) -> dict:
        return {
            "sources": {alias: str(value) for alias, value in self.sources.items()},
            "passages": {
                alias: {"chunk_id": str(ref.chunk_id), "source_label": ref.source_label}
                for alias, ref in self.passages.items()
            },
            "claims": {alias: str(value) for alias, value in self.claims.items()},
            "subquestions": {alias: str(value) for alias, value in self.subquestions.items()},
        }


# --- claim status (design §3) ----------------------------------------------------


class PoolStatus(StrEnum):
    """verification-v2's verdict, or why there is none."""

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONTRADICTED = "contradicted"
    UNSUPPORTED = "unsupported"
    NOT_ENOUGH_EVIDENCE = "not_enough_evidence"
    NOT_EVALUATED = "not_evaluated"


class CitationOutcome(StrEnum):
    """citation-check-v1's result for one cited passage."""

    SUPPORTS = "supports"
    PARTIALLY_SUPPORTS = "partially_supports"
    CONTRADICTS = "contradicts"
    DOES_NOT_ESTABLISH = "does_not_establish"
    INVALID_REFERENCE = "invalid_reference"
    NOT_EVALUATED = "not_evaluated"


class ClaimStatus(StrEnum):
    """The five reader-facing categories, kept distinct (design §3)."""

    SUPPORTED_BY_CITED_SOURCE = "supported_by_cited_source"
    CITATION_NOT_CONFIRMED = "supported_citation_not_confirmed"
    CONTESTED = "contested"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_EVALUATED = "not_evaluated"


# --- quantitative audit (design §4; audit-only) ----------------------------------


class QuantCategory(StrEnum):
    UNIT_OR_DENOMINATOR = "unit_or_denominator_difference"
    BASELINE_OR_PERIOD = "baseline_or_period_difference"
    METHOD_OR_SCOPE = "method_or_scope_difference"
    CONSISTENT = "consistent_within_rounding_or_uncertainty"
    SAME_METRIC_DISAGREEMENT = "same_metric_value_disagreement"
    UNRESOLVED = "unresolved_discrepancy"


@dataclass(frozen=True)
class QuantityMention:
    """One quantity found in a text, with the context attributed to it.

    Context facets are `None` when the text does not state them. Unknown never
    equals anything — the classifier treats a one-sided facet as unknown.
    """

    raw: str
    start: int
    end: int
    value: float  # in the dimension's base unit, scale applied
    low: float  # the stated interval (range, ±, CI), base unit; == value if none
    high: float
    step: float  # half of this is the rounding tolerance, base unit
    approximate: bool
    unit: str | None  # normalised unit as written ("mg", "percent", "pp", ...)
    dimension: str  # what the unit measures ("mass", "percent", "count", ...)
    stated_number: float  # the leading number exactly as written (no conversion)
    denominator: frozenset[tuple[float, str]] | None = None  # alternatives: (amount, unit)
    period: frozenset[str] | None = None
    baseline: frozenset[str] | None = None
    method: frozenset[str] | None = None
    geography: frozenset[str] | None = None
    population: frozenset[str] | None = None
    setting: frozenset[str] | None = None  # care setting / endpoint
    entities: frozenset[str] | None = None


PairRelation = Literal["claim_claim", "claim_quote", "claim_cited_passage", "case"]


@dataclass(frozen=True)
class QuantitativeFinding:
    """One classified pair. Audit-only: never changes a verdict, never deletes."""

    alias: str  # "F#"
    relation: PairRelation
    category: QuantCategory
    owner_a: str  # claim alias / id of side a
    owner_b: str
    mention_a: QuantityMention
    mention_b: QuantityMention
    reason: str  # code-templated; which rule fired


# --- subquestion and conflict assessment (design stage 9) ------------------------

SubquestionStatusName = Literal["well_supported", "mixed", "contested", "evidence_gap"]


@dataclass(frozen=True)
class SubquestionAssessment:
    alias: str
    is_core: bool
    status: SubquestionStatusName
    addressed: bool  # any claim of the report falls under it
    passages: int
    academic_passages: int
    claims: int
    statuses: dict[str, int]  # ClaimStatus value → count
    gap_reason: Literal["no_evidence", "provider_failure"] | None = None


@dataclass(frozen=True)
class SourceProfile:
    """Provider-reported metadata only; nothing inferred, no quality score."""

    label: str
    source_type: str
    year: int | None
    venue: str | None
    evidence_depth: str


@dataclass(frozen=True)
class ConflictContext:
    conflict_type: str
    description: str  # the stored, verifier-written factual description
    side_supporting: SourceProfile | None
    side_contradicting: SourceProfile | None
    framing: str  # code-templated: a possible disagreement, never proof of falsity


# --- final validation (design §5) ------------------------------------------------


class Placement(StrEnum):
    BODY = "body"
    QUALIFIED = "qualified"  # body, explicitly qualified
    CITATION_NOT_CONFIRMED = "citation_not_confirmed"
    CONTESTED = "contested"
    UNRESOLVED = "unresolved"
    UNVERIFIED_LIST = "unverified_list"
    NOT_EVALUATED = "not_evaluated"


IN_BODY = frozenset({Placement.BODY, Placement.QUALIFIED})

ActionName = Literal[
    "reference_rejected",
    "meaning_changed",
    "marker_removed",
    "qualifier_added",
    "moved",
    "untraceable_statement",
    "duplicate_flagged",
    "omission_restored",
]


@dataclass(frozen=True)
class ValidationAction:
    """One recorded repair, move or flag — with the original text, always."""

    action: ActionName
    section: str
    sentence_index: int | None  # position in the model's section; None for restorations
    original_text: str
    detail: str


OutcomeStatus = Literal["complete", "degraded", "finalization_failed"]
