"""
The shape the verifier must fill, validated by Pydantic.

Everything here is the verifier's *assertion*; nothing is trusted on arrival. The
verifier code re-checks the two things a model is most tempted to fudge:

- a quote must be a verbatim substring of the passage it is attributed to, or the
  passage's relation is downgraded to neutral;
- a reported conflict must name two passages whose (enforced) relations really are
  one supporting and one contradicting, or it is dropped and warned about.

All fields are required — OpenAI's strict mode wants every property present, and
absence is expressed with null.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.constants import ConflictType, EvidenceRelation, VerificationVerdict


class PassageAssessment(BaseModel):
    """What one evidence passage says about the claim."""

    passage: str = Field(description='The passage label, e.g. "P1".')
    relation: EvidenceRelation = Field(
        description=(
            "supports: the passage states the claim's content. contradicts: it "
            "states the opposite. neutral: related but does not establish either."
        )
    )
    quote: str | None = Field(
        description=(
            "The exact words from THIS passage that support or contradict the "
            "claim, copied verbatim — required for supports/contradicts, null for "
            "neutral. Never paraphrase."
        )
    )

    @field_validator("passage")
    @classmethod
    def normalise_label(cls, value: str) -> str:
        return value.strip("[] \t").upper()


class ConflictReport(BaseModel):
    """Two passages that disagree about the claim.

    The description records WHAT each passage says. Guessing WHY they disagree —
    methodology, bias, age of data — is explicitly forbidden; a cause belongs to a
    human reading both, not to this field.
    """

    kind: Literal["direct", "nuanced"] = Field(
        description=(
            "direct: the passages state incompatible facts. nuanced: they diverge "
            "in scope, period, population or degree rather than flatly conflicting."
        )
    )
    conflict_type: ConflictType = Field(
        description=(
            "numeric_disagreement | directional_disagreement | temporal_staleness "
            "| scope_mismatch"
        )
    )
    supporting_passage: str = Field(description="Label of the passage supporting the claim.")
    contradicting_passage: str = Field(
        description="Label of the passage contradicting the claim."
    )
    description: str = Field(
        min_length=10,
        description=(
            "State what each passage says, e.g. 'P1 reports a 6.5% increase; P3 "
            "reports no significant change.' Do NOT speculate about why."
        ),
    )

    @field_validator("supporting_passage", "contradicting_passage")
    @classmethod
    def normalise_label(cls, value: str) -> str:
        return value.strip("[] \t").upper()


class ClaimVerificationModel(BaseModel):
    """The verdict on one claim, grounded in the passages provided."""

    verdict: VerificationVerdict = Field(
        description=(
            "supported: a passage states the claim. partially_supported: passages "
            "establish part of it. unsupported: nothing relevant found. "
            "contradicted: a passage states the opposite. not_enough_evidence: the "
            "passages are too thin to judge."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0, description="How settled the verdict is.")
    rationale: str = Field(
        min_length=10,
        description=(
            "One or two sentences on what the passages establish. About the "
            "evidence, never about the claim's general plausibility."
        ),
    )
    passages: list[PassageAssessment] = Field(
        description="One assessment per provided passage, in order."
    )
    conflict: ConflictReport | None = Field(
        description=(
            "Present only when one passage supports the claim and another "
            "contradicts it; null otherwise."
        )
    )
