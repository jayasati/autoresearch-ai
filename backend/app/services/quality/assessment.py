"""
Claim status, subquestion assessment and conflict context — design stages 7–9,
deterministic parts only (docs/REPORT_QUALITY_PIPELINE.md §2–§3).

Pure aggregation: inputs are verdicts and outcomes that later stages produce and
store; nothing here calls a model or reads the database.

Opposing evidence is treated as a **possible scientific disagreement**, never as
proof that a claim is false (approved decision): a contradicting passage makes a
claim *contested*, and the conflict framing says exactly that.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.services.quality.models import (
    CitationOutcome,
    ClaimStatus,
    ConflictContext,
    PoolStatus,
    SourceProfile,
    SubquestionAssessment,
)

_POOL_SUPPORT = {PoolStatus.SUPPORTED, PoolStatus.PARTIALLY_SUPPORTED}
_POOL_INSUFFICIENT = {PoolStatus.UNSUPPORTED, PoolStatus.NOT_ENOUGH_EVIDENCE}
_CITED_SUPPORT = {CitationOutcome.SUPPORTS, CitationOutcome.PARTIALLY_SUPPORTS}


def classify_claim(pool: PoolStatus, citations: Iterable[CitationOutcome]) -> ClaimStatus:
    """The design §3 table, in precedence order.

    1. no pool verdict                      → not evaluated
    2. pool contradicted / a cited passage contradicts → contested
    3. pool unsupported / not enough evidence → insufficient evidence
    4. pool (partially) supported and a cited passage (partially) supports
                                            → supported by the cited source
    5. … but a cited passage was not evaluated → not evaluated
    6. … otherwise (no cited passage supports, or nothing was cited)
                                            → citation not confirmed

    An `invalid_reference` never counts as support.
    """
    outcomes = list(citations)
    if pool is PoolStatus.NOT_EVALUATED:
        return ClaimStatus.NOT_EVALUATED
    if pool is PoolStatus.CONTRADICTED or CitationOutcome.CONTRADICTS in outcomes:
        return ClaimStatus.CONTESTED
    if pool in _POOL_INSUFFICIENT:
        return ClaimStatus.INSUFFICIENT_EVIDENCE
    if any(outcome in _CITED_SUPPORT for outcome in outcomes):
        return ClaimStatus.SUPPORTED_BY_CITED_SOURCE
    if CitationOutcome.NOT_EVALUATED in outcomes:
        return ClaimStatus.NOT_EVALUATED
    return ClaimStatus.CITATION_NOT_CONFIRMED


@dataclass(frozen=True)
class SubquestionInput:
    alias: str  # "Q2"
    is_core: bool
    passages: int
    academic_passages: int
    retrieval_failed: bool = False  # every provider call for it failed


def assess_subquestions(
    subquestions: Sequence[SubquestionInput],
    claim_statuses: Mapping[str, Sequence[ClaimStatus]],  # subquestion alias → its claims
) -> list[SubquestionAssessment]:
    """One assessment per planned subquestion.

    - evidence_gap: no passage was selected for it (and why: no evidence, or the
      providers failed) — listed, never hidden.
    - contested: at least one of its claims is contested.
    - well_supported: at least one claim is supported by its cited source, and such
      claims are at least half of its claims.
    - mixed: everything else, including evidence that the report never used
      (`addressed=False`).
    """
    results = []
    for subquestion in subquestions:
        statuses = list(claim_statuses.get(subquestion.alias, ()))
        counts = Counter(status.value for status in statuses)
        supported = counts.get(ClaimStatus.SUPPORTED_BY_CITED_SOURCE.value, 0)
        if subquestion.passages == 0:
            status = "evidence_gap"
            gap = "provider_failure" if subquestion.retrieval_failed else "no_evidence"
        elif counts.get(ClaimStatus.CONTESTED.value, 0):
            status, gap = "contested", None
        elif supported and supported * 2 >= len(statuses):
            status, gap = "well_supported", None
        else:
            status, gap = "mixed", None
        results.append(
            SubquestionAssessment(
                alias=subquestion.alias,
                is_core=subquestion.is_core,
                status=status,  # type: ignore[arg-type]
                addressed=bool(statuses),
                passages=subquestion.passages,
                academic_passages=subquestion.academic_passages,
                claims=len(statuses),
                statuses=dict(counts),
                gap_reason=gap,  # type: ignore[arg-type]
            )
        )
    return results


CONFLICT_FRAMING = (
    "The sources disagree on this point. This may reflect a genuine scientific "
    "disagreement — for example different populations, periods or methods — and does "
    "not by itself show that either statement is false."
)


def conflict_context(
    conflict_type: str,
    description: str,
    supporting: SourceProfile | None,
    contradicting: SourceProfile | None,
) -> ConflictContext:
    """A stored conflict, with each side's provider-reported metadata attached and a
    fixed framing. No cause is inferred: the description is the verifier's factual
    statement of what each passage says, and the framing names possibilities only."""
    return ConflictContext(
        conflict_type=conflict_type,
        description=description,
        side_supporting=supporting,
        side_contradicting=contradicting,
        framing=CONFLICT_FRAMING,
    )
