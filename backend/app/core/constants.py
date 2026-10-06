"""
Shared enums and constants.

These names define the vocabulary the whole system agrees on. They are
declared now so that retrieval, evidence, evaluation and the frontend all
speak the same language from stage one.
"""

from enum import StrEnum


class ResearchMode(StrEnum):
    """The three configurations the benchmark will compare."""

    MODEL_ONLY = "model_only"          # no retrieval; parametric knowledge only
    HYBRID = "hybrid"                  # + web search
    SEARCH_GROUNDED = "search_grounded"  # + academic papers + RAG over full text


class SourceType(StrEnum):
    WEB = "web"
    ACADEMIC = "academic"
    MODEL = "model"          # asserted by the model with no external source


class RunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RETRIEVING = "retrieving"
    DRAFTING = "drafting"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


class VerificationVerdict(StrEnum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    NOT_ENOUGH_EVIDENCE = "not_enough_evidence"


class CitationStatus(StrEnum):
    VALID = "valid"              # citation resolves and supports the claim
    BROKEN = "broken"            # URL/DOI does not resolve
    MISATTRIBUTED = "misattributed"  # resolves, but does not support the claim
    FABRICATED = "fabricated"    # no such source exists


class ConflictType(StrEnum):
    NUMERIC_DISAGREEMENT = "numeric_disagreement"
    DIRECTIONAL_DISAGREEMENT = "directional_disagreement"
    TEMPORAL_STALENESS = "temporal_staleness"
    SCOPE_MISMATCH = "scope_mismatch"


class EvidenceRelation(StrEnum):
    """How a retrieved passage stands toward a claim.

    Separate from `VerificationVerdict`: a verdict is the *conclusion* about a
    claim, while this is the role of one individual passage. A claim can have
    supporting and contradicting evidence at once -- that is exactly the input
    conflict detection needs.
    """

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"


class EvidenceDepth(StrEnum):
    """How much of a source we actually read.

    Recorded per source because a claim grounded in full text and a claim
    grounded in an abstract are not equally well supported. Without this, the
    groundedness metrics would overstate the result.
    """

    SNIPPET = "snippet"          # a search-result snippet only
    ABSTRACT = "abstract"        # paper abstract, no full text
    FULL_TEXT = "full_text"      # fetched and extracted body text


class PipelineStage(StrEnum):
    """Which step of a run an LLM call or status transition belongs to."""

    PLANNING = "planning"
    RETRIEVAL = "retrieval"
    SYNTHESIS = "synthesis"
    CRITIQUE = "critique"
    CLAIM_EXTRACTION = "claim_extraction"
    VERIFICATION = "verification"
    CITATION_VALIDATION = "citation_validation"
    CONFLICT_DETECTION = "conflict_detection"


class MetricKey(StrEnum):
    """The metrics the benchmark reports.

    An enum rather than free text: a typo in a metric key would silently split
    one metric into two columns in the comparison table.
    """

    CLAIM_SUPPORT_RATE = "claim_support_rate"
    HALLUCINATION_RATE = "hallucination_rate"
    CITATION_PRECISION = "citation_precision"
    CITATION_RECALL = "citation_recall"
    FABRICATION_RATE = "fabrication_rate"
    COVERAGE = "coverage"
    SOURCE_DIVERSITY = "source_diversity"
    FULL_TEXT_GROUNDING_RATE = "full_text_grounding_rate"
    CONFLICTS_DETECTED = "conflicts_detected"
    TOTAL_TOKENS = "total_tokens"
    COST_USD = "cost_usd"
    WALL_CLOCK_SECONDS = "wall_clock_seconds"

    @property
    def is_ratio(self) -> bool:
        """True when the metric must fall in [0, 1]."""
        return self.value.endswith(("_rate", "_precision", "_recall")) or self in {
            MetricKey.COVERAGE,
            MetricKey.CITATION_PRECISION,
            MetricKey.CITATION_RECALL,
        }
