"""
The three traceability chains, as explicit response models.

These exist because "the data is traceable" is a claim that should be checkable,
not a property you have to reconstruct by writing the right joins. Each model below
is one of the required chains, materialised end to end, so the API can hand a
grader or a user the whole provenance of a single assertion in one response.

    EvidenceTrace      Research -> Report -> Claim -> Citation -> Source -> chunk
    VerificationTrace  Claim -> Evidence -> VerificationResult
    EvaluationTrace    Research run -> Configuration -> Metrics
"""

import uuid

from pydantic import Field, model_validator

from app.core.constants import (
    CitationStatus,
    EvidenceDepth,
    EvidenceRelation,
    ResearchMode,
    VerificationVerdict,
)
from app.schemas.base import ReadModel
from app.schemas.evaluation import EvaluationResultRead
from app.schemas.research import RunConfigurationRead


class ChunkPointer(ReadModel):
    """The end of the chain: specific characters in a specific document.

    The offsets are the point. "Supported by this paper" is an assertion;
    "supported by characters 1840-2012 of this fetched text, which say the
    following" is evidence.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    start_char: int
    end_char: int
    text: str

    @property
    def span(self) -> str:
        return f"[{self.start_char}:{self.end_char}]"


class SourcePointer(ReadModel):
    source_id: uuid.UUID
    title: str | None
    url: str | None
    doi: str | None
    evidence_depth: EvidenceDepth | None = None


class CitationTrace(ReadModel):
    """One citation, and what it actually resolved to.

    `source` is null for a fabricated citation, which is the whole reason this is
    modelled as optional rather than assumed present.
    """

    citation_id: uuid.UUID
    marker: str
    status: CitationStatus
    source: SourcePointer | None = None
    supporting_chunk: ChunkPointer | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def fabricated_citations_resolve_to_nothing(self) -> "CitationTrace":
        if self.status is CitationStatus.FABRICATED and self.source is not None:
            raise ValueError("a fabricated citation cannot resolve to a source")
        return self


class EvidenceTrace(ReadModel):
    """Chain 1: Research -> Report -> Claim -> Citation -> Source -> Evidence chunk.

    Every link is an id, so each step can be opened independently rather than taken
    on trust.
    """

    research_run_id: uuid.UUID
    topic: str
    mode: ResearchMode
    report_id: uuid.UUID
    claim_id: uuid.UUID
    claim_text: str
    claim_position: int
    citations: list[CitationTrace] = Field(default_factory=list)
    evidence_chunks: list[ChunkPointer] = Field(default_factory=list)

    @property
    def is_fully_traceable(self) -> bool:
        """True when at least one citation resolves to a source *and* a passage.

        A claim with citations that all failed to validate is not traceable, even
        though it looks cited. This property is what the groundedness metric
        ultimately counts.
        """
        return any(
            c.status is CitationStatus.VALID and c.source and c.supporting_chunk
            for c in self.citations
        )


class EvidenceLink(ReadModel):
    """One passage considered while verifying a claim, and its stance."""

    evidence_id: uuid.UUID
    relation: EvidenceRelation
    similarity: float | None = None
    quoted_text: str | None = None
    chunk: ChunkPointer


class VerificationTrace(ReadModel):
    """Chain 2: Claim -> Evidence -> VerificationResult.

    All the evidence considered is listed, not just the passage that won, and
    `decisive_evidence_id` names which one the verdict rests on. Showing only the
    supporting passage would hide contradicting evidence that was weighed and
    rejected -- and that is precisely what a reader needs in order to disagree.
    """

    claim_id: uuid.UUID
    claim_text: str
    verdict: VerificationVerdict
    confidence: float | None = None
    downgraded: bool = False
    rationale: str | None = None
    evidence: list[EvidenceLink] = Field(default_factory=list)
    decisive_evidence_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def decisive_evidence_must_be_listed(self) -> "VerificationTrace":
        """The passage a verdict rests on has to appear among the evidence.

        Otherwise the trace points at something the response does not contain, and
        the chain is broken exactly where it matters most.
        """
        if self.decisive_evidence_id is None:
            return self
        if self.decisive_evidence_id not in {e.evidence_id for e in self.evidence}:
            raise ValueError(
                "decisive_evidence_id must refer to one of the listed evidence items"
            )
        return self

    @property
    def supporting(self) -> list[EvidenceLink]:
        return [e for e in self.evidence if e.relation is EvidenceRelation.SUPPORTS]

    @property
    def contradicting(self) -> list[EvidenceLink]:
        return [e for e in self.evidence if e.relation is EvidenceRelation.CONTRADICTS]


class EvaluationTrace(ReadModel):
    """Chain 3: Research run -> Configuration -> Metrics.

    The configuration is embedded in full rather than referenced, because a metric
    without the settings that produced it is not interpretable. `fingerprint` on
    that configuration is what makes two runs' numbers legitimately comparable.
    """

    research_run_id: uuid.UUID
    topic: str
    configuration: RunConfigurationRead
    metrics: list[EvaluationResultRead] = Field(default_factory=list)
    metrics_version: str | None = None

    @model_validator(mode="after")
    def metrics_share_one_version(self) -> "EvaluationTrace":
        """Mixing metric versions in one trace produces an incoherent total.

        If one number was computed under v1 of the definitions and another under
        v2, they are not measurements of the same thing and must not be presented
        as one set.
        """
        versions = {m.metrics_version for m in self.metrics}
        if len(versions) > 1:
            raise ValueError(
                f"metrics in one trace must share a metrics_version, got {sorted(versions)}"
            )
        if versions:
            object.__setattr__(self, "metrics_version", versions.pop())
        return self

    @property
    def by_key(self) -> dict[str, float]:
        return {m.metric_key.value: m.value for m in self.metrics}
