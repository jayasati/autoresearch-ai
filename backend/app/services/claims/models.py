"""
The shape the extraction model must fill, validated by Pydantic.

These are the structured-output schemas handed to `generate_structured_output`:
the provider is asked for strict JSON matching them, and the content is validated
against them before anything reaches storage. Every field is required (no defaults)
because OpenAI's strict mode wants every property present — absence is expressed
with null, not omission.

The validators normalise rather than reject where the intent is unambiguous: a
model writing `[S1]` or `s1` for a label means S1, and failing the whole extraction
over bracket style would retry a call that already answered.
"""

from pydantic import BaseModel, Field, field_validator

from app.core.constants import ClaimImportance, ClaimType

#: More claims than this in one report means the extractor is splitting hairs, and
#: each claim costs a verification call later. Enforced here, not asked of the model.
MAX_CLAIMS_PER_REPORT = 50

_LABEL_CHARS = "[] \t"


class ExtractedClaimModel(BaseModel):
    """One atomic, individually checkable assertion found in the report."""

    claim_text: str = Field(
        min_length=8,
        description=(
            "The assertion, as one self-contained sentence. Atomic: if the report "
            "sentence asserts two things, it becomes two claims."
        ),
    )
    claim_type: ClaimType = Field(
        description=(
            "factual | numerical | comparative | methodological | causal | "
            "historical | conclusion"
        )
    )
    importance: ClaimImportance = Field(
        description="How central the claim is to answering the topic: high, medium or low."
    )
    section: str | None = Field(
        description=(
            "The ## heading of the report section the claim appears under, exactly "
            "as written; null when it appears before any section heading."
        )
    )
    citation_labels: list[str] = Field(
        description=(
            "The bracketed markers cited for this claim in the report, e.g. "
            '["S1", "S3"]. Only markers that actually appear with this claim; '
            "empty when the sentence cites nothing."
        )
    )
    requires_citation: bool = Field(
        description=(
            "False only for definitions and framing that assert nothing checkable; "
            "true for everything else, whether or not the report cited anything."
        )
    )

    @field_validator("claim_text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("section")
    @classmethod
    def strip_heading_markers(cls, value: str | None) -> str | None:
        """Models echo the markdown (`## Findings`); stored headings are bare.

        Observed live on the first real extraction: every claim's section came back
        with the `## ` prefix, matched nothing, and silently mapped to no section.
        """
        if value is None:
            return None
        cleaned = value.strip().lstrip("#").strip()
        return cleaned or None

    @field_validator("citation_labels")
    @classmethod
    def normalise_labels(cls, labels: list[str]) -> list[str]:
        """`[S1]`, `s1` and `S1` are the same marker; dedupe preserving order."""
        cleaned = [label.strip(_LABEL_CHARS).upper() for label in labels]
        return list(dict.fromkeys(label for label in cleaned if label))

    def as_record(self) -> dict:
        """The dict shape `ResearchService.record_claims` stores."""
        return {
            "text": self.claim_text,
            "claim_type": self.claim_type,
            "importance": self.importance,
            "section": self.section,
            "citation_labels": self.citation_labels,
            "requires_citation": self.requires_citation,
        }


class ClaimExtractionModel(BaseModel):
    """The whole extraction: every factual claim in the report, in reading order."""

    claims: list[ExtractedClaimModel] = Field(
        description="Every extractable claim, in the order it appears in the report."
    )
