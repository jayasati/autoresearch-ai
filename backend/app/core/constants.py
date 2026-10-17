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
    # + academic papers + RAG over retrieved snippets and abstracts. No full-text
    # fetching exists in the pipeline (no document carries EvidenceDepth.FULL_TEXT);
    # nor does this mode verify claims — that is the separate claims/verify/finalize
    # path, which the benchmark's full_autoresearch_verified arm adds on top.
    SEARCH_GROUNDED = "search_grounded"


class PipelineVersion(StrEnum):
    """Which report pipeline produced a run. Orthogonal to `ResearchMode`.

    `legacy` is every run the system has made so far and remains the default.
    `report-quality-v1` is the opt-in pipeline designed in
    `docs/REPORT_QUALITY_PIPELINE.md`; as of stage 27 (S0) it is declared only —
    no request field, route or service selects it, so nothing can run under it.
    """

    LEGACY = "legacy"
    REPORT_QUALITY_V1 = "report-quality-v1"


# --- report-quality-v1 stored vocabularies (stage 29, S2) --------------------------
# Defined here, not in `app.services.quality`, because the database columns use them
# and the model layer must not load the pipeline package (S1 isolation guarantee).


class PoolStatus(StrEnum):
    """verification-v2's verdict for a claim, or that there is none."""

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
    """The five reader-facing claim categories, kept distinct (design §3)."""

    SUPPORTED_BY_CITED_SOURCE = "supported_by_cited_source"
    CITATION_NOT_CONFIRMED = "supported_citation_not_confirmed"
    CONTESTED = "contested"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_EVALUATED = "not_evaluated"


class Placement(StrEnum):
    """Where a final-report sentence ended up (design §5)."""

    BODY = "body"
    QUALIFIED = "qualified"  # body, explicitly qualified
    CITATION_NOT_CONFIRMED = "citation_not_confirmed"
    CONTESTED = "contested"
    UNRESOLVED = "unresolved"
    UNVERIFIED_LIST = "unverified_list"
    NOT_EVALUATED = "not_evaluated"


class NotEvaluatedReason(StrEnum):
    """Why a claim, citation or rewritten sentence was not evaluated — never silent."""

    BUDGET = "budget"
    DEADLINE = "deadline"
    TECHNICAL = "technical"
    CLAIM_CAP = "claim_cap"
    UNALIGNED = "unaligned"
    AWAITING_REVERIFICATION = "awaiting_reverification"


class EstablishedBy(StrEnum):
    """Which procedure established a cited passage's relation to its claim."""

    POOL_VERIFICATION = "pool_verification"  # verification-v2 quoted this exact chunk
    CITATION_CHECK = "citation_check"  # citation-check-v1 over the cited passages


class ReportQualityStatus(StrEnum):
    """A report-quality-v1 finalization's state; the last three are terminal."""

    RUNNING = "running"
    COMPLETE = "complete"
    DEGRADED = "degraded"
    FINALIZATION_FAILED = "finalization_failed"


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
    """The five claim verdicts.

    `NOT_ENOUGH_EVIDENCE` is this project's stored spelling of the spec
    vocabulary's INSUFFICIENT_EVIDENCE. Renaming the stored value would orphan
    the database enum and every frozen benchmark artifact, so the mapping is
    documented here instead of applied.
    """

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    NOT_ENOUGH_EVIDENCE = "not_enough_evidence"


class CitationStatus(StrEnum):
    UNVALIDATED = "unvalidated"  # recorded at extraction; validation has not run yet
    VALID = "valid"              # citation resolves and supports the claim
    BROKEN = "broken"            # URL/DOI does not resolve
    MISATTRIBUTED = "misattributed"  # resolves, but does not support the claim
    FABRICATED = "fabricated"    # no such source exists


class ClaimType(StrEnum):
    """What kind of assertion a claim makes.

    Typed because the verification and metric stages treat them differently: a
    NUMERICAL claim can be checked against a figure, a CAUSAL claim needs much
    stronger evidence than a FACTUAL one, and a CONCLUSION is the report's own
    synthesis rather than something one passage can establish.
    """

    FACTUAL = "factual"              # a verifiable statement of fact
    NUMERICAL = "numerical"          # asserts a quantity, rate, or measurement
    COMPARATIVE = "comparative"      # compares two or more things
    METHODOLOGICAL = "methodological"  # about how a study or process was conducted
    CAUSAL = "causal"                # asserts that one thing causes another
    HISTORICAL = "historical"        # about past events or developments
    CONCLUSION = "conclusion"        # the report's own summarising judgement


class ClaimImportance(StrEnum):
    """How central a claim is to the report's answer.

    Three levels, not a score: an LLM assigns these far more consistently than a
    number, and the metrics only need "weight the central claims more".
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


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

    METADATA = "metadata"        # title and bibliographic data only -- no text at all
    SNIPPET = "snippet"          # a search-result snippet only
    ABSTRACT = "abstract"        # paper abstract, no full text
    FULL_TEXT = "full_text"      # fetched and extracted body text

    @property
    def rank(self) -> int:
        """How much text we have, 0 (none) to 3 (the whole document).

        Ordered so metrics can ask "was this claim grounded in more than an
        abstract" without hard-coding the comparison in several places.
        """
        return {
            EvidenceDepth.METADATA: 0,
            EvidenceDepth.SNIPPET: 1,
            EvidenceDepth.ABSTRACT: 2,
            EvidenceDepth.FULL_TEXT: 3,
        }[self]

    @property
    def has_text(self) -> bool:
        """False for METADATA: there is nothing to quote, so nothing to verify against.

        A paper whose abstract the provider did not return is still a real source
        worth recording -- dropping it would bias retrieval toward whatever happens
        to have an abstract indexed -- but it cannot support a claim until something
        fetches its text. Saying so is the honest alternative to labelling it
        "abstract" and hoping nobody checks.
        """
        return self is not EvidenceDepth.METADATA


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
