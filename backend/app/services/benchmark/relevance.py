"""
Relevance labels for the retrieval metrics, judged against the dataset's ground truth.

Precision@K, Recall@K and nDCG@K need per-chunk relevance labels, and against the
live web no human-labelled corpus exists. The labels therefore come from a fixed
LLM judge comparing each retrieved chunk against the item's `expected_evidence`
statements — the ground truth the dataset actually has — under one versioned prompt,
identical for every arm. The retrieval metrics are computed **only for items whose
`expected_evidence` is non-empty** ("where ground truth exists"); for the rest they
are None, not zero.

The judge decides containment ("does this passage establish this statement"), the
narrowest judgement an LLM can be given here. It never sees which arm produced the
chunks, cannot invent chunks or statements (it answers over indices), and its raw
output is stored in results.json so every label can be audited.
"""

import logging

from pydantic import BaseModel, Field

from app.services.llm import LLMResponseInvalid, LLMService

logger = logging.getLogger(__name__)

RELEVANCE_JUDGE_VERSION = "relevance-judge-v1"

RELEVANCE_SYSTEM = """\
You judge whether retrieved passages establish expected evidence statements.

For every (passage, statement) pair where the passage genuinely states or directly
establishes the statement's content, report the pair. Topical overlap is not enough:
the passage must actually say it. Report nothing else — no scores, no explanations.
Judge only from the text given.
"""


class RelevantPair(BaseModel):
    passage_index: int = Field(ge=0, description="0-based index of the passage.")
    statement_index: int = Field(ge=0, description="0-based index of the statement.")


class RelevanceJudgement(BaseModel):
    """Every (passage, statement) pair the judge found established."""

    pairs: list[RelevantPair]


def build_relevance_prompt(statements: list[str], passages: list[str]) -> str:
    statement_block = "\n".join(f"S{i}: {text}" for i, text in enumerate(statements))
    passage_block = "\n\n".join(f"P{i}: {text}" for i, text in enumerate(passages))
    return (
        f"Expected evidence statements:\n{statement_block}\n\n"
        f"Retrieved passages:\n\n{passage_block}\n\n"
        "Report every pair where a passage establishes a statement, as "
        "(passage_index, statement_index)."
    )


async def judge_relevance(
    llm: LLMService, statements: list[str], passages: list[str]
) -> tuple[list[bool], set[int], list[dict]] | None:
    """Labels for the retrieval metrics, or None when judging failed.

    Returns `(chunk_is_relevant, covered_statement_indices, raw_pairs)`:
    a chunk is relevant iff it establishes at least one expected statement, and a
    statement is covered iff at least one chunk establishes it. Out-of-range indices
    from the judge are dropped.
    """
    if not statements or not passages:
        return None
    prompt = build_relevance_prompt(statements, passages)
    try:
        response = await llm.generate_structured_output(
            prompt, RelevanceJudgement, system=RELEVANCE_SYSTEM
        )
    except LLMResponseInvalid:
        try:
            response = await llm.generate_structured_output(
                prompt + "\n\nReturn ONLY the structured object.",
                RelevanceJudgement,
                system=RELEVANCE_SYSTEM,
            )
        except LLMResponseInvalid:
            logger.warning("Relevance judging failed twice; retrieval metrics withheld")
            return None

    relevant = [False] * len(passages)
    covered: set[int] = set()
    raw_pairs: list[dict] = []
    for pair in response.data.pairs:
        if pair.passage_index < len(passages) and pair.statement_index < len(statements):
            relevant[pair.passage_index] = True
            covered.add(pair.statement_index)
            raw_pairs.append(
                {"passage_index": pair.passage_index, "statement_index": pair.statement_index}
            )
    return relevant, covered, raw_pairs
