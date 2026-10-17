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
         ─▶ DraftPreparation
    verify_claims ─▶ VERIFYING: verification-v2 per claim (legacy verifier, unchanged)
            under the claim cap, deadline and budget; citation-check-v1 against exactly
            the cited passages; claim_audit and citation_check rows; quantitative,
            subquestion and conflict assessments (audit-only)
         ─▶ VerificationOutcome
    finalize ─▶ revision only if some claim cannot stand as drafted
            (final-report-structured-v1, budgeted), V1–V7 validation, re-verification of
            changed sentences against exactly their cited passages, second validation,
            final render with code-owned markers, final_claim_check / claim_audit
            placements, terminal audit status; run COMPLETED
         ─▶ FinalOutcome (complete | degraded | finalization_failed)

Every model call — planner, draft, extractor, verifier, citation check — goes through
one `BudgetedLLM` per run (attempts counted, SDK retries off, tokens measured or
estimated and labelled), created with the run in `prepare_evidence`.

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
from app.core.constants import PoolStatus, ResearchMode, RunStatus
from app.core.exceptions import AppError
from app.schemas.research import ResearchRequest, ResearchStartRequest
from app.services.claims import ClaimExtractor
from app.services.llm import LLMNotConfigured, LLMService
from app.services.orchestration import DEPTH_PROFILES
from app.services.orchestration.exceptions import NoEvidenceError
from app.services.orchestration.orchestrator import DepthProfile
from app.services.processing import DocumentProcessor, SourceMetadata
from app.services.quality.alignment import Alignment, ClaimToAlign, align_claims
from app.services.quality.budget import (
    DEFAULT_OUTPUT_ESTIMATE,
    BudgetedLLM,
    BudgetInsufficient,
    RunBudget,
    no_sdk_retries,
)
from app.services.quality.drafting import DraftResult, write_draft
from app.services.quality.evidence import (
    EvidenceSet,
    RetrievalOutcome,
    per_subquestion_results,
    retrieve,
    select_evidence,
)
from app.services.quality.exceptions import ReportQualityDisabled, ReportQualityModeError
from app.services.quality.finalization import Finalizer, FinalOutcome, terminal_status
from app.services.quality.models import AliasTable
from app.services.quality.planner import CoveragePlanner, PlanResult
from app.services.quality.prompts import FINAL_REPORT_PROMPT_VERSION, PLANNER_PROMPT_VERSION
from app.services.quality.store import QualityStore, quality_configuration_values
from app.services.quality.verification import (
    ClaimRecord,
    Verifier,
    conflict_contexts,
    conflict_records,
    initial_claim_cap,
    quantitative_findings,
    subquestion_assessments,
)
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
    budget: RunBudget | None = None


@dataclass
class DraftPreparation:
    research_id: uuid.UUID
    report_id: uuid.UUID
    draft: DraftResult
    aliases: AliasTable  # sources, passages, subquestions — and now claims
    alignments: dict[str, Alignment]
    extractor_model: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class VerificationOutcome:
    research_id: uuid.UUID
    report_id: uuid.UUID
    claims: list[ClaimRecord]
    quantitative_findings: list[dict]
    subquestions: list[dict]
    conflicts: list[dict]
    budget: dict
    warnings: list[str] = field(default_factory=list)


def preflight(settings: Settings, depth: str, budget: RunBudget) -> None:
    """Refuse, before any paid call, a budget that cannot fit a minimal run: planner,
    draft, extraction, one verification and one citation check (provisional rule)."""
    draft_tokens = getattr(settings, f"REPORT_QUALITY_DRAFT_MAX_TOKENS_{depth.upper()}")
    minimum_tokens = 2 * draft_tokens + 3 * DEFAULT_OUTPUT_ESTIMATE
    if budget.max_attempts < 5 or budget.max_tokens < minimum_tokens:
        raise BudgetInsufficient(
            details={
                "attempts": budget.max_attempts,
                "tokens": budget.max_tokens,
                "minimum_attempts": 5,
                "minimum_tokens": minimum_tokens,
            }
        )


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
        store_factory=None,
        clock=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.research = research_service or ResearchService(settings=self.settings)
        self.store = store or QualityStore()
        self._llm = llm
        self._web = web_search
        self._academic = academic_search
        self._embedder = embedder
        self.processor = processor or DocumentProcessor()
        self._store_factory = store_factory
        self._clock = clock

    def _budgeted(self, budget: RunBudget, stage: str) -> BudgetedLLM:
        """The run's budget around an injected LLM, or a fresh client with SDK
        retries off (every attempt is then one counted HTTP request)."""
        inner = self._llm or LLMService(settings=no_sdk_retries(self.settings))
        return BudgetedLLM(inner, budget, stage=stage)

    async def _close(self, llm: BudgetedLLM) -> None:
        if self._llm is None:
            await llm.aclose()

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
        clock = {"clock": self._clock} if self._clock is not None else {}
        budget = RunBudget.for_depth(self.settings, request.depth, **clock)
        preflight(self.settings, request.depth, budget)
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
            return await self._prepare(
                run_id, request, max_subquestions, total_passages, profile, budget
            )
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
        budget: RunBudget,
    ) -> EvidencePreparation:
        timings: dict[str, float] = {}
        embedder = self._embedder or get_default_embedder()

        # --- stages 1–2 -------------------------------------------------------------
        self.research.advance_status(run_id, RunStatus.PLANNING)
        t0 = time.perf_counter()
        llm = self._budgeted(budget, "planning")
        try:
            plan = await CoveragePlanner(
                llm,
                embedder.embed_documents,
                max_subquestions=max_subquestions,
                duplicate_min_cosine=self.settings.REPORT_QUALITY_PLAN_DUPLICATE_MIN_COSINE,
            ).plan(request.topic)
        finally:
            await self._close(llm)
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
            budget=budget,
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

        budget = prepared.budget or RunBudget.for_depth(self.settings, prepared.depth)
        prepared.budget = budget
        llm = self._budgeted(budget, "drafting")
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
            llm.stage = "extraction"
            extraction = await ClaimExtractor(
                settings=self.settings,
                research_service=self.research,
                llm=llm,  # type: ignore[arg-type]
            ).extract(run_id)
        finally:
            await self._close(llm)

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

    # --- stages 7–9 (stage 32, S5a) --------------------------------------------------

    async def verify_claims(
        self, prepared: EvidencePreparation, drafted: DraftPreparation
    ) -> VerificationOutcome:
        """Pool verification, citation checks, claim audits and the audit-only
        assessments. Same gate and failure policy; running out of budget or time is
        NOT a failure — what could not run is recorded as not evaluated."""
        if not self.settings.REPORT_QUALITY_ENABLED:
            raise ReportQualityDisabled()
        try:
            return await self._verify(prepared, drafted)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "internal_error"
            message = exc.message if isinstance(exc, AppError) else str(exc)[:500]
            try:
                self.research.fail_run(prepared.research_id, code=code, message=message)
            except Exception:  # noqa: BLE001 - the original error must not be masked
                logger.exception("Could not record the failure of run %s", prepared.research_id)
            raise

    async def _verify(
        self, prepared: EvidencePreparation, drafted: DraftPreparation
    ) -> VerificationOutcome:
        run_id, evidence = prepared.research_id, prepared.evidence
        budget = prepared.budget or RunBudget.for_depth(self.settings, prepared.depth)
        prepared.budget = budget
        embedder = self._embedder or get_default_embedder()
        self.research.advance_status(run_id, RunStatus.VERIFYING)
        warnings = list(drafted.warnings)

        # Hold back the revision's share now (S5b spends it): verification cannot.
        draft_tokens = getattr(
            self.settings, f"REPORT_QUALITY_DRAFT_MAX_TOKENS_{prepared.depth.upper()}"
        )
        budget.reserve("revision", 2 * draft_tokens)
        cap = initial_claim_cap(
            budget.max_claims, self.settings.REPORT_QUALITY_REVERIFY_RESERVE_FRACTION
        )

        # Each draft sentence's passages and subquestion, in reading order.
        sentence_evidence: list[list[str]] = []
        sentence_subquestion: list[str | None] = []
        for section in drafted.draft.report.sections:
            for sentence in section.sentences:
                sentence_evidence.append(list(sentence.evidence_ids))
                sentence_subquestion.append(
                    section.subquestion
                    if section.subquestion in evidence.aliases.subquestions
                    else None
                )
        core = {s.alias for s in prepared.plan.subquestions if s.is_core}
        _, rows = self.research.claims_awaiting_verification(run_id)
        importance = {row["claim_id"]: row["importance"] for row in rows}
        markers = {
            row["claim_id"]: frozenset(c["marker"] for c in row["citations"]) for row in rows
        }
        records = []
        for position, (alias, claim_id) in enumerate(drafted.aliases.claims.items()):
            alignment = drafted.alignments[alias]
            subquestion = (
                sentence_subquestion[alignment.sentence_index]
                if alignment.sentence_index is not None
                else None
            )
            records.append(
                ClaimRecord(
                    alias=alias,
                    claim_id=claim_id,
                    text=next(row["text"] for row in rows if row["claim_id"] == claim_id),
                    markers=markers.get(claim_id, frozenset()),
                    important=getattr(importance.get(claim_id), "value", "") == "high",
                    subquestion=subquestion,
                    is_core=subquestion in core,
                    priority_rank=position,
                    alignment_status=alignment.status,
                    sentence_index=alignment.sentence_index,
                    alignment_score=alignment.score,
                )
            )

        llm = self._budgeted(budget, "verification")
        verifier = Verifier(
            settings=self.settings,
            research=self.research,
            store=self.store,
            llm=llm,
            store_factory=self._store_factory,
            embedder=embedder,
        )
        try:
            await verifier.verify_pool(run_id, records, cap, warnings)
            await verifier.check_citations(
                records, sentence_evidence, drafted.aliases, evidence.chunks, warnings
            )
        finally:
            await self._close(llm)
        verifier.finalize_claims(records)

        entities = prepared.plan.analysis.key_entities if prepared.plan.analysis else []
        findings = quantitative_findings(
            records,
            verifier._state,
            drafted.aliases,
            evidence.chunks,
            embedder,
            entities=entities,
            approx_rel=self.settings.REPORT_QUALITY_QUANT_APPROX_REL_TOLERANCE,
            group_cosine=self.settings.REPORT_QUALITY_QUANT_GROUP_MIN_COSINE,
        )
        assessments = subquestion_assessments(evidence, records)
        conflicts = conflict_records(
            self.store.conflicts_for_run(run_id), drafted.aliases, evidence.profiles
        )
        not_evaluated = [r.alias for r in records if r.pool_status.value == "not_evaluated"]
        if not_evaluated:
            warnings.append(f"{len(not_evaluated)} claim(s) not evaluated: {not_evaluated}")
        self.store.update_audit(
            drafted.report_id,
            stage="verified",
            payload={
                "verification": [r.as_record() for r in records],
                "claim_cap": cap,
                "quantitative_findings": findings,
                "subquestion_assessments": assessments,
                "conflicts": conflicts,
                "budget": budget.as_record(),
                "warnings": warnings,
            },
        )
        logger.info(
            "Run %s verified: %d claims, %d not evaluated, %d findings",
            run_id,
            len(records),
            len(not_evaluated),
            len(findings),
        )
        return VerificationOutcome(
            run_id,
            drafted.report_id,
            records,
            findings,
            assessments,
            conflicts,
            budget.as_record(),
            warnings,
        )

    # --- stages 10b, 11, 12 (stage 33, S5b) --------------------------------------------

    async def finalize(
        self,
        prepared: EvidencePreparation,
        drafted: DraftPreparation,
        verified: VerificationOutcome,
    ) -> FinalOutcome:
        """Revise if needed, validate, re-verify changed sentences, validate again,
        render and persist. A finalization that cannot produce an honest report ends
        `finalization_failed` with the draft kept; an unexpected error fails the run."""
        if not self.settings.REPORT_QUALITY_ENABLED:
            raise ReportQualityDisabled()
        try:
            return await self._finalize(prepared, drafted, verified)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "internal_error"
            message = exc.message if isinstance(exc, AppError) else str(exc)[:500]
            try:
                self.research.fail_run(prepared.research_id, code=code, message=message)
            except Exception:  # noqa: BLE001 - the original error must not be masked
                logger.exception("Could not record the failure of run %s", prepared.research_id)
            raise

    async def _finalize(
        self,
        prepared: EvidencePreparation,
        drafted: DraftPreparation,
        verified: VerificationOutcome,
    ) -> FinalOutcome:
        run_id, evidence, plan = prepared.research_id, prepared.evidence, prepared.plan
        budget = prepared.budget or RunBudget.for_depth(self.settings, prepared.depth)
        prepared.budget = budget
        embedder = self._embedder or get_default_embedder()
        warnings = list(verified.warnings)
        records = verified.claims
        draft_tokens = getattr(
            self.settings, f"REPORT_QUALITY_DRAFT_MAX_TOKENS_{prepared.depth.upper()}"
        )

        llm = self._budgeted(budget, "revision")
        finalizer = Finalizer(
            settings=self.settings,
            research=self.research,
            store=self.store,
            llm=llm,
            embedder=embedder,
        )
        reverified_audit: dict[str, dict] = {}
        try:
            final, revised, failure = await finalizer.revise(
                prepared.topic,
                plan,
                drafted.draft.report,
                records,
                max_tokens=draft_tokens,
                warnings=warnings,
            )
            budget.release("revision")
            validated = None
            if final is not None:
                first = finalizer.validate(final, drafted.aliases, records, plan)
                reverified: dict = {}
                if first.needs_reverification:
                    attempted = sum(
                        1 for r in records if r.pool_status is not PoolStatus.NOT_EVALUATED
                    )
                    reverified, reverified_audit = await finalizer.reverify(
                        drafted.report_id,
                        final,
                        first.needs_reverification,
                        records,
                        drafted.aliases,
                        evidence.chunks,
                        allowance=budget.max_claims - attempted,
                        warnings=warnings,
                    )
                validated = finalizer.validate(
                    final, drafted.aliases, records, plan, reverified=reverified
                )
        finally:
            await self._close(llm)

        if validated is None:
            status, reasons, markdown = "finalization_failed", [failure or "no revision"], None
            actions: list[dict] = []
            metrics: dict = {}
            placements: dict = {}
        else:
            if validated.needs_reverification:  # cannot happen: every key got a result
                raise RuntimeError("validation still awaits re-verification")
            status, reasons = validated.status, list(validated.reasons)
            actions = [a.__dict__ for a in validated.actions]
            metrics = dict(validated.metrics)
            placements = finalizer.record_placements(validated, records)
            markdown = None
            if status != "finalization_failed":
                conflicts = conflict_contexts(
                    self.store.conflicts_for_run(run_id), drafted.aliases, evidence.profiles
                )
                markdown = finalizer.render(
                    validated, evidence, plan, verified.subquestions, conflicts
                )
                self.research.record_final_report(
                    run_id, final_markdown=markdown, model=drafted.draft.model
                )

        self.research.advance_status(run_id, RunStatus.COMPLETED)
        self.store.update_audit(
            drafted.report_id,
            stage="finalized",
            quality_status=terminal_status(status),
            payload={
                "finalization": {
                    "status": status,
                    "reasons": reasons,
                    "revised": revised,
                    "revision_prompt_version": FINAL_REPORT_PROMPT_VERSION if revised else None,
                    "revision": final.model_dump() if final is not None else None,
                    "actions": actions,
                    "metrics": metrics,
                    "placements": placements,
                    "reverified": reverified_audit,
                },
                "budget": budget.as_record(),
                "warnings": warnings,
            },
        )
        logger.info("Run %s finalized: %s (%s)", run_id, status, "; ".join(reasons) or "ok")
        return FinalOutcome(
            status, reasons, markdown, revised, reverified_audit, metrics, actions, warnings
        )
