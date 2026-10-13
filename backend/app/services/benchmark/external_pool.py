"""
The external evidence pool: evaluator-owned ground for the metrics-v2 judgement.

    question ──evaluator retrieval──▶ processed chunks ──cache──▶ pools/<version>/<id>.json
                                                      ──index──▶ eval_<version>_<id>

One pool per (question_id, pool_version). The pool derives from the **question
alone** — no arm's report can shape it — and is faced identically by every arm.

The cache is the source of truth, and it is never silently refreshed: if the cache
file exists, its exact passages are used (and indexed on demand when the vector
collection is missing); retrieval runs only when no cache exists. Changing a pool
means minting a new pool_version, visibly. The cache stores the exact passages with
their source metadata and a canonical content hash, so any result can name — and any
auditor can reopen — precisely the evidence it was judged against.

A pool failure (both providers down, or nothing with usable text) is an explicit
`PoolUnavailable` outcome that the runner records as such; it must never become a
zero in any metric.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings, get_settings
from app.services.processing import DocumentProcessor, TextChunk
from app.services.retrieval import (
    AcademicCandidateSource,
    CandidateSource,
    RetrievalError,
    SemanticScholarService,
    TavilySearchService,
)
from app.services.vectorstore import VectorStore, get_default_embedder

logger = logging.getLogger(__name__)

POOL_VERSION = "pool-v1"
#: Deliberately at least as rich as any arm's own retrieval at quick depth (4+4),
#: so no arm is judged against a pool thinner than what it saw itself.
POOL_WEB_RESULTS = 8
POOL_ACADEMIC_RESULTS = 8


@dataclass(frozen=True)
class ExternalPool:
    question_id: str
    pool_version: str
    content_hash: str
    chunk_count: int
    source_count: int
    collection_name: str
    cache_path: str


class PoolUnavailable(Exception):
    """The pool could not be built. An explicit outcome, not an error to hide:
    the runner records it and the external metrics become None."""

    def __init__(self, question_id: str, reason: str) -> None:
        self.question_id = question_id
        self.reason = reason
        super().__init__(f"External pool for {question_id} unavailable: {reason}")


def pool_cache_path(question_id: str, pool_version: str, settings: Settings) -> Path:
    return Path(settings.BENCHMARK_DATA_DIR) / "pools" / pool_version / f"{question_id}.json"


def pool_collection_name(question_id: str, pool_version: str) -> str:
    safe = question_id.replace("-", "_")
    return f"eval_{pool_version.replace('-', '_')}_{safe}"


def _canonical_hash(chunk_payloads: list[dict]) -> str:
    canonical = json.dumps(chunk_payloads, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def load_or_build_pool(
    question_id: str,
    question: str,
    *,
    pool_version: str = POOL_VERSION,
    settings: Settings | None = None,
    store_factory=None,
    web_search: TavilySearchService | None = None,
    academic_search: SemanticScholarService | None = None,
) -> tuple[ExternalPool, VectorStore]:
    """The pool for this question, from cache when one exists, built once otherwise.

    Returns the pool descriptor and a vector store over its collection. The
    collection is rebuilt from the cached passages whenever it is empty, so the
    cache file alone fully determines what every arm is judged against.
    """
    settings = settings or get_settings()
    cache = pool_cache_path(question_id, pool_version, settings)

    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        # Hash first, validate second: an edited cache must fail as an integrity
        # violation, not as whatever validator the edit happens to trip.
        content_hash = payload["content_hash"]
        recomputed = _canonical_hash(payload["chunks"])
        if recomputed != content_hash:
            raise PoolUnavailable(
                question_id,
                f"cached pool hash mismatch (stored {content_hash[:12]}, "
                f"recomputed {recomputed[:12]}); the cache was edited",
            )
        try:
            chunks = [TextChunk.model_validate(entry) for entry in payload["chunks"]]
        except ValueError as exc:
            raise PoolUnavailable(question_id, f"cached pool failed validation: {exc}") from exc
    else:
        chunks = await _retrieve_pool(question_id, question, settings, web_search, academic_search)
        chunk_payloads = [json.loads(chunk.model_dump_json()) for chunk in chunks]
        content_hash = _canonical_hash(chunk_payloads)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "question_id": question_id,
                    "question": question,
                    "pool_version": pool_version,
                    "created_at": datetime.now(UTC).isoformat(),
                    "web_results_requested": POOL_WEB_RESULTS,
                    "academic_results_requested": POOL_ACADEMIC_RESULTS,
                    "content_hash": content_hash,
                    "chunks": chunk_payloads,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        logger.info(
            "Built external pool %s/%s: %d chunks, hash %s",
            pool_version,
            question_id,
            len(chunks),
            content_hash[:12],
        )

    collection = pool_collection_name(question_id, pool_version)
    store = (
        store_factory(collection)
        if store_factory is not None
        else VectorStore(embedder=get_default_embedder(), collection_name=collection)
    )
    if store.count() == 0 and chunks:
        store.index_chunks(chunks)

    return (
        ExternalPool(
            question_id=question_id,
            pool_version=pool_version,
            content_hash=content_hash,
            chunk_count=len(chunks),
            source_count=len({chunk.source_fingerprint for chunk in chunks}),
            collection_name=collection,
            cache_path=str(cache),
        ),
        store,
    )


async def _retrieve_pool(
    question_id: str,
    question: str,
    settings: Settings,
    web_search: TavilySearchService | None,
    academic_search: SemanticScholarService | None,
) -> list[TextChunk]:
    """Evaluator-owned retrieval: the question is the only input."""
    web = web_search or TavilySearchService(settings=settings)
    academic = academic_search or SemanticScholarService(settings=settings)
    candidates: list[CandidateSource | AcademicCandidateSource] = []
    failures: list[str] = []
    try:
        try:
            web_outcome = await web.search(question, max_results=POOL_WEB_RESULTS)
            candidates.extend(web_outcome.candidates)
        except RetrievalError as exc:
            failures.append(f"tavily: {exc.message}")
        try:
            academic_outcome = await academic.search(question, limit=POOL_ACADEMIC_RESULTS)
            candidates.extend(academic_outcome.candidates)
        except RetrievalError as exc:
            failures.append(f"semantic_scholar: {exc.message}")
    finally:
        if web_search is None:
            await web.aclose()
        if academic_search is None:
            await academic.aclose()

    if not candidates:
        raise PoolUnavailable(
            question_id, "; ".join(failures) or "both providers returned nothing"
        )
    documents = DocumentProcessor().process_many(candidates)
    chunks = [
        chunk for document in documents if not document.is_empty for chunk in document.chunks
    ]
    if not chunks:
        raise PoolUnavailable(question_id, "no retrieved source had usable text")
    return chunks
