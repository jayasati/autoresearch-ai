"""
Offline evidence-selection experiment over an existing development run's chunks.

    backend/.venv/Scripts/python.exe scripts/experiment_selection.py

Replays each semantic cell's persisted chunk pool from the stage-19 development
run and compares top-k selection strategies, judged by the SAME versioned
relevance judge the benchmark uses (relevance-judge-v1, over each item's
expected_evidence). **This makes LLM judge calls** (one per distinct selection
per cell, identical selections judged once), which is why it is a deliberate
script, not a test.

Strategies:
    topk        plain cosine top-k against the question (the current pipeline)
    source_cap  cosine order, at most 2 chunks per source, backfilled if short
    mmr         maximal marginal relevance, lambda=0.7

Channel interleaving is NOT evaluated here: the stage-19 pools contain zero
academic chunks (the starvation the query keywordization fixes), so there is
nothing to interleave — it needs a post-fix run to be measurable at all.

Reads the database and the committed results; writes ONLY a new artifact under
`data/benchmarks/results/retrieval-experiments/`. The frozen test set is not
touched. Relevance labels come from the established judge, never invented.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.benchmark import metrics as formulas  # noqa: E402
from app.services.benchmark.relevance import (  # noqa: E402
    RELEVANCE_JUDGE_VERSION,
    judge_relevance,
)
from app.services.llm import LLMService  # noqa: E402
from app.services.research_service import ResearchService  # noqa: E402
from app.services.vectorstore import get_default_embedder  # noqa: E402

SOURCE_DIR = ROOT / "data/benchmarks/results/development/20261013T090058Z"
OUT_DIR = ROOT / "data/benchmarks/results/retrieval-experiments"
K = 4
MMR_LAMBDA = 0.7
SOURCE_CAP = 2


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # embeddings are normalised


def rank_by_similarity(query_vec, chunk_vecs) -> list[int]:
    sims = [cosine(query_vec, vec) for vec in chunk_vecs]
    return sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)


def select_topk(order, chunks, k):
    return order[:k]


def select_source_cap(order, chunks, k, cap=SOURCE_CAP):
    taken: list[int] = []
    per_source: dict[str, int] = {}
    for index in order:
        fp = chunks[index].source_fingerprint
        if per_source.get(fp, 0) < cap:
            taken.append(index)
            per_source[fp] = per_source.get(fp, 0) + 1
        if len(taken) == k:
            return taken
    for index in order:  # backfill: a short pool must not shrink the budget
        if index not in taken:
            taken.append(index)
        if len(taken) == k:
            break
    return taken


def select_mmr(order, chunks, k, query_vec, chunk_vecs, lam=MMR_LAMBDA):
    candidates = list(order)
    taken: list[int] = []
    while candidates and len(taken) < k:
        def score(i):
            relevance = cosine(query_vec, chunk_vecs[i])
            redundancy = max(
                (cosine(chunk_vecs[i], chunk_vecs[j]) for j in taken), default=0.0
            )
            return lam * relevance - (1 - lam) * redundancy

        best = max(candidates, key=score)
        taken.append(best)
        candidates.remove(best)
    return taken


async def main() -> int:
    payload = json.loads((SOURCE_DIR / "results.json").read_text(encoding="utf-8"))
    dev = json.loads(
        (ROOT / "data/benchmarks/development.json").read_text(encoding="utf-8")
    )
    items = {i["question_id"]: i for i in dev["items"]}
    cells = [
        r for r in payload["rows"]
        if r["arm"] in ("search_grounded", "full_autoresearch_verified")
        and not r.get("error") and items[r["question_id"]].get("expected_evidence")
    ]

    research = ResearchService()
    embedder = get_default_embedder()
    llm = LLMService()
    records, judge_calls = [], 0
    try:
        for cell in cells:
            item = items[cell["question_id"]]
            chunks = research.persisted_chunks_for_run(uuid.UUID(cell["research_id"]))
            if not chunks:
                continue
            chunk_vecs = embedder.embed_documents([c.text for c in chunks])
            query_vec = embedder.embed_query(item["question"])
            order = rank_by_similarity(query_vec, chunk_vecs)

            selections = {
                "topk": select_topk(order, chunks, K),
                "source_cap": select_source_cap(order, chunks, K),
                "mmr": select_mmr(order, chunks, K, query_vec, chunk_vecs),
            }
            judged_cache: dict[tuple, tuple] = {}
            for strategy, picked in selections.items():
                key = tuple(picked)
                if key not in judged_cache:
                    judged = await judge_relevance(
                        llm, item["expected_evidence"], [chunks[i].text for i in picked]
                    )
                    judge_calls += 1
                    judged_cache[key] = judged
                judged = judged_cache[key]
                if judged is None:
                    continue
                relevant, covered, _ = judged
                records.append({
                    "question_id": cell["question_id"],
                    "arm": cell["arm"],
                    "strategy": strategy,
                    "selection": [str(chunks[i].chunk_id) for i in picked],
                    "pool_size": len(chunks),
                    "distinct_sources": len({chunks[i].source_fingerprint for i in picked}),
                    "precision_at_k": formulas.precision_at_k(relevant, K),
                    "recall_at_k": formulas.recall_at_k(
                        len(covered), len(item["expected_evidence"])
                    ),
                    "ndcg_at_k": formulas.ndcg_at_k(relevant, K),
                })
    finally:
        await llm.aclose()

    summary = {}
    for strategy in ("topk", "source_cap", "mmr"):
        rows = [r for r in records if r["strategy"] == strategy]
        summary[strategy] = {
            "n": len(rows),
            "precision_at_4": round(mean(r["precision_at_k"] for r in rows), 4),
            "recall_at_4": round(mean(r["recall_at_k"] for r in rows), 4),
            "ndcg_at_4": round(mean(r["ndcg_at_k"] for r in rows), 4),
            "mean_distinct_sources": round(mean(r["distinct_sources"] for r in rows), 2),
        }
        print(f"{strategy:<12} {summary[strategy]}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"{stamp}-selection-strategies.json"
    out.write_text(json.dumps({
        "source": str(SOURCE_DIR.relative_to(ROOT)),
        "relevance_judge": RELEVANCE_JUDGE_VERSION,
        "k": K, "mmr_lambda": MMR_LAMBDA, "source_cap": SOURCE_CAP,
        "judge_calls": judge_calls,
        "note": (
            "Offline replay over stage-19 persisted chunks (web snippets only - "
            "academic chunks were starved pre-keywordization). Channel "
            "interleaving not evaluable on these pools."
        ),
        "summary": summary,
        "records": records,
    }, indent=2), encoding="utf-8")
    print(f"\nWritten: {out.relative_to(ROOT)} ({judge_calls} judge calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
