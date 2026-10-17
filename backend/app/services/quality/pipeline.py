"""
The report-quality-v1 pipeline, as far as stage 31 (S4) takes it: stages 1–5, 10a, 6
and the claim→sentence alignment.

    gate ─▶ run (report-quality configuration) ─▶ PLANNING: plan, plan rows
         ─▶ RETRIEVING: per-subquestion retrieval, sources linked with their
            subquestion, documents processed and stored, evidence selected,
            S# labels frozen and persisted, gaps marked not addressed
         ─▶ EvidencePreparation (in memory)
    draft_and_align ─▶ DRAFTING: structured draft (synthesis-structured-v1), report and
            sections stored (sections linked to subquestions), the existing claim
            extractor, deterministic claim→sentence alignment, audit row started
         ─▶ DraftPreparation — verification, citation check and revision are S5

**Gated.** Nothing runs unless `REPORT_QUALITY_ENABLED` is on (off by default) and the
mode is `search_grounded`; the check happens before any row is written or any call
is made. No route, request field or benchmark arm calls this yet (S6/S7).

**Composed, not refactored.** It calls the existing retrieval services, document
processor, research service (status machine, document persistence, citation
labels) and repositories; the legacy orchestrator and research service are not
modified. A failure marks the run failed with the error's own code, as the legacy
pipeline does.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.constants import ResearchMode, RunStatus
from app.core.exceptions import AppError
from app.schemas.research import ResearchRequest, ResearchStartRequest
from app.services.claims import ClaimExtractor
from app.services.llm import LLMNotConfigured, LLMService
from app.services.orchestration import DEPTH_PROFILES
from app.services.orchestration.exceptions import NoEvidenceError
from app.services.orchestration.orchestrator import DepthProfile
from app.services.processing import DocumentProcessor, SourceMetadata
from app.services.quality.alignment import Alignment, ClaimToAlign, align_claims
from app.services.quality.drafting import DraftResult, write_draft
from app.services.quality.evidence import (
    EvidenceSet,
    RetrievalOutcome,
    per_subquestion_results,
    retrieve,
    select_evidence,
)
from app.services.quality.exceptions import ReportQualityDisabled, ReportQualityModeError
from app.services.quality.models import AliasTable
from app.services.quality.planner import CoveragePlanner, PlanResult
from app.services.quality.prompts import PLANNER_PROMPT_VERSION
from app.services.quality.store import QualityStore, quality_configuration_values
from app.services.research_service import ResearchService
from app.services.retrieval import (
    AcademicCandidateSource,
    SemanticScholarService,
    TavilySearchService,
)
from app.services.vectorstore import get_default_embedder

logger = logging.getLogger(__name__)


@dataclass
class EvidencePreparation:
    research_id: uuid.UUID
    plan: PlanResult
    evidence: EvidenceSet
    retrieval: RetrievalOutcome
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    topic: str = ""
    depth: str = "quick"


@dataclass
class DraftPreparation:
    research_id: uuid.UUID
    report_id: uuid.UUID
    draft: DraftResult
    aliases: AliasTable  # sources, passages, subquestions — and now claims
    alignments: dict[str, Alignment]
    extractor_model: str
    warnings: list[str] = field(default_factory=list)


def budget_for(settings: Settings, depth: str) -> tuple[int, int]:
    """(max subquestions, total evidence passages) for a depth — provisional knobs."""
    key = depth.upper()
    return (
        getattr(settings, f"REPORT_QUALITY_SUBQUESTIONS_{key}"),
        getattr(settings, f"REPORT_QUALITY_EVIDENCE_PASSAGES_{key}"),
    )


class ReportQualityPipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        research_service: ResearchService | None = None,
        store: QualityStore | None = None,
        llm=None,
        web_search=None,
        academic_search=None,
        embedder=None,
        processor: DocumentProcessor | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.research = research_service or ResearchService(settings=self.settings)
        self.store = store or QualityStore()
        self._llm = llm
        self._web = web_search
        self._academic = academic_search
        self._embedder = embedder
        self.processor = processor or DocumentProcessor()

    def check_gate(self, request: ResearchStartRequest) -> None:
        """Refuse before anything is written or called."""
        if not self.settings.REPORT_QUALITY_ENABLED:
            raise ReportQualityDisabled()
        if request.mode is not ResearchMode.SEARCH_GROUNDED:
            raise ReportQualityModeError(details={"mode": request.mode.value})
        if self.settings.llm_provider is None:
            raise LLMNotConfigured(details={"setting": "OPENAI_API_KEY"})

    async def prepare_evidence(
        self, request: ResearchStartRequest, *, benchmark_run_id: uuid.UUID | None = None
    ) -> EvidencePreparation:
        self.check_gate(request)
        max_subquestions, total_passages = budget_for(self.settings, request.depth)
        profile = DEPTH_PROFILES[request.depth]

        base = self.research.configuration_values_for(
            ResearchRequest(
                topic=request.topic,
                mode=request.mode,
                max_subquestions=max_subquestions,
                max_sources=profile.max_sources,
            )
        )
        values = quality_configuration_values(
            {**base, "planner_prompt_version": PLANNER_PROMPT_VERSION}, self.settings
        )
        run_id = self.store.create_run(request.topic, values, benchmark_run_id=benchmark_run_id)
        try:
            return await self._prepare(run_id, request, max_subquestions, total_passages, profile)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "internal_error"
            message = exc.message if isinstance(exc, AppError) else str(exc)[:500]
            try:
                self.research.fail_run(run_id, code=code, message=message)
            except Exception:  # noqa: BLE001 - the original error must not be masked
                logger.exception("Could not record the failure of run %s", run_id)
            raise

    async def _prepare(
        self,
        run_id: uuid.UUID,
        request: ResearchStartRequest,
        max_subquestions: int,
        total_passages: int,
        profile: DepthProfile,
    ) -> EvidencePreparation:
        timings: dict[str, float] = {}
        embedder = self._embedder or get_default_embedder()

        # --- stages 1–2 -------------------------------------------------------------
        self.research.advance_status(run_id, RunStatus.PLANNING)
        t0 = time.perf_counter()
        llm = self._llm or LLMService(settings=self.settings)
        try:
            plan = await CoveragePlanner(
                llm,
                embedder.embed_documents,
                max_subquestions=max_subquestions,
                duplicate_min_cosine=self.settings.REPORT_QUALITY_PLAN_DUPLICATE_MIN_COSINE,
            ).plan(request.topic)
        finally:
            if self._llm is None:
                await llm.aclose()
        ids = self.store.record_plan(
            run_id,
            [
                (sq.text, f"web: {sq.web_query} | academic: {sq.academic_query}")
                for sq in plan.subquestions
            ],
        )
        subquestion_ids = {sq.alias: id_ for sq, id_ in zip(plan.subquestions, ids, strict=True)}
        timings["planning"] = round(time.perf_counter() - t0, 3)

        # --- stage 3 ----------------------------------------------------------------
        self.research.advance_status(run_id, RunStatus.RETRIEVING)
        t0 = time.perf_counter()
        web = self._web or TavilySearchService(settings=self.settings)
        academic = self._academic or SemanticScholarService(settings=self.settings)
        try:
            retrieval = await retrieve(
                plan.subquestions,
                web,
                academic,
                web_results=per_subquestion_results(profile.web_results, len(plan.subquestions)),
                academic_results=per_subquestion_results(
                    profile.academic_results, len(plan.subquestions)
                ),
            )
        finally:
            if self._web is None:
                await web.aclose()
            if self._academic is None:
                await academic.aclose()
        timings["retrieval"] = round(time.perf_counter() - t0, 3)
        if not retrieval.candidates:
            raise NoEvidenceError(
                "Retrieval returned no source for any subquestion.",
                details={"topic": request.topic, "provider_failures": len(retrieval.failures)},
            )

        attach = []
        for candidate in retrieval.candidates:
            meta = (
                SourceMetadata.from_academic_candidate(candidate)
                if isinstance(candidate, AcademicCandidateSource)
                else SourceMetadata.from_web_candidate(candidate)
            )
            first = retrieval.found_by[candidate.fingerprint][0]
            attach.append(
                (
                    {
                        "fingerprint": meta.fingerprint,
                        "evidence_depth": candidate.evidence_depth,
                        "retriever": meta.provider,
                        "retrieval_query": meta.query,
                        "rank": meta.rank,
                        **meta.to_source_create().model_dump(),
                    },
                    subquestion_ids[first],
                )
            )
        source_ids = self.store.attach_sources(run_id, attach)

        # --- stages 4–5 -------------------------------------------------------------
        t0 = time.perf_counter()
        documents = self.processor.process_many(retrieval.candidates)
        chunks = [
            chunk for document in documents if not document.is_empty for chunk in document.chunks
        ]
        if not chunks:
            raise NoEvidenceError(
                "Every retrieved source was metadata-only or cleaned to nothing; "
                "there is no text to ground a report in.",
                details={"sources": len(retrieval.candidates)},
            )
        self.research.record_processed_documents(run_id, documents=documents, source_ids=source_ids)
        evidence = select_evidence(
            plan.subquestions,
            subquestion_ids,
            chunks,
            retrieval,
            source_ids,
            embedder,
            total_passages=total_passages,
            entities=plan.analysis.key_entities if plan.analysis else (),
        )
        self.research.set_citation_labels(run_id, evidence.labels)
        if evidence.gaps:
            self.store.mark_gaps(run_id, [gap.subquestion_id for gap in evidence.gaps])
        timings["evidence"] = round(time.perf_counter() - t0, 3)

        warnings = list(plan.warnings) + [
            f"{f.provider} failed for {f.subquestion}: {f.message}" for f in retrieval.failures
        ]
        logger.info(
            "Run %s evidence prepared: %d subquestions (%s), %d sources, %d passages, %d gaps",
            run_id,
            len(plan.subquestions),
            "fallback" if plan.fallback else "planned",
            len(source_ids),
            len(evidence.chunks),
            len(evidence.gaps),
        )
        return EvidencePreparation(
            run_id,
            plan,
            evidence,
            retrieval,
            timings,
            warnings,
            topic=request.topic,
            depth=request.depth,
        )

    # --- stages 10a, 6 and alignment (stage 31, S4) ---------------------------------

    async def draft_and_align(self, prepared: EvidencePreparation) -> DraftPreparation:
        """Draft, store, extract claims and align them. Same gate, same failure policy:
        any failure marks the run failed with the error's own code."""
        if not self.settings.REPORT_QUALITY_ENABLED:
            raise ReportQualityDisabled()
        try:
            return await self._draft(prepared)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "internal_error"
            message = exc.message if isinstance(exc, AppError) else str(exc)[:500]
            try:
                self.research.fail_run(prepared.research_id, code=code, message=message)
            except Exception:  # noqa: BLE001 - the original error must not be masked
                logger.exception("Could not record the failure of run %s", prepared.research_id)
            raise

    async def _draft(self, prepared: EvidencePreparation) -> DraftPreparation:
        run_id, evidence, plan = prepared.research_id, prepared.evidence, prepared.plan
        embedder = self._embedder or get_default_embedder()
        self.research.advance_status(run_id, RunStatus.DRAFTING)

        llm = self._llm or LLMService(settings=self.settings)
        try:
            draft = await write_draft(
                llm,
                prepared.topic,
                plan,
                evidence,
                max_tokens=getattr(
                    self.settings, f"REPORT_QUALITY_DRAFT_MAX_TOKENS_{prepared.depth.upper()}"
                ),
            )
            report = self.research.record_report(
                run_id,
                title=draft.report.title,
                markdown=draft.markdown,
                model=draft.model,
                prompt_version=draft.prompt_version,
            )
            self.store.link_sections(
                report.id,
                [
                    evidence.aliases.subquestions.get(section.subquestion)
                    for section in draft.report.sections
                ],
                [section.heading for section in draft.report.sections],
            )
            # Stage 6: the existing extractor, unmodified (claim-extraction-v1).
            extraction = await ClaimExtractor(
                settings=self.settings, research_service=self.research, llm=llm
            ).extract(run_id)
        finally:
            if self._llm is None:
                await llm.aclose()

        claims = sorted(extraction.claims, key=lambda claim: claim.position)
        claim_aliases = {f"C{index + 1}": claim.claim_id for index, claim in enumerate(claims)}
        alignments = align_claims(
            [
                ClaimToAlign(alias, claim.claim_text, frozenset(claim.markers))
                for alias, claim in zip(claim_aliases, claims, strict=True)
            ],
            draft.sentences(evidence),
            embedder.embed_documents,
            min_margin=self.settings.REPORT_QUALITY_ALIGNMENT_MIN_MARGIN,
        )
        aliases = AliasTable(
            sources=evidence.aliases.sources,
            passages=evidence.aliases.passages,
            claims=claim_aliases,
            subquestions=evidence.aliases.subquestions,
        )

        warnings = [*prepared.warnings, *draft.warnings]
        unaligned = sorted(a for a, result in alignments.items() if result.status == "unaligned")
        if unaligned:
            warnings.append(
                f"{len(unaligned)} claim(s) could not be aligned to one draft "
                f"sentence and will not get a citation check: {unaligned}"
            )
        self.store.start_audit(report.id)
        self.store.update_audit(
            report.id,
            stage="aligned",
            payload={
                "plan": plan.as_record(),
                "evidence": evidence.as_record(),
                "draft": draft.as_record(),
                "aliases": aliases.as_record(),
                "alignments": {
                    alias: {
                        "status": a.status,
                        "reason": a.reason,
                        "sentence_index": a.sentence_index,
                        "score": a.score,
                        "margin": a.margin,
                    }
                    for alias, a in alignments.items()
                },
                "extraction": {
                    "model": extraction.extractor_model,
                    "prompt_version": extraction.prompt_version,
                    "claims": len(claims),
                },
                "timings": prepared.timings,
                "warnings": warnings,
            },
        )
        logger.info(
            "Run %s drafted: %d sections, %d claims, %d unaligned",
            run_id,
            len(draft.report.sections),
            len(claims),
            len(unaligned),
        )
        return DraftPreparation(
            run_id, report.id, draft, aliases, alignments, extraction.extractor_model, warnings
        )
