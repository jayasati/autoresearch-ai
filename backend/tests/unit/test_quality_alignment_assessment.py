"""Claim → sentence alignment, claim status, subquestion and conflict assessment."""

import pytest

from app.services.quality.alignment import ClaimToAlign, DraftSentence, align_claims
from app.services.quality.assessment import (
    CONFLICT_FRAMING,
    SubquestionInput,
    assess_subquestions,
    classify_claim,
    conflict_context,
)
from app.services.quality.models import CitationOutcome, ClaimStatus, PoolStatus, SourceProfile

OUT = CitationOutcome


def bag_embed(texts):
    """Deterministic fake embedder: a bag of words over the batch's vocabulary."""
    vocab = sorted({w for t in texts for w in t.lower().replace(".", "").split()})
    return [[float(t.lower().replace(".", "").split().count(w)) for w in vocab] for t in texts]


def sentence(index, text, *markers):
    return DraftSentence(index, text, frozenset(markers))


class TestAlignment:
    sentences = [
        sentence(0, "Coffee intake may lower diabetes risk by 15% in adults.", "S1"),
        sentence(1, "Tea intake was not associated with stroke.", "S2"),
        sentence(2, "Coffee intake may lower stroke risk.", "S1", "S2"),
    ]

    def align(self, claim, margin=0.05, sentences=None):
        return align_claims([claim], sentences or self.sentences, bag_embed, min_margin=margin)[
            claim.alias
        ]

    def test_markers_restrict_candidates_and_cosine_picks_the_best(self):
        result = self.align(
            ClaimToAlign("C1", "Coffee intake may lower diabetes risk by 15%.", frozenset({"S1"}))
        )
        assert result.status == "aligned" and result.sentence_index == 0
        assert result.reason == "clear_best" and result.margin > 0

    def test_single_candidate_is_accepted_without_a_margin(self):
        result = self.align(ClaimToAlign("C2", "Stroke risk was lower.", frozenset({"S1", "S2"})))
        assert (result.status, result.sentence_index, result.reason) == (
            "aligned",
            2,
            "single_candidate",
        )
        assert result.margin is None

    def test_no_sentence_carries_the_markers(self):
        result = self.align(ClaimToAlign("C3", "Anything.", frozenset({"S9"})))
        assert (result.status, result.reason) == ("unaligned", "no_candidate")

    def test_too_close_to_call_is_never_guessed(self):
        twins = [
            sentence(0, "Coffee may lower risk.", "S1"),
            sentence(1, "Coffee may lower risk.", "S1"),
        ]
        result = self.align(
            ClaimToAlign("C4", "Coffee may lower risk.", frozenset({"S1"})), sentences=twins
        )
        assert (result.status, result.reason) == ("unaligned", "ambiguous")
        assert result.sentence_index is None

    def test_no_sentences_and_no_claims(self):
        claim = ClaimToAlign("C5", "x", frozenset())
        assert align_claims([claim], [], bag_embed, min_margin=0.05)["C5"].reason == "no_candidate"
        assert align_claims([], self.sentences, bag_embed, min_margin=0.05) == {}


@pytest.mark.parametrize(
    ("pool", "outcomes", "expected"),
    [
        (PoolStatus.NOT_EVALUATED, [OUT.SUPPORTS], ClaimStatus.NOT_EVALUATED),
        (PoolStatus.CONTRADICTED, [], ClaimStatus.CONTESTED),
        (PoolStatus.SUPPORTED, [OUT.SUPPORTS, OUT.CONTRADICTS], ClaimStatus.CONTESTED),
        (PoolStatus.UNSUPPORTED, [OUT.SUPPORTS], ClaimStatus.INSUFFICIENT_EVIDENCE),
        (PoolStatus.NOT_ENOUGH_EVIDENCE, [], ClaimStatus.INSUFFICIENT_EVIDENCE),
        (
            PoolStatus.SUPPORTED,
            [OUT.DOES_NOT_ESTABLISH, OUT.SUPPORTS],
            ClaimStatus.SUPPORTED_BY_CITED_SOURCE,
        ),
        (
            PoolStatus.PARTIALLY_SUPPORTED,
            [OUT.PARTIALLY_SUPPORTS],
            ClaimStatus.SUPPORTED_BY_CITED_SOURCE,
        ),
        (PoolStatus.SUPPORTED, [OUT.NOT_EVALUATED], ClaimStatus.NOT_EVALUATED),
        (PoolStatus.SUPPORTED, [OUT.DOES_NOT_ESTABLISH], ClaimStatus.CITATION_NOT_CONFIRMED),
        (PoolStatus.SUPPORTED, [OUT.INVALID_REFERENCE], ClaimStatus.CITATION_NOT_CONFIRMED),
        (PoolStatus.SUPPORTED, [], ClaimStatus.CITATION_NOT_CONFIRMED),
    ],
)
def test_claim_status_table(pool, outcomes, expected):
    assert classify_claim(pool, outcomes) is expected


class TestSubquestions:
    def test_each_status(self):
        S = ClaimStatus
        result = {
            a.alias: a
            for a in assess_subquestions(
                [
                    SubquestionInput("Q1", True, passages=0, academic_passages=0),
                    SubquestionInput(
                        "Q2", True, passages=0, academic_passages=0, retrieval_failed=True
                    ),
                    SubquestionInput("Q3", True, passages=3, academic_passages=1),
                    SubquestionInput("Q4", False, passages=2, academic_passages=0),
                    SubquestionInput("Q5", True, passages=2, academic_passages=2),
                    SubquestionInput("Q6", True, passages=2, academic_passages=0),
                ],
                {
                    "Q3": [S.SUPPORTED_BY_CITED_SOURCE, S.CONTESTED],
                    "Q4": [S.SUPPORTED_BY_CITED_SOURCE, S.INSUFFICIENT_EVIDENCE],
                    "Q5": [S.SUPPORTED_BY_CITED_SOURCE, S.NOT_EVALUATED, S.INSUFFICIENT_EVIDENCE],
                },
            )
        }
        assert (result["Q1"].status, result["Q1"].gap_reason) == ("evidence_gap", "no_evidence")
        assert result["Q2"].gap_reason == "provider_failure"
        assert result["Q3"].status == "contested"
        assert result["Q4"].status == "well_supported" and result["Q4"].addressed
        assert result["Q5"].status == "mixed"
        assert result["Q6"].status == "mixed" and not result["Q6"].addressed
        assert result["Q5"].academic_passages == 2 and result["Q5"].claims == 3


def test_conflict_context_is_framed_as_a_possible_disagreement_never_a_cause():
    side = SourceProfile("S2", "academic", 2021, "BMJ", "abstract")
    context = conflict_context("numeric_disagreement", "P1 reports 8%; P3 reports 12%.", side, None)
    assert context.framing == CONFLICT_FRAMING
    assert "does not by itself show that either statement is false" in context.framing
    assert (
        context.description == "P1 reports 8%; P3 reports 12%."
        and context.side_contradicting is None
    )
