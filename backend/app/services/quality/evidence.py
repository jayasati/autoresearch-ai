"""
Retrieval, source profiles and evidence selection — design stages 3–5 (stage 30, S3).

    plan ──per subquestion: web_query → Tavily, academic_query → Semantic Scholar
           (existing services, concurrently)──▶ merged candidates + who found them
         ──provider metadata only──▶ SourceProfile per labelled source
         ──per subquestion, MMR over the chunks of ITS OWN sources──▶ passages
         ──first-selection order──▶ frozen S# per source, E# per passage
         ──regex, per sentence──▶ QuantityMentions per passage (audit input)

Choices recorded for review:

- **Merging mirrors the legacy rule exactly** (`ResearchOrchestrator._retrieve`): two
  candidates are one document when they share any identity key, and the one with
  more text wins. It is mirrored, not imported, so the legacy file stays untouched; a
  test feeds both the same outcomes and asserts the same result.
- **A subquestion selects only from sources its own retrieval found.** That is what
  makes an evidence gap mean something: a subquestion whose searches found nothing
  (or failed) gets zero passages and is a listed gap, instead of borrowing passages
  retrieved for another subquestion. A source found by several subquestions serves
  each of them; `research_source.subquestion_id` records the first (decision 8) and
  `found_by` keeps the full mapping.
- **Selection is MMR with the legacy lambda (0.7)** over the subquestion's chunks,
  computed with the injected embedder — no new threshold. The passage budget is split
  evenly; a remainder goes to core subquestions first.
- **Source quality is not scored and does not affect ranking** (approved): profiles
  carry provider-reported fields only.
"""

import asyncio
import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.services.processing import TextChunk
from app.services.quality.models import (
    AliasTable,
    PassageRef,
    QuantityMention,
    SourceProfile,
)
from app.services.quality.planner import Subquestion
from app.services.quality.quantities import cosine, extract_quantities
from app.services.retrieval import (
    AcademicCandidateSource,
    CandidateSource,
    RetrievalError,
)

Candidate = CandidateSource | AcademicCandidateSource

#: The legacy `VectorStore.search_mmr` default, reused rather than re-tuned.
MMR_LAMBDA = 0.7


# --- stage 3: retrieval per subquestion ---------------------------------------------


@dataclass(frozen=True)
class RetrievalFailure:
    subquestion: str
    provider: str
    code: str
    message: str


@dataclass
class RetrievalOutcome:
    candidates: list[Candidate]
    found_by: dict[str, list[str]]  # candidate fingerprint → subquestion aliases, in order
    failures: list[RetrievalFailure]
    failed_subquestions: set[str]  # every provider call for it failed
    duplicates_merged: int = 0


def merge_candidates(
    outcomes: Sequence[tuple[str, Sequence[Candidate]]],
) -> tuple[list[Candidate], dict[str, list[str]], int]:
    """Deduplicate on ANY shared identity key; on a collision the candidate with more
    text wins. The same rule, in the same order, as the legacy pipeline's `_retrieve`.

    `outcomes` is `(subquestion alias, candidates)` in retrieval order. Returns the
    kept candidates, which subquestions found each (by the kept candidate's
    fingerprint), and how many duplicates were merged.
    """
    candidates: list[Candidate] = []
    owners: list[list[str]] = []
    key_owner: dict[str, int] = {}
    merged = 0
    for alias, found in outcomes:
        for candidate in found:
            keys = candidate.identity_keys()
            existing = {key_owner[key] for key in keys if key in key_owner}
            if not existing:
                index = len(candidates)
                candidates.append(candidate)
                owners.append([])
            else:
                merged += 1
                index = min(existing)
                if len(existing) == 1 and candidate.text_length() > candidates[index].text_length():
                    candidates[index] = candidate
            if alias not in owners[index]:
                owners[index].append(alias)
            for key in keys:
                key_owner[key] = index
    found_by = {candidate.fingerprint: owners[i] for i, candidate in enumerate(candidates)}
    return candidates, found_by, merged


def per_subquestion_results(total: int, subquestions: int) -> int:
    """A depth's legacy result count, shared across subquestions (at least 2 each)."""
    return max(2, math.ceil(total / max(1, subquestions)))


async def retrieve(
    subquestions: Sequence[Subquestion],
    web,
    academic,
    *,
    web_results: int,
    academic_results: int,
) -> RetrievalOutcome:
    """Search every subquestion on both providers, concurrently.

    A provider failure is recorded per call, never hidden; a subquestion whose calls
    all failed is marked. Only when EVERY call failed does retrieval fail — the legacy
    policy, since a retrieval mode with no retrieval is not that mode.
    """
    calls: list[tuple[str, str]] = []
    coroutines = []
    for sq in subquestions:
        calls.append((sq.alias, "tavily"))
        coroutines.append(web.search(sq.web_query, max_results=web_results))
        calls.append((sq.alias, "semantic_scholar"))
        coroutines.append(academic.search(sq.academic_query, limit=academic_results))
    results = await asyncio.gather(*coroutines, return_exceptions=True)

    failures: list[RetrievalFailure] = []
    outcomes: list[tuple[str, Sequence[Candidate]]] = []
    failed_calls: dict[str, int] = {}
    for (alias, provider), result in zip(calls, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, RetrievalError):
                raise result  # our bug, not a provider failure
            failures.append(RetrievalFailure(alias, provider, result.code, result.message))
            failed_calls[alias] = failed_calls.get(alias, 0) + 1
            continue
        outcomes.append((alias, result.candidates))
    if len(failures) == len(calls):
        raise next(r for r in results if isinstance(r, RetrievalError))

    candidates, found_by, merged = merge_candidates(outcomes)
    failed = {alias for alias, count in failed_calls.items() if count == 2}
    return RetrievalOutcome(candidates, found_by, failures, failed, merged)


# --- stage 4: source profiles -------------------------------------------------------


def source_profile(label: str, chunk: TextChunk) -> SourceProfile:
    """Provider-reported metadata only; nothing inferred, nothing scored."""
    source = chunk.source
    return SourceProfile(
        label=label,
        source_type=source.source_type.value,
        year=source.year,
        venue=source.venue,
        evidence_depth=chunk.depth.value,
    )


# --- stage 5: selection, labels, quantities -----------------------------------------


def allocate_passages(subquestions: Sequence[Subquestion], total: int) -> dict[str, int]:
    """An even split of the passage budget; the remainder goes to core subquestions
    first, then supporting ones, in plan order."""
    if not subquestions:
        return {}
    base, remainder = divmod(total, len(subquestions))
    allocation = {sq.alias: base for sq in subquestions}
    ordered = [sq for sq in subquestions if sq.is_core] + [
        sq for sq in subquestions if not sq.is_core
    ]
    for sq in ordered[:remainder]:
        allocation[sq.alias] += 1
    return allocation


def mmr_select(
    query_vector: Sequence[float],
    candidates: Sequence[tuple[uuid.UUID, Sequence[float]]],
    k: int,
    *,
    mmr_lambda: float = MMR_LAMBDA,
) -> list[uuid.UUID]:
    """Maximal marginal relevance, as `VectorStore.search_mmr` computes it. Ties keep
    the candidates' given order, so the result is deterministic."""
    remaining = list(range(len(candidates)))
    selected: list[int] = []
    while remaining and len(selected) < k:

        def score(index: int) -> float:
            relevance = cosine(query_vector, candidates[index][1])
            redundancy = max(
                (cosine(candidates[index][1], candidates[j][1]) for j in selected), default=0.0
            )
            return mmr_lambda * relevance - (1 - mmr_lambda) * redundancy

        best = max(remaining, key=lambda i: (score(i), -i))
        selected.append(best)
        remaining.remove(best)
    return [candidates[i][0] for i in selected]


#: A break after . ! or ? before a capital — but not after an initialism ("U.S."),
#: "e.g.", "i.e.", "vs." or "et al.", which would cut one sentence in two.
_SENTENCE_BREAK = re.compile(
    r"(?<=[.!?])(?<![A-Z]\.[A-Z]\.)(?<!e\.g\.)(?<!i\.e\.)(?<!vs\.)(?<!al\.)\s+(?=[A-Z(\[])"
)


def split_sentences(text: str) -> list[str]:
    """A conservative sentence split for the quantity audit (one sentence at a time)."""
    return [part.strip() for part in _SENTENCE_BREAK.split(text) if part.strip()]


@dataclass(frozen=True)
class SubquestionEvidence:
    alias: str
    subquestion_id: uuid.UUID
    is_core: bool
    passages: list[str]  # E# aliases, in selection order
    candidate_chunks: int
    academic_passages: int
    gap_reason: str | None  # "no_evidence" | "provider_failure" | None


@dataclass
class EvidenceSet:
    """Stage 5's output, handed to drafting (S4). Labels are frozen here."""

    aliases: AliasTable
    chunks: dict[str, TextChunk]  # E# → chunk
    labels: dict[str, str]  # source fingerprint → S#
    profiles: dict[str, SourceProfile]  # S# → profile
    per_subquestion: list[SubquestionEvidence]
    quantities: dict[str, list[QuantityMention]]  # E# → mentions
    found_by: dict[str, list[str]]
    failures: list[RetrievalFailure] = field(default_factory=list)

    @property
    def gaps(self) -> list[SubquestionEvidence]:
        return [s for s in self.per_subquestion if s.gap_reason is not None]

    def as_record(self) -> dict:
        """The audit's view: aliases, per-subquestion counts, provenance. No text."""
        return {
            "aliases": self.aliases.as_record(),
            "per_subquestion": [
                {
                    "alias": s.alias,
                    "is_core": s.is_core,
                    "passages": s.passages,
                    "candidate_chunks": s.candidate_chunks,
                    "academic_passages": s.academic_passages,
                    "gap_reason": s.gap_reason,
                }
                for s in self.per_subquestion
            ],
            "found_by": self.found_by,
            "failures": [f.__dict__ for f in self.failures],
            "quantity_mentions": {alias: len(m) for alias, m in self.quantities.items()},
        }


def select_evidence(
    subquestions: Sequence[Subquestion],
    subquestion_ids: dict[str, uuid.UUID],
    chunks: Sequence[TextChunk],
    retrieval: RetrievalOutcome,
    source_ids: dict[str, uuid.UUID],
    embedder,
    *,
    total_passages: int,
    entities: Sequence[str] = (),
) -> EvidenceSet:
    """Choose each subquestion's passages from its own sources and freeze the labels."""
    allocation = allocate_passages(subquestions, total_passages)
    by_chunk = {chunk.chunk_id: chunk for chunk in chunks}
    vectors = (
        dict(
            zip(
                by_chunk, embedder.embed_documents([c.text for c in by_chunk.values()]), strict=True
            )
        )
        if by_chunk
        else {}
    )

    selections: list[tuple[Subquestion, list[uuid.UUID], int]] = []
    for sq in subquestions:
        own = [
            chunk
            for chunk in chunks
            if sq.alias in retrieval.found_by.get(chunk.source_fingerprint, ())
        ]
        chosen = (
            mmr_select(
                embedder.embed_query(sq.text),
                [(chunk.chunk_id, vectors[chunk.chunk_id]) for chunk in own],
                allocation[sq.alias],
            )
            if own
            else []
        )
        selections.append((sq, chosen, len(own)))

    labels: dict[str, str] = {}
    passage_alias: dict[uuid.UUID, str] = {}
    passages: dict[str, PassageRef] = {}
    chosen_chunks: dict[str, TextChunk] = {}
    for _, chosen, _ in selections:
        for chunk_id in chosen:
            chunk = by_chunk[chunk_id]
            label = labels.setdefault(chunk.source_fingerprint, f"S{len(labels) + 1}")
            if chunk_id not in passage_alias:
                alias = f"E{len(passage_alias) + 1}"
                passage_alias[chunk_id] = alias
                passages[alias] = PassageRef(chunk_id, label)
                chosen_chunks[alias] = chunk

    per_subquestion = []
    for sq, chosen, candidate_count in selections:
        gap = None
        if not chosen:
            gap = "provider_failure" if sq.alias in retrieval.failed_subquestions else "no_evidence"
        per_subquestion.append(
            SubquestionEvidence(
                alias=sq.alias,
                subquestion_id=subquestion_ids[sq.alias],
                is_core=sq.is_core,
                passages=[passage_alias[c] for c in chosen],
                candidate_chunks=candidate_count,
                academic_passages=sum(
                    1 for c in chosen if by_chunk[c].source.source_type.value == "academic"
                ),
                gap_reason=gap,
            )
        )

    profiles: dict[str, SourceProfile] = {}
    for alias, chunk in chosen_chunks.items():
        label = passages[alias].source_label
        profiles.setdefault(label, source_profile(label, chunk))

    aliases = AliasTable(
        sources={label: source_ids[fingerprint] for fingerprint, label in labels.items()},
        passages=passages,
        subquestions=dict(subquestion_ids),
    )
    quantities = {
        alias: [
            m
            for sentence in split_sentences(chunk.text)
            for m in extract_quantities(sentence, entities)
        ]
        for alias, chunk in chosen_chunks.items()
    }
    return EvidenceSet(
        aliases,
        chosen_chunks,
        labels,
        profiles,
        per_subquestion,
        quantities,
        retrieval.found_by,
        retrieval.failures,
    )
