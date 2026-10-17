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

from app.core.constants import CitationOutcome as CitationOutcome
from app.core.constants import ClaimStatus as ClaimStatus
from app.core.constants import Placement as Placement
from app.core.constants import PoolStatus as PoolStatus

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


# --- the coverage plan (stages 1–2; model-filled, validated by code) -------------


class QuestionAnalysis(BaseModel):
    question_type: Literal[
        "descriptive", "causal", "comparative", "quantitative", "historical", "contested"
    ]
    key_entities: list[str] = Field(description="At most 8 named entities the question is about.")
    time_scope: str | None = Field(description="The period the question concerns, or null.")
    population_or_scope: str | None = Field(
        description="The population, place or setting the question concerns, or null."
    )
    needs_quantitative_evidence: bool
    premise_to_check: str | None = Field(
        description="A presupposition of the question the report must test, not assume; or null."
    )

    @field_validator("key_entities")
    @classmethod
    def clean_entities(cls, values: list[str]) -> list[str]:
        cleaned = [" ".join(value.split()) for value in values]
        return list(dict.fromkeys(value for value in cleaned if value))[:8]


class PlannedSubquestion(BaseModel):
    text: str = Field(min_length=10, description="One research subquestion, as a full question.")
    rationale: str = Field(description="Why answering the topic needs this subquestion.")
    web_query: str = Field(description="A web search query for this subquestion.")
    academic_query: str = Field(description="A keyword query for a scholarly paper search.")
    priority: Literal["core", "supporting"]

    @field_validator("text", "rationale", "web_query", "academic_query")
    @classmethod
    def single_line(cls, value: str) -> str:
        return " ".join(value.split())


class CoveragePlan(BaseModel):
    analysis: QuestionAnalysis
    subquestions: list[PlannedSubquestion]


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
# PoolStatus, CitationOutcome, ClaimStatus and Placement are stored vocabularies
# (stage 29, S2), so they live in `app.core.constants` and are re-exported here
# unchanged: the database layer must not import this package.


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
    "omission_listed",
    "omission_recorded",
    "partial_not_narrowed",
    "superficial_hedge",
    "section_restored",
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
