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
