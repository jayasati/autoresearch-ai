"""
The first end-to-end pipeline: topic in, grounded and cited report out.

    topic
      → Tavily + Semantic Scholar        (concurrently; one may fail, both may not)
      → source normalisation             (SourceMetadata; rows attached to the run)
      → document processing              (clean, chunk — stage 8)
      → embeddings → ChromaDB            (per-run collection — stage 8b)
      → evidence retrieval               (top-k chunks for the topic)
      → OpenAI                           (grounded synthesis — stage 9)
      → report with [S#] citations       (Sources section appended by CODE, not model)

SEARCH_GROUNDED only, and honest about what that means today: evidence is snippets
and abstracts (nothing fetches full text yet), there is no sub-question planning
(the topic is the one query), and no claim verification (that is the next stage —
the citations here are the model following instructions, not yet checked claims).

Failure policy — the spec's "do not fabricate successful results" rule, enforced:

- One retrieval provider failing is **partial retrieval**: recorded in
  `execution_metadata.provider_failures`, run continues. Both failing fails the run.
- Nothing retrieved, or nothing with usable text, raises `NoEvidenceError` (422):
  a grounded report cannot exist, so no report is returned.
- A report whose sentences cite nothing gets one retry; a second uncited draft
  raises `UncitedReportError`. An uncited report is the MODEL_ONLY mode wearing this
  mode's name.
- Every failure after run creation marks the run `failed` in the database with the
  error's own code, so "provider was down" and "our bug" stay distinguishable.

The run, its sources and its report are persisted through `ResearchService`; the
chunks live in the run's ChromaDB collection with full source metadata. Document and
chunk *rows* are deliberately not written yet — claim verification is what needs
claim→chunk links, so those tables arrive with it.
"""

import asyncio
import logging
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

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
from app.services.llm import LLMNotConfigured, LLMService
from app.services.orchestration.exceptions import NoEvidenceError, UncitedReportError
from app.services.orchestration.prompts import (
    RETRY_REMINDER,
    SYNTHESIS_PROMPT_VERSION,
    SYNTHESIS_SYSTEM,
    build_synthesis_prompt,
    format_evidence_block,
)
from app.services.processing import DocumentProcessor, SourceMetadata
from app.services.research_service import ResearchService
from app.services.retrieval import (
    AcademicCandidateSource,
    AcademicSearchResult,
    CandidateSource,
    RetrievalError,
    SemanticScholarService,
    TavilySearchService,
    WebSearchResult,
)
from app.services.vectorstore import SentenceTransformerEmbedder, VectorStore

logger = logging.getLogger(__name__)

CITATION_PATTERN = re.compile(r"\[(S\d+)\]")

Candidate = CandidateSource | AcademicCandidateSource


@dataclass(frozen=True)
class DepthProfile:
    """What each depth buys. The knobs, in one place, so "deep" means the same
    thing in every run and the benchmark can hold it constant."""

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


@lru_cache
def _default_embedder() -> SentenceTransformerEmbedder:
    """One embedder per process. The model takes seconds to load and holds no
    per-run state, so reloading it for every request would be pure waste."""
    return SentenceTransformerEmbedder()


class ResearchOrchestrator:
    """Wires retrieval → processing → vectorstore → LLM into one run.

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
        """Execute the full pipeline for one topic. Synchronous from the caller's
        point of view: returns when the report exists, raises when it cannot."""
        if self.settings.llm_provider is None:
            # Checked before any row or paid call: a run that could never reach
            # synthesis should not spend two providers' quota finding sources.
            raise LLMNotConfigured(details={"setting": "OPENAI_API_KEY"})

        profile = DEPTH_PROFILES[request.depth]
        run = self.research.create_run(
            ResearchRequest(
                topic=request.topic,
                mode=ResearchMode.SEARCH_GROUNDED,
                max_sources=profile.max_sources,
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
        self.research.advance_status(run_id, RunStatus.RETRIEVING)

        # 1. Retrieval, both providers concurrently.
        t0 = time.perf_counter()
        candidates = await self._retrieve(request.topic, profile, failures)
        timings["retrieval"] = round(time.perf_counter() - t0, 3)
        if not candidates:
            raise NoEvidenceError(
                "Neither provider returned a source for this topic.",
                details={"topic": request.topic, "provider_failures": len(failures)},
            )

        # 2. Source normalisation: every candidate becomes a persisted, deduplicated
        # source row linked to this run with its retrieval facts.
        t0 = time.perf_counter()
        source_ids = self._attach_sources(run_id, candidates)
        timings["source_normalisation"] = round(time.perf_counter() - t0, 3)

        # 3. Processing: clean and chunk whatever text the providers gave us.
        t0 = time.perf_counter()
        documents = self.processor.process_many(candidates)
        chunks = [chunk for document in documents if not document.is_empty
                  for chunk in document.chunks]
        documents_with_text = sum(1 for document in documents if not document.is_empty)
        timings["processing"] = round(time.perf_counter() - t0, 3)
        if not chunks:
            raise NoEvidenceError(
                "Every retrieved source was metadata-only or cleaned to nothing; "
                "there is no text to ground a report in.",
                details={"sources": len(candidates)},
            )

        # 4. Embeddings → ChromaDB, in a collection scoped to this run so evidence
        # retrieval can never pick up another run's chunks.
        collection = f"run_{run_id.hex}"
        store = self._store_for(collection)
        t0 = time.perf_counter()
        index_result = await asyncio.to_thread(store.index_chunks, chunks)
        timings["indexing"] = round(time.perf_counter() - t0, 3)

        # 5. Evidence retrieval.
        t0 = time.perf_counter()
        hits = await asyncio.to_thread(store.search, request.topic, profile.top_k)
        timings["evidence_retrieval"] = round(time.perf_counter() - t0, 3)
        if not hits:
            raise NoEvidenceError("The vector search returned no passages.")

        # 6. Labels: one per source, in first-retrieved order, shared by all of a
        # source's chunks — citations point at sources, passages carry them.
        labels: dict[str, str] = {}
        chunks_used: Counter[str] = Counter()
        meta_by_fp: dict[str, SourceMetadata] = {}
        depth_by_fp: dict[str, str] = {}
        blocks: list[str] = []
        for hit in hits:
            fingerprint = hit.source.fingerprint
            label = labels.setdefault(fingerprint, f"S{len(labels) + 1}")
            chunks_used[fingerprint] += 1
            meta_by_fp.setdefault(fingerprint, hit.source)
            depth_by_fp.setdefault(fingerprint, hit.depth.value)
            blocks.append(format_evidence_block(label, hit))

        # 7. Synthesis.
        self.research.advance_status(run_id, RunStatus.DRAFTING)
        t0 = time.perf_counter()
        llm = self._llm or LLMService(settings=self.settings)
        try:
            response, used_labels = await self._synthesize(
                request.topic, blocks, set(labels.values()), profile, llm, warnings
            )
        finally:
            if self._llm is None:
                await llm.aclose()
        timings["synthesis"] = round(time.perf_counter() - t0, 3)

        # 8. The Sources section is appended by code from the real retrieved sources,
        # so no URL in the final report can be a hallucination.
        report = self._with_sources_section(response.text, labels, meta_by_fp)

        title = self._report_title(response.text, request.topic)
        self.research.record_report(
            run_id,
            title=title,
            markdown=report,
            model=response.model,
            prompt_version=SYNTHESIS_PROMPT_VERSION,
        )
        self.research.advance_status(run_id, RunStatus.COMPLETED)

        sources = [
            CitedSourceRead(
                label=label,
                source_id=source_ids.get(fingerprint),
                source_type=meta_by_fp[fingerprint].source_type.value,
                title=meta_by_fp[fingerprint].title,
                url=meta_by_fp[fingerprint].url,
                doi=meta_by_fp[fingerprint].doi,
                provider=meta_by_fp[fingerprint].provider,
                year=meta_by_fp[fingerprint].year,
                venue=meta_by_fp[fingerprint].venue,
                evidence_depth=depth_by_fp[fingerprint],
                fingerprint=fingerprint,
                chunks_used=chunks_used[fingerprint],
                cited_in_report=label in used_labels,
            )
            for fingerprint, label in labels.items()
        ]

        metadata = ExecutionMetadata(
            mode=ResearchMode.SEARCH_GROUNDED.value,
            depth=request.depth,
            model=response.model,
            embedding_model=store.embedder.model_name,
            collection=collection,
            web_candidates=sum(
                1 for c in candidates if not isinstance(c, AcademicCandidateSource)
            ),
            academic_candidates=sum(
                1 for c in candidates if isinstance(c, AcademicCandidateSource)
            ),
            sources_attached=len(source_ids),
            documents_processed=len(documents),
            documents_with_text=documents_with_text,
            chunks_indexed=index_result.newly_indexed + index_result.already_present,
            chunks_retrieved=len(hits),
            sources_in_evidence=len(labels),
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
            "Run %s completed: %d sources, %d chunks, %d cited, %.1fs",
            run_id,
            len(source_ids),
            len(hits),
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

    # --- stages ---------------------------------------------------------------

    async def _retrieve(
        self, topic: str, profile: DepthProfile, failures: list[ProviderFailure]
    ) -> list[Candidate]:
        """Both providers, concurrently. One may fail — recorded, not hidden; both
        failing raises, because a grounded run with no retrieval is not a run."""
        web = self._web or TavilySearchService(settings=self.settings)
        academic = self._academic or SemanticScholarService(settings=self.settings)
        try:
            web_outcome, academic_outcome = await asyncio.gather(
                web.search(topic, max_results=profile.web_results),
                academic.search(topic, limit=profile.academic_results),
                return_exceptions=True,
            )
        finally:
            if self._web is None:
                await web.aclose()
            if self._academic is None:
                await academic.aclose()

        outcomes: list[tuple[str, WebSearchResult | AcademicSearchResult | BaseException]] = [
            ("tavily", web_outcome),
            ("semantic_scholar", academic_outcome),
        ]
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
    ):
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
        if response.finish_reason == "length":
            warnings.append("The report was truncated by the token limit.")
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
