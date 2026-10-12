"""
The pipeline: topic in, report out, under one of three experimental configurations.

These are **experimental configurations for comparison, not novel techniques** —
each is a standard pattern, and the point of having all three is the benchmark:
holding topic, depth, prompt and model constant while varying only how evidence is
acquired, so the differences that show up are attributable to exactly that.

    MODEL_ONLY        topic → OpenAI → report
                      No retrieval, no sources, no citations. The ungrounded
                      baseline: whatever the model asserts comes from its weights.

    HYBRID            topic → Tavily → OpenAI → report + sources
                      Web snippets, cleaned and chunked, fed as evidence in the
                      provider's rank order ('provider_rank'). No embeddings.

    SEARCH_GROUNDED   topic → Tavily + Semantic Scholar → ChromaDB → evidence
                      → OpenAI → report + sources
                      Both providers, chunks embedded into a per-run collection,
                      evidence chosen by vector similarity ('semantic_top_k').

What is shared — deliberately, for fairness: the depth profiles, the synthesis
system prompt and evidence formatting (for the two evidence modes), the citation
requirement and its retry, the report persistence, the status machine and the
failure policy. What differs is isolated in `_acquire_evidence`, and the response's
`execution_metadata.evidence_selection` names which path ran.

Failure policy (unchanged from stage 10): a run that cannot produce what its mode
promises fails honestly. For the evidence modes that means `NoEvidenceError` when
nothing usable was retrieved and `UncitedReportError` after an uncited retry; every
failure marks the run `failed` with the error's own code. MODEL_ONLY has no evidence
to fail on, and no citation requirement — citation-like markers it emits anyway are
warned about, because they refer to nothing.
"""

import asyncio
import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Coroutine
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.core.config import Settings, get_settings
from app.core.constants import ResearchMode, RunStatus
from app.core.exceptions import AppError
from app.schemas.research import (
    CitedSourceRead,
    ExecutionMetadata,
    ProviderFailure,
    ResearchRequest,
    ResearchStartRequest,
    ResearchStartResponse,
)
from app.services.llm import LLMNotConfigured, LLMResponse, LLMService
from app.services.orchestration.exceptions import NoEvidenceError, UncitedReportError
from app.services.orchestration.prompts import (
    MODEL_ONLY_PROMPT_VERSION,
    MODEL_ONLY_SYSTEM,
    RETRY_REMINDER,
    SYNTHESIS_PROMPT_VERSION,
    SYNTHESIS_SYSTEM,
    EvidencePassage,
    build_model_only_prompt,
    build_synthesis_prompt,
    format_evidence_block,
)
from app.services.processing import DocumentProcessor, SourceMetadata
from app.services.research_service import ResearchService
from app.services.retrieval import (
    AcademicCandidateSource,
    CandidateSource,
    RetrievalError,
    SemanticScholarService,
    TavilySearchService,
)
from app.services.vectorstore import SentenceTransformerEmbedder, VectorStore

logger = logging.getLogger(__name__)

CITATION_PATTERN = re.compile(r"\[(S\d+)\]")

Candidate = CandidateSource | AcademicCandidateSource


@dataclass(frozen=True)
class DepthProfile:
    """What each depth buys. The knobs, in one place, so "deep" means the same
    thing in every run and the benchmark can hold it constant across modes."""

    web_results: int
    academic_results: int
    top_k: int
    max_report_tokens: int

    @property
    def max_sources(self) -> int:
        return self.web_results + self.academic_results


DEPTH_PROFILES: dict[str, DepthProfile] = {
    "quick": DepthProfile(web_results=4, academic_results=4, top_k=4, max_report_tokens=900),
    "medium": DepthProfile(web_results=8, academic_results=8, top_k=6, max_report_tokens=1600),
    "deep": DepthProfile(web_results=12, academic_results=15, top_k=10, max_report_tokens=2800),
}


@dataclass
class Evidence:
    """Everything the synthesis and the response need to know about the evidence.

    One shape for all modes, so the code after `_acquire_evidence` cannot care which
    mode produced it — that indifference is what keeps the comparison fair.
    """

    selection: str  # 'semantic_top_k' | 'provider_rank' | 'none'
    blocks: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)  # fingerprint → S#
    meta_by_fp: dict[str, SourceMetadata] = field(default_factory=dict)
    depth_by_fp: dict[str, str] = field(default_factory=dict)
    chunks_used: Counter = field(default_factory=Counter)
    source_ids: dict[str, uuid.UUID] = field(default_factory=dict)

    web_candidates: int = 0
    academic_candidates: int = 0
    documents_processed: int = 0
    documents_with_text: int = 0
    chunks_indexed: int = 0
    chunks_retrieved: int = 0
    embedding_model: str | None = None
    collection: str | None = None

    def add_passage(self, passage: EvidencePassage) -> None:
        fingerprint = passage.source.fingerprint
        label = self.labels.setdefault(fingerprint, f"S{len(self.labels) + 1}")
        self.chunks_used[fingerprint] += 1
        self.meta_by_fp.setdefault(fingerprint, passage.source)
        self.depth_by_fp.setdefault(fingerprint, passage.depth.value)
        self.blocks.append(format_evidence_block(label, passage))
        self.chunks_retrieved += 1


@lru_cache
def _default_embedder() -> SentenceTransformerEmbedder:
    """One embedder per process. The model takes seconds to load and holds no
    per-run state, so reloading it for every request would be pure waste."""
    return SentenceTransformerEmbedder()


class ResearchOrchestrator:
    """Wires retrieval → processing → (vectorstore) → LLM into one run.

    Every collaborator is injectable, so the pipeline's logic is testable with fakes
    while the real wiring stays the default. Collaborators the orchestrator created
    itself are closed by it; injected ones belong to the caller.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        research_service: ResearchService | None = None,
        web_search: TavilySearchService | None = None,
        academic_search: SemanticScholarService | None = None,
        processor: DocumentProcessor | None = None,
        store_factory=None,
        llm: LLMService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.research = research_service or ResearchService(settings=self.settings)
        self.processor = processor or DocumentProcessor()
        self._web = web_search
        self._academic = academic_search
        self._store_factory = store_factory
        self._llm = llm

    # --- entry point -----------------------------------------------------------

    async def run(self, request: ResearchStartRequest) -> ResearchStartResponse:
        """Execute the requested mode for one topic. Synchronous from the caller's
        point of view: returns when the report exists, raises when it cannot."""
        if self.settings.llm_provider is None:
            # Checked before any row or paid call: a run that could never reach
            # synthesis should not spend two providers' quota finding sources.
            raise LLMNotConfigured(details={"setting": "OPENAI_API_KEY"})

        profile = DEPTH_PROFILES[request.depth]
        run = self.research.create_run(
            ResearchRequest(
                topic=request.topic, mode=request.mode, max_sources=profile.max_sources
            )
        )

        try:
            return await self._execute(run.id, request, profile)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "internal_error"
            message = exc.message if isinstance(exc, AppError) else str(exc)[:500]
            try:
                self.research.fail_run(run.id, code=code, message=message)
            except Exception:  # noqa: BLE001 - the original error must not be masked
                logger.exception("Could not record the failure of run %s", run.id)
            raise

    # --- the pipeline ----------------------------------------------------------

    async def _execute(
        self, run_id: uuid.UUID, request: ResearchStartRequest, profile: DepthProfile
    ) -> ResearchStartResponse:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        warnings: list[str] = []
        failures: list[ProviderFailure] = []

        # No planner yet: the topic is the single query, and the status transitions
        # pass through PLANNING because the state machine (correctly) has no shortcut.
        self.research.advance_status(run_id, RunStatus.PLANNING)

        evidence = await self._acquire_evidence(
            run_id, request, profile, timings, failures
        )

        self.research.advance_status(run_id, RunStatus.DRAFTING)
        t0 = time.perf_counter()
        llm = self._llm or LLMService(settings=self.settings)
        try:
            if request.mode is ResearchMode.MODEL_ONLY:
                response = await llm.generate_text(
                    build_model_only_prompt(request.topic),
                    system=MODEL_ONLY_SYSTEM,
                    max_tokens=profile.max_report_tokens,
                )
                used_labels: set[str] = set()
                phantom = set(CITATION_PATTERN.findall(response.text))
                if phantom:
                    warnings.append(
                        f"The model emitted citation-like markers with no evidence "
                        f"behind them: {sorted(phantom)}. They refer to nothing."
                    )
                prompt_version = MODEL_ONLY_PROMPT_VERSION
            else:
                response, used_labels = await self._synthesize(
                    request.topic, evidence.blocks, set(evidence.labels.values()),
                    profile, llm, warnings,
                )
                prompt_version = SYNTHESIS_PROMPT_VERSION
            if response.finish_reason == "length":
                warnings.append("The report was truncated by the token limit.")
        finally:
            if self._llm is None:
                await llm.aclose()
        timings["synthesis"] = round(time.perf_counter() - t0, 3)

        # The Sources section is appended by code from the real retrieved sources,
        # so no URL in a final report can be a hallucination. MODEL_ONLY has none.
        report = response.text.rstrip() + "\n"
        if evidence.labels:
            report = self._with_sources_section(
                response.text, evidence.labels, evidence.meta_by_fp
            )

        title = self._report_title(response.text, request.topic)
        self.research.record_report(
            run_id,
            title=title,
            markdown=report,
            model=response.model,
            prompt_version=prompt_version,
        )
        self.research.advance_status(run_id, RunStatus.COMPLETED)

        sources = [
            CitedSourceRead(
                label=label,
                source_id=evidence.source_ids.get(fingerprint),
                source_type=evidence.meta_by_fp[fingerprint].source_type.value,
                title=evidence.meta_by_fp[fingerprint].title,
                url=evidence.meta_by_fp[fingerprint].url,
                doi=evidence.meta_by_fp[fingerprint].doi,
                provider=evidence.meta_by_fp[fingerprint].provider,
                year=evidence.meta_by_fp[fingerprint].year,
                venue=evidence.meta_by_fp[fingerprint].venue,
                evidence_depth=evidence.depth_by_fp[fingerprint],
                fingerprint=fingerprint,
                chunks_used=evidence.chunks_used[fingerprint],
                cited_in_report=label in used_labels,
            )
            for fingerprint, label in evidence.labels.items()
        ]

        metadata = ExecutionMetadata(
            mode=request.mode.value,
            depth=request.depth,
            model=response.model,
            evidence_selection=evidence.selection,
            embedding_model=evidence.embedding_model,
            collection=evidence.collection,
            web_candidates=evidence.web_candidates,
            academic_candidates=evidence.academic_candidates,
            sources_attached=len(evidence.source_ids),
            documents_processed=evidence.documents_processed,
            documents_with_text=evidence.documents_with_text,
            chunks_indexed=evidence.chunks_indexed,
            chunks_retrieved=evidence.chunks_retrieved,
            sources_in_evidence=len(evidence.labels),
            sources_cited=len(used_labels),
            llm_calls=llm.calls_made,
            token_usage=(
                llm.total_usage.model_dump() if llm.total_usage.total_tokens else None
            ),
            timings_seconds=timings,
            total_seconds=round(time.perf_counter() - started, 3),
            provider_failures=failures,
            warnings=warnings,
        )

        logger.info(
            "Run %s completed (%s): %d sources, %d passages, %d cited, %.1fs",
            run_id,
            request.mode.value,
            len(evidence.source_ids),
            evidence.chunks_retrieved,
            len(used_labels),
            metadata.total_seconds,
        )
        return ResearchStartResponse(
            research_id=run_id,
            topic=request.topic,
            status=RunStatus.COMPLETED,
            report=report,
            sources=sources,
            execution_metadata=metadata,
        )

    # --- evidence acquisition: the one place the modes differ -------------------

    async def _acquire_evidence(
        self,
        run_id: uuid.UUID,
        request: ResearchStartRequest,
        profile: DepthProfile,
        timings: dict[str, float],
        failures: list[ProviderFailure],
    ) -> Evidence:
        if request.mode is ResearchMode.MODEL_ONLY:
            # PLANNING → DRAFTING directly; there is nothing to retrieve, and
            # pretending otherwise would put a retrieval stage on a run that had none.
            return Evidence(selection="none")

        self.research.advance_status(run_id, RunStatus.RETRIEVING)
        include_academic = request.mode is ResearchMode.SEARCH_GROUNDED

        t0 = time.perf_counter()
        candidates = await self._retrieve(request.topic, profile, failures, include_academic)
        timings["retrieval"] = round(time.perf_counter() - t0, 3)
        if not candidates:
            raise NoEvidenceError(
                "Retrieval returned no source for this topic.",
                details={"topic": request.topic, "provider_failures": len(failures)},
            )

        t0 = time.perf_counter()
        source_ids = self._attach_sources(run_id, candidates)
        timings["source_normalisation"] = round(time.perf_counter() - t0, 3)

        t0 = time.perf_counter()
        documents = self.processor.process_many(candidates)
        chunks = [chunk for document in documents if not document.is_empty
                  for chunk in document.chunks]
        timings["processing"] = round(time.perf_counter() - t0, 3)
        if not chunks:
            raise NoEvidenceError(
                "Every retrieved source was metadata-only or cleaned to nothing; "
                "there is no text to ground a report in.",
                details={"sources": len(candidates)},
            )

        evidence = Evidence(
            selection="semantic_top_k" if include_academic else "provider_rank",
            source_ids=source_ids,
            web_candidates=sum(
                1 for c in candidates if not isinstance(c, AcademicCandidateSource)
            ),
            academic_candidates=sum(
                1 for c in candidates if isinstance(c, AcademicCandidateSource)
            ),
            documents_processed=len(documents),
            documents_with_text=sum(1 for d in documents if not d.is_empty),
        )

        if request.mode is ResearchMode.SEARCH_GROUNDED:
            # Embed into a per-run collection; evidence is the top-k by similarity.
            collection = f"run_{run_id.hex}"
            store = self._store_for(collection)
            t0 = time.perf_counter()
            index_result = await asyncio.to_thread(store.index_chunks, chunks)
            timings["indexing"] = round(time.perf_counter() - t0, 3)

            t0 = time.perf_counter()
            hits = await asyncio.to_thread(store.search, request.topic, profile.top_k)
            timings["evidence_retrieval"] = round(time.perf_counter() - t0, 3)
            if not hits:
                raise NoEvidenceError("The vector search returned no passages.")

            evidence.collection = collection
            evidence.embedding_model = store.embedder.model_name
            evidence.chunks_indexed = index_result.newly_indexed + index_result.already_present
            for hit in hits:
                evidence.add_passage(hit)
        else:
            # HYBRID: no embeddings. The same evidence budget (top_k passages), but
            # selected by the provider's own ranking — that difference IS the
            # experiment, and `evidence_selection` records it.
            t0 = time.perf_counter()
            for chunk in chunks[: profile.top_k]:
                evidence.add_passage(chunk)
            timings["evidence_retrieval"] = round(time.perf_counter() - t0, 3)

        return evidence

    # --- stages ---------------------------------------------------------------

    async def _retrieve(
        self,
        topic: str,
        profile: DepthProfile,
        failures: list[ProviderFailure],
        include_academic: bool,
    ) -> list[Candidate]:
        """The mode's providers, concurrently. Some may fail — recorded, not hidden;
        all of them failing raises, because a retrieval mode with no retrieval is
        not that mode."""
        web = self._web or TavilySearchService(settings=self.settings)
        searches: list[tuple[str, Coroutine[Any, Any, Any]]] = [
            ("tavily", web.search(topic, max_results=profile.web_results))
        ]

        academic = None
        if include_academic:
            academic = self._academic or SemanticScholarService(settings=self.settings)
            searches.append(
                ("semantic_scholar", academic.search(topic, limit=profile.academic_results))
            )

        try:
            results = await asyncio.gather(
                *(coroutine for _, coroutine in searches), return_exceptions=True
            )
        finally:
            if self._web is None:
                await web.aclose()
            if academic is not None and self._academic is None:
                await academic.aclose()

        outcomes = list(zip((name for name, _ in searches), results, strict=True))
        for provider, outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, RetrievalError):
                raise outcome  # our bug, not a provider failure — surface it as one
            if isinstance(outcome, RetrievalError):
                failures.append(
                    ProviderFailure(provider=provider, code=outcome.code, message=outcome.message)
                )
                logger.warning("%s failed during retrieval: %s", provider, outcome.message)

        errors = [o for _, o in outcomes if isinstance(o, RetrievalError)]
        if len(errors) == len(outcomes):
            raise errors[0]

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for _, outcome in outcomes:
            if isinstance(outcome, BaseException):
                continue
            for candidate in outcome.candidates:
                if candidate.fingerprint not in seen:
                    seen.add(candidate.fingerprint)
                    candidates.append(candidate)
        return candidates

    def _attach_sources(
        self, run_id: uuid.UUID, candidates: list[Candidate]
    ) -> dict[str, uuid.UUID]:
        """Persist and link every candidate; fingerprint → source row id."""
        source_ids: dict[str, uuid.UUID] = {}
        for candidate in candidates:
            if isinstance(candidate, AcademicCandidateSource):
                meta = SourceMetadata.from_academic_candidate(candidate)
            else:
                meta = SourceMetadata.from_web_candidate(candidate)
            source, _ = self.research.attach_source(
                run_id,
                fingerprint=meta.fingerprint,
                evidence_depth=candidate.evidence_depth,
                retriever=meta.provider,
                retrieval_query=meta.query,
                rank=meta.rank,
                **meta.to_source_create().model_dump(),
            )
            source_ids[meta.fingerprint] = source.id
        return source_ids

    async def _synthesize(
        self,
        topic: str,
        blocks: list[str],
        known_labels: set[str],
        profile: DepthProfile,
        llm: LLMService,
        warnings: list[str],
    ) -> tuple[LLMResponse, set[str]]:
        """One draft, one retry if it cited nothing, then an honest failure."""
        prompt = build_synthesis_prompt(topic, blocks)
        response = await llm.generate_text(
            prompt, system=SYNTHESIS_SYSTEM, max_tokens=profile.max_report_tokens
        )
        used = set(CITATION_PATTERN.findall(response.text)) & known_labels
        if not used:
            warnings.append("The first draft contained no citations; retried once.")
            response = await llm.generate_text(
                prompt + RETRY_REMINDER,
                system=SYNTHESIS_SYSTEM,
                max_tokens=profile.max_report_tokens,
            )
            used = set(CITATION_PATTERN.findall(response.text)) & known_labels
            if not used:
                raise UncitedReportError(details={"attempts": 2})

        unknown = set(CITATION_PATTERN.findall(response.text)) - known_labels
        if unknown:
            warnings.append(
                f"The report cites labels that were never provided: {sorted(unknown)}. "
                "Those references resolve to nothing."
            )
        return response, used

    # --- helpers ---------------------------------------------------------------

    def _store_for(self, collection: str) -> VectorStore:
        if self._store_factory is not None:
            return self._store_factory(collection)
        return VectorStore(embedder=_default_embedder(), collection_name=collection)

    @staticmethod
    def _with_sources_section(
        report_text: str, labels: dict[str, str], meta_by_fp: dict[str, SourceMetadata]
    ) -> str:
        lines = ["", "---", "", "## Sources", ""]
        ordered = sorted(labels.items(), key=lambda item: int(item[1][1:]))
        for fingerprint, label in ordered:
            meta = meta_by_fp[fingerprint]
            descriptor = meta.title or meta.url or meta.doi or "untitled source"
            provenance: list[str] = []
            if meta.provider:
                provenance.append(meta.provider)
            if meta.venue:
                provenance.append(meta.venue)
            if meta.year is not None:
                provenance.append(str(meta.year))
            locator = meta.url or (f"https://doi.org/{meta.doi}" if meta.doi else None)
            entry = f"- **[{label}]** {descriptor}"
            if provenance:
                entry += f" ({', '.join(provenance)})"
            if locator:
                entry += f" — {locator}"
            lines.append(entry)
        return report_text.rstrip() + "\n" + "\n".join(lines) + "\n"

    @staticmethod
    def _report_title(report_text: str, topic: str) -> str:
        for line in report_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip() or f"Research report: {topic}"
        return f"Research report: {topic}"
