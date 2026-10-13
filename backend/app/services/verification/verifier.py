"""
Claim verification, citation validation and conflict detection, per claim:

    claim.text ──vector search──▶ top-k chunks ──LLM──▶ ClaimVerificationModel
               ──enforce quotes──▶ Evidence rows + VerificationResult
               ──structurally───▶ Citation outcomes (valid / misattributed)
               ──if reported────▶ Conflict row

The verifier's output is never taken at its word:

- **Quotes are checked.** A supports/contradicts relation whose quote is not a
  verbatim substring of its passage is downgraded to neutral and counted. The
  repository then applies the documented downgrade: an evidence-bearing verdict
  left with no quoted support becomes `not_enough_evidence`, `downgraded=True`.
- **Citation validity is structural.** A citation is `valid` only when a supporting
  passage belongs to the cited source. That the URL resolves, the paper exists, or
  the source is topical counts for nothing — the spec's rule, enforced in
  `record_claim_verification`, not judged by the model.
- **Conflicts must point at real disagreement.** A reported conflict whose named
  passages do not hold enforced supports/contradicts relations is dropped and
  warned about. Descriptions state what each passage says; the prompt and schema
  both forbid inventing a reason the sources disagree.

Per-claim failures do not sink the run: a claim whose extraction cannot be parsed
after one retry is skipped and reported, and re-POSTing the endpoint verifies
exactly the stragglers (claims are selected by the absence of a verdict).
"""

import asyncio
import logging
import time
import uuid
from collections import Counter

from app.core.config import Settings, get_settings
from app.core.constants import EvidenceRelation, ResearchMode, VerificationVerdict
from app.core.exceptions import NotFoundError
from app.schemas.evidence import (
    ClaimVerificationRead,
    DetectedConflictRead,
    VerificationRunResponse,
)
from app.services.llm import LLMResponseInvalid, LLMService
from app.services.research_service import ResearchService
from app.services.vectorstore import RetrievedChunk, VectorStore, get_default_embedder
from app.services.verification.exceptions import NothingToVerify
from app.services.verification.models import ClaimVerificationModel, ConflictReport
from app.services.verification.prompts import (
    VERIFICATION_PROMPT_VERSION,
    VERIFICATION_SYSTEM,
    build_verification_prompt,
)

logger = logging.getLogger(__name__)

#: Passages retrieved per claim. Four is enough to catch support and disagreement
#: without burying the verifier in near-duplicates from overlapping chunks.
TOP_K_PER_CLAIM = 4
#: Concurrent LLM verifications. The SDK handles its own rate limiting; this bound
#: keeps one run from monopolising the account.
MAX_CONCURRENCY = 4


class ClaimVerifier:
    """Verifies one run's extracted claims against its own retrieved evidence."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        research_service: ResearchService | None = None,
        store_factory=None,
        llm: LLMService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.research = research_service or ResearchService(settings=self.settings)
        self._store_factory = store_factory
        self._llm = llm

    async def verify(self, research_id: uuid.UUID) -> VerificationRunResponse:
        started = time.perf_counter()
        run = self.research.get_run(research_id)
        if run.report is None:
            raise NotFoundError(
                f"Run {research_id} has no report.", details={"run_id": str(research_id)}
            )
        mode = run.configuration.mode

        report_id, claim_rows = self.research.claims_awaiting_verification(research_id)
        if not claim_rows:
            total = self.research.run_summary(research_id)["claim_count"]
            raise NothingToVerify(
                "No claims have been extracted yet; run claim extraction first."
                if total == 0
                else "Every claim of this run is already verified.",
                details={"run_id": str(research_id), "claims_total": total},
            )

        store = self._evidence_store(research_id, mode)
        llm = self._llm or LLMService(settings=self.settings)
        warnings: list[str] = []
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def verify_one(row: dict) -> dict | None:
            async with semaphore:
                return await self._verify_claim(research_id, row, store, llm, warnings)

        try:
            outcomes = await asyncio.gather(*(verify_one(row) for row in claim_rows))
        finally:
            if self._llm is None:
                await llm.aclose()

        results = [outcome for outcome in outcomes if outcome is not None]
        skipped = len(outcomes) - len(results)

        citation_statuses = self.research.citation_status_summary(research_id)
        verdict_counts = Counter(result["verdict"].value for result in results)
        reads = [self._read_for(result) for result in results]
        assessment_counts = Counter(read.citation_assessment for read in reads)
        conflicts = [
            DetectedConflictRead(
                conflict_id=result["conflict_id"],
                claim_id=result["claim_id"],
                kind=result["conflict_kind"],
                conflict_type=result["conflict_type"],
                description=result["conflict_description"],
            )
            for result in results
            if result["conflict_id"] is not None
        ]

        logger.info(
            "Verified %d/%d claims for run %s: %s; %d conflicts",
            len(results),
            len(outcomes),
            research_id,
            dict(verdict_counts),
            len(conflicts),
        )
        return VerificationRunResponse(
            research_id=research_id,
            report_id=report_id,
            claims_verified=len(results),
            claims_skipped=skipped,
            verdicts=dict(verdict_counts),
            citation_statuses=citation_statuses,
            assessments=dict(assessment_counts),
            conflicts_detected=len(conflicts),
            conflicts=conflicts,
            results=reads,
            verifier_model=results[0]["verifier_model"] if results else "none",
            prompt_version=VERIFICATION_PROMPT_VERSION,
            llm_calls=llm.calls_made,
            token_usage=(
                llm.total_usage.model_dump() if llm.total_usage.total_tokens else None
            ),
            duration_seconds=round(time.perf_counter() - started, 3),
            warnings=warnings,
        )

    # --- one claim ---------------------------------------------------------------

    async def _verify_claim(
        self,
        run_id: uuid.UUID,
        row: dict,
        store: VectorStore | None,
        llm: LLMService,
        warnings: list[str],
    ) -> dict | None:
        claim_id, claim_text = row["claim_id"], row["text"]
        hits = (
            await asyncio.to_thread(store.search, claim_text, TOP_K_PER_CLAIM)
            if store is not None
            else []
        )

        if not hits:
            # Nothing retrieved for this claim at all: recorded as such, with no
            # model call — there is nothing a verifier could judge from.
            stored = self.research.record_claim_verification(
                run_id,
                claim_id,
                passages=[],
                verdict=VerificationVerdict.NOT_ENOUGH_EVIDENCE,
                confidence=None,
                rationale="No evidence passages were retrieved for this claim.",
                supporting_passage_chunk_id=None,
                verifier_model=None,
                prompt_version=VERIFICATION_PROMPT_VERSION,
            )
            return self._result(row, stored, verifier_model="none")

        labels = {f"P{index + 1}": hit for index, hit in enumerate(hits)}
        prompt = build_verification_prompt(
            claim_text,
            [
                (label, hit.source.title or hit.source.url or "untitled", hit.text)
                for label, hit in labels.items()
            ],
        )

        try:
            try:
                response = await llm.generate_structured_output(
                    prompt, ClaimVerificationModel, system=VERIFICATION_SYSTEM
                )
            except LLMResponseInvalid:
                response = await llm.generate_structured_output(
                    prompt + "\n\nReturn ONLY the structured object, every field present.",
                    ClaimVerificationModel,
                    system=VERIFICATION_SYSTEM,
                )
        except LLMResponseInvalid:
            warnings.append(
                f"Claim {claim_id} skipped: the verifier's output failed validation twice. "
                "Re-run verification to retry it."
            )
            return None

        verification = response.data
        relations = self._enforced_relations(verification, labels, claim_id, warnings)

        passages = []
        for label, hit in labels.items():
            relation, quote = relations[label]
            quote_start = quote_end = None
            if quote is not None:
                index = hit.text.find(quote)
                if index >= 0:
                    quote_start, quote_end = index, index + len(quote)
            passages.append(
                {
                    "chunk_id": hit.chunk_id,
                    "relation": relation,
                    "similarity": max(0.0, min(1.0, hit.similarity)),
                    "rank": hit.rank,
                    "quoted_text": quote,
                    "quote_start_char": quote_start,
                    "quote_end_char": quote_end,
                }
            )

        supporting_chunk = self._verdict_anchor(verification.verdict, relations, labels)
        conflict = self._checked_conflict(
            verification.conflict, relations, labels, claim_id, warnings
        )

        stored = self.research.record_claim_verification(
            run_id,
            claim_id,
            passages=passages,
            verdict=verification.verdict,
            confidence=verification.confidence,
            rationale=verification.rationale,
            supporting_passage_chunk_id=supporting_chunk,
            conflict=conflict,
            verifier_model=response.model,
            prompt_version=VERIFICATION_PROMPT_VERSION,
        )
        return self._result(
            row,
            stored,
            verifier_model=response.model,
            conflict_report=verification.conflict if conflict is not None else None,
        )

    # --- enforcement -------------------------------------------------------------

    @staticmethod
    def _enforced_relations(
        verification: ClaimVerificationModel,
        labels: dict[str, RetrievedChunk],
        claim_id: uuid.UUID,
        warnings: list[str],
    ) -> dict[str, tuple[EvidenceRelation, str | None]]:
        """The verifier's relations, after the quote check. Passages the verifier
        did not assess stay neutral; assessments of unknown labels are dropped."""
        relations: dict[str, tuple[EvidenceRelation, str | None]] = {
            label: (EvidenceRelation.NEUTRAL, None) for label in labels
        }
        for assessment in verification.passages:
            hit = labels.get(assessment.passage)
            if hit is None:
                warnings.append(
                    f"Claim {claim_id}: the verifier assessed unknown passage "
                    f"{assessment.passage!r}; dropped."
                )
                continue
            if assessment.relation is EvidenceRelation.NEUTRAL:
                relations[assessment.passage] = (EvidenceRelation.NEUTRAL, None)
                continue
            quote = (assessment.quote or "").strip()
            if quote and quote in hit.text:
                relations[assessment.passage] = (assessment.relation, quote)
            else:
                warnings.append(
                    f"Claim {claim_id}: the verifier's quote for "
                    f"{assessment.passage} is not verbatim in the passage; relation "
                    f"downgraded from {assessment.relation.value} to neutral."
                )
        return relations

    @staticmethod
    def _verdict_anchor(
        verdict: VerificationVerdict,
        relations: dict[str, tuple[EvidenceRelation, str | None]],
        labels: dict[str, RetrievedChunk],
    ) -> uuid.UUID | None:
        """The chunk the verdict rests on: the first enforced supporting passage for
        a support verdict, the first contradicting one for `contradicted`. None when
        nothing survived enforcement — the repository downgrade then applies."""
        wanted = {
            VerificationVerdict.SUPPORTED: EvidenceRelation.SUPPORTS,
            VerificationVerdict.PARTIALLY_SUPPORTED: EvidenceRelation.SUPPORTS,
            VerificationVerdict.CONTRADICTED: EvidenceRelation.CONTRADICTS,
        }.get(verdict)
        if wanted is None:
            return None
        for label, (relation, _) in relations.items():
            if relation is wanted:
                return labels[label].chunk_id
        return None

    @staticmethod
    def _checked_conflict(
        report: ConflictReport | None,
        relations: dict[str, tuple[EvidenceRelation, str | None]],
        labels: dict[str, RetrievedChunk],
        claim_id: uuid.UUID,
        warnings: list[str],
    ) -> dict | None:
        """A conflict the enforced relations actually exhibit, or nothing."""
        if report is None:
            return None
        supporting = labels.get(report.supporting_passage)
        contradicting = labels.get(report.contradicting_passage)
        if (
            supporting is None
            or contradicting is None
            or relations[report.supporting_passage][0] is not EvidenceRelation.SUPPORTS
            or relations[report.contradicting_passage][0] is not EvidenceRelation.CONTRADICTS
        ):
            warnings.append(
                f"Claim {claim_id}: a reported conflict did not match the enforced "
                "passage relations; dropped."
            )
            return None
        return {
            "supporting_chunk_id": supporting.chunk_id,
            "contradicting_chunk_id": contradicting.chunk_id,
            "conflict_type": report.conflict_type,
            "description": report.description,
        }

    # --- response shaping ----------------------------------------------------------

    @staticmethod
    def _result(
        row: dict, stored: dict, *, verifier_model: str, conflict_report=None
    ) -> dict:
        return {
            **stored,
            "claim_text": row["text"],
            "requires_citation": row["requires_citation"],
            "verifier_model": verifier_model,
            "conflict_kind": conflict_report.kind if conflict_report else None,
            "conflict_type": conflict_report.conflict_type if conflict_report else None,
            "conflict_description": conflict_report.description if conflict_report else None,
        }

    @staticmethod
    def _read_for(result: dict) -> ClaimVerificationRead:
        """Derive the spec's five-way citation assessment from stored primitives."""
        citations: dict[str, str] = result["citations"]
        if result["conflict_id"] is not None:
            assessment = "conflicting"
        elif not result["requires_citation"]:
            assessment = "not_required"
        elif not citations:
            assessment = "missing"
        elif "valid" in citations.values():
            assessment = (
                "partially_valid"
                if result["verdict"] is VerificationVerdict.PARTIALLY_SUPPORTED
                or any(status != "valid" for status in citations.values())
                else "valid"
            )
        else:
            assessment = "invalid"
        return ClaimVerificationRead(
            claim_id=result["claim_id"],
            claim_text=result["claim_text"],
            verdict=result["verdict"],
            confidence=result["confidence"],
            downgraded=result["downgraded"],
            rationale=result["rationale"],
            citation_assessment=assessment,
            citations=citations,
            evidence_count=result["evidence_count"],
            conflict_id=result["conflict_id"],
        )

    def _evidence_store(self, research_id: uuid.UUID, mode: ResearchMode) -> VectorStore | None:
        """The store claims are verified against, built per mode.

        SEARCH_GROUNDED runs usually have their per-run collection on disk; HYBRID
        runs never built one. Either way, an empty collection is rebuilt on demand
        from the persisted chunk rows — the ids are the same content-derived UUIDv5s,
        so evidence rows resolve identically. MODEL_ONLY retrieved nothing, so there
        is nothing to verify against and every claim takes the no-evidence path;
        that emptiness is the baseline's measured property, not an error.
        """
        if mode is ResearchMode.MODEL_ONLY:
            return None
        store = self._store_for(f"run_{research_id.hex}")
        if store.count() == 0:
            chunks = self.research.persisted_chunks_for_run(research_id)
            if chunks:
                store.index_chunks(chunks)
                logger.info(
                    "Rebuilt collection for run %s from %d stored chunks",
                    research_id,
                    len(chunks),
                )
        return store

    def _store_for(self, collection: str) -> VectorStore:
        if self._store_factory is not None:
            return self._store_factory(collection)
        return VectorStore(embedder=get_default_embedder(), collection_name=collection)
