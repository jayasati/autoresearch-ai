"""
Claim → draft-sentence alignment (docs/REPORT_QUALITY_PIPELINE.md §3, decision D2).

claim-extraction-v1 stays untouched, so a claim does not say which draft sentence it
came from. This module recovers that deterministically, and refuses to guess:

1. Candidates: sentences whose rendered markers contain all of the claim's
   `citation_labels` (a claim citing nothing may come from any sentence).
2. Score: cosine between the claim and each candidate, from an injected embedder
   (the local sentence-transformer in production, a fake in tests).
3. Decision: the best candidate, if it beats the runner-up by at least `min_margin`.
   A single candidate is accepted with no margin to beat. Otherwise the claim is
   `unaligned` and its citation check becomes `not_evaluated(unaligned)` later.

Alignment accuracy is a hypothesis to measure, not an established property.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.services.quality.quantities import cosine

Embed = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True)
class ClaimToAlign:
    alias: str  # "C7"
    text: str
    citation_labels: frozenset[str]  # the S# markers the extractor recorded


@dataclass(frozen=True)
class DraftSentence:
    index: int  # global order in the draft
    text: str
    markers: frozenset[str]  # the S# markers rendered on this sentence


@dataclass(frozen=True)
class Alignment:
    claim_alias: str
    sentence_index: int | None
    score: float | None
    margin: float | None  # None when there was a single candidate
    status: Literal["aligned", "unaligned"]
    reason: Literal["single_candidate", "clear_best", "no_candidate", "ambiguous"]


def align_claims(
    claims: Sequence[ClaimToAlign],
    sentences: Sequence[DraftSentence],
    embed: Embed,
    *,
    min_margin: float,
) -> dict[str, Alignment]:
    """One `Alignment` per claim alias; never a guess."""
    if not claims:
        return {}
    if not sentences:
        return {
            claim.alias: Alignment(claim.alias, None, None, None, "unaligned", "no_candidate")
            for claim in claims
        }
    texts = [claim.text for claim in claims] + [sentence.text for sentence in sentences]
    vectors = embed(texts)
    claim_vectors = vectors[: len(claims)]
    sentence_vectors = vectors[len(claims) :]

    results: dict[str, Alignment] = {}
    for claim, claim_vector in zip(claims, claim_vectors, strict=True):
        candidates = [
            (index, sentence)
            for index, sentence in enumerate(sentences)
            if claim.citation_labels <= sentence.markers
        ]
        if not candidates:
            results[claim.alias] = Alignment(
                claim.alias, None, None, None, "unaligned", "no_candidate"
            )
            continue
        scored = sorted(
            (
                (cosine(claim_vector, sentence_vectors[index]), sentence)
                for index, sentence in candidates
            ),
            key=lambda pair: (-pair[0], pair[1].index),
        )
        best_score, best = scored[0]
        if len(scored) == 1:
            results[claim.alias] = Alignment(
                claim.alias, best.index, round(best_score, 6), None, "aligned", "single_candidate"
            )
            continue
        margin = best_score - scored[1][0]
        if margin >= min_margin:
            results[claim.alias] = Alignment(
                claim.alias,
                best.index,
                round(best_score, 6),
                round(margin, 6),
                "aligned",
                "clear_best",
            )
        else:
            results[claim.alias] = Alignment(
                claim.alias, None, round(best_score, 6), round(margin, 6), "unaligned", "ambiguous"
            )
    return results
