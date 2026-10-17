"""
Revision, re-verification, final validation and finalization — design stages 10b,
11 and 12 (stage 33, S5b; docs/REPORT_QUALITY_PIPELINE.md §5).

    verified claims ──revision needed?──no──▶ the draft, tagged with its aligned claims
                    └──yes──▶ LLM (final-report-structured-v1, budgeted, one retry)
    ──validate (V1–V7)──▶ sentences whose meaning changed vs their claims
    ──re-verify each against ONLY the passages its final sentence cites──▶ results
    ──validate again with those results──▶ final placement, markers, outcome
    ──render (code-owned markers) + References (legacy formatter)──▶ final report
    ──persist: final_markdown, final_claim_check, claim_audit placements, audit

Rules that hold on every path:

- **No loop.** At most one revision call (plus one retry on invalid output) and one
  re-verification per flagged sentence; the second validation pass never revises.
- **Re-verification is exact-citation only** — the verification-v2 prompt over the
  sentence's own cited passages (the citation-check procedure). A prior verdict is
  never carried forward for a sentence whose meaning changed; a sentence that could
  not be re-verified (budget, deadline, claim reserve, malformed output, provider
  failure, no cited passage) stays out of the body as not evaluated / insufficient.
- **Nothing outdated is restored.** V7 restores only claims no revised sentence states.
- **Markers only from support.** Validation renders an `[S#]` only for a passage whose
  chunk is established as supporting the sentence's claim (S5a citation checks) or the
  re-verified sentence.
- **Outcome is the validator's**, recorded as is: complete, degraded or
  finalization_failed. A revision that could not be produced (invalid twice, budget,
  deadline, provider failure) is a finalization failure — the draft stays, nothing is
  converted into support.
"""

import uuid
from dataclasses import dataclass, field

from app.core.constants import (
    CitationOutcome,
    ClaimStatus,
    NotEvaluatedReason,
    Placement,
    PoolStatus,
    ReportQualityStatus,
)
from app.core.exceptions import ExternalServiceError
from app.services.llm import LLMResponseInvalid
from app.services.orchestration.finalizer import ReportFinalizer
from app.services.quality.assessment import classify_claim
from app.services.quality.budget import BudgetedLLM, BudgetExhausted
from app.services.quality.drafting import _validated
from app.services.quality.models import (
    ReportSectionDraft,
    ReportSentence,
    StructuredReport,
    SubquestionAssessment,
)
from app.services.quality.prompts import (
    FINAL_REPORT_PROMPT_VERSION,
    FINAL_REPORT_RETRY_INVALID,
    FINAL_REPORT_SYSTEM,
    build_final_report_prompt,
)
from app.services.quality.rendering import render_final
from app.services.quality.store import ClaimAuditRecord, FinalClaimCheckRecord
from app.services.quality.validation import (
    ClaimInfo,
    Reverification,
    ValidatedReport,
    ValidationConfig,
    validate_final,
)
from app.services.quality.verification import (
    CITATION_CHECK_VERSION,
    RETRY_SUFFIX,
    ClaimRecord,
    passage_outcome,
)
from app.services.verification import ClaimVerifier
from app.services.verification.models import ClaimVerificationModel
from app.services.verification.prompts import VERIFICATION_SYSTEM, build_verification_prompt

_SUPPORTING = {CitationOutcome.SUPPORTS, CitationOutcome.PARTIALLY_SUPPORTS}
_STATUS_WORD = {
    ClaimStatus.SUPPORTED_BY_CITED_SOURCE: "SUPPORTED",
    ClaimStatus.CITATION_NOT_CONFIRMED: "CITATION_NOT_CONFIRMED",
    ClaimStatus.CONTESTED: "CONTESTED",
    ClaimStatus.INSUFFICIENT_EVIDENCE: "INSUFFICIENT",
    ClaimStatus.NOT_EVALUATED: "NOT_EVALUATED",
}
_TERMINAL = {
    "complete": ReportQualityStatus.COMPLETE,
    "degraded": ReportQualityStatus.DEGRADED,
    "finalization_failed": ReportQualityStatus.FINALIZATION_FAILED,
}


@dataclass
class FinalOutcome:
    status: str  # "complete" | "degraded" | "finalization_failed"
    reasons: list[str]
    final_markdown: str | None
    revised: bool
    reverified: dict[str, dict]
    metrics: dict
    actions: list[dict]
    warnings: list[str] = field(default_factory=list)


def supported_chunks(record: ClaimRecord, aliases) -> frozenset[uuid.UUID]:
    """Chunks established as supporting this claim: its CITED passages whose citation
    check (or reused exact-chunk pool relation) supports it. Nothing else."""
    return frozenset(
        aliases.passages[alias].chunk_id
        for alias, outcome in record.outcomes.items()
        if outcome in _SUPPORTING
    )


def claim_infos(records: list[ClaimRecord], aliases) -> dict[str, ClaimInfo]:
    return {
        r.alias: ClaimInfo(
            alias=r.alias,
            text=r.text,
            importance="high" if r.important else "medium",
            subquestion=r.subquestion,
            is_core=r.is_core,
            status=r.claim_status,
            pool=r.pool_status,
            supported_chunks=supported_chunks(r, aliases),
        )
        for r in records
    }


def revision_needed(records: list[ClaimRecord]) -> bool:
    """Revise only when some claim cannot stand as a cited finding as drafted."""
    return any(r.claim_status is not ClaimStatus.SUPPORTED_BY_CITED_SOURCE for r in records)


def draft_as_final(draft: StructuredReport, records: list[ClaimRecord]) -> StructuredReport:
    """The draft, each sentence tagged with the claims aligned to it — the no-revision
    path. It still goes through full validation."""
    by_sentence: dict[int, list[str]] = {}
    for r in records:
        if r.alignment_status == "aligned" and r.sentence_index is not None:
            by_sentence.setdefault(r.sentence_index, []).append(r.alias)
    index = 0
    sections = []
    for section in draft.sections:
        sentences = []
        for sentence in section.sentences:
            sentences.append(sentence.model_copy(update={"claim_ids": by_sentence.get(index, [])}))
            index += 1
        sections.append(
            ReportSectionDraft(
                subquestion=section.subquestion, heading=section.heading, sentences=sentences
            )
        )
    return StructuredReport(title=draft.title, sections=sections)


def revision_prompt(topic, plan, draft: StructuredReport, records: list[ClaimRecord]) -> str:
    subquestions = [f"{s.alias}: {s.text}" for s in plan.subquestions]
    draft_lines = [
        f"{section.subquestion} | {sentence.text}"
        for section in draft.sections
        for sentence in section.sentences
    ]
    claim_lines = []
    for r in records:
        status = _STATUS_WORD[r.claim_status]
        if (
            r.claim_status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE
            and r.pool_status is PoolStatus.PARTIALLY_SUPPORTED
        ):
            status = "PARTIAL"
        passages = [a for a, o in r.outcomes.items() if o in _SUPPORTING]
        claim_lines.append(f"{r.alias} | {status} | {', '.join(passages) or '-'} | {r.text}")
    return build_final_report_prompt(topic, draft_lines, claim_lines, subquestions)


class Finalizer:
    def __init__(self, *, settings, research, store, llm: BudgetedLLM, embedder) -> None:
        self.settings = settings
        self.research = research
        self.store = store
        self.llm = llm
        self.embedder = embedder

    # --- 10b: revision ------------------------------------------------------------

    async def revise(self, topic, plan, draft, records, *, max_tokens, warnings):
        """(structured final report or None, revised?, failure reason or None)."""
        if not revision_needed(records):
            return draft_as_final(draft, records), False, None
        self.llm.stage = "revision"
        prompt = revision_prompt(topic, plan, draft, records)
        for suffix in ("", FINAL_REPORT_RETRY_INVALID):
            try:
                response = await self.llm.generate_structured_output(
                    prompt + suffix,
                    StructuredReport,
                    system=FINAL_REPORT_SYSTEM,
                    max_tokens=max_tokens,
                )
            except LLMResponseInvalid as exc:
                warnings.append(f"Revision output invalid: {exc.message}")
                continue
            except BudgetExhausted as exc:
                return None, True, f"revision not produced: budget ({exc.reason})"
            except ExternalServiceError as exc:
                return None, True, f"revision not produced: provider failure ({exc.message})"
            report = _validated(response.data, plan, warnings, allow_claims=True)
            return report, True, None
        return None, True, "revision not produced: invalid output twice"

    # --- 11: re-verification of changed sentences ----------------------------------------

    async def reverify(
        self,
        report_id,
        final: StructuredReport,
        keys,
        records,
        aliases,
        chunks,
        allowance: int,
        warnings: list[str],
    ):
        """Re-verify each flagged sentence against ONLY its cited passages. Returns
        validation inputs and audit records; every outcome is persisted."""
        self.llm.stage = "reverification"
        alias_to_claim = {r.alias: r for r in records}
        results: dict[tuple[int, int], Reverification] = {}
        audit: dict[str, dict] = {}
        stopped: NotEvaluatedReason | None = None
        for key in keys:
            sentence: ReportSentence = final.sections[key[0]].sentences[key[1]]
            cited = [a for a in sentence.evidence_ids if a in aliases.passages]
            cited_chunks = [aliases.passages[a].chunk_id for a in cited]
            derived = next(
                (alias_to_claim[c].claim_id for c in sentence.claim_ids if c in alias_to_claim),
                None,
            )
            base = dict(
                section_index=key[0],
                sentence_index=key[1],
                text=sentence.text,
                change_kinds=["meaning_changed"],
                cited_chunk_ids=cited_chunks,
                derived_from_claim_id=derived,
            )
            self.store.record_final_claim_check(
                report_id,
                FinalClaimCheckRecord(
                    **base,
                    pool_status=PoolStatus.NOT_EVALUATED,
                    claim_status=ClaimStatus.NOT_EVALUATED,
                    not_evaluated_reason=NotEvaluatedReason.AWAITING_REVERIFICATION,
                ),
            )

            reason: NotEvaluatedReason | None = None
            if stopped is not None:
                reason = stopped
            elif allowance <= 0:
                reason = NotEvaluatedReason.CLAIM_CAP
            elif self.llm.budget.expired():
                reason = stopped = NotEvaluatedReason.DEADLINE

            if reason is None and not cited:
                # Nothing cited: there is nothing a cited-source check could judge.
                record = FinalClaimCheckRecord(
                    **base,
                    pool_status=PoolStatus.NOT_ENOUGH_EVIDENCE,
                    claim_status=ClaimStatus.INSUFFICIENT_EVIDENCE,
                    prompt_version=CITATION_CHECK_VERSION,
                )
                self.store.record_final_claim_check(report_id, record)
                results[key] = Reverification(
                    ClaimStatus.INSUFFICIENT_EVIDENCE, PoolStatus.NOT_ENOUGH_EVIDENCE, frozenset()
                )
                audit[f"{key[0]}.{key[1]}"] = {
                    "status": "insufficient_evidence",
                    "reason": "no cited passage",
                }
                continue

            if reason is None:
                allowance -= 1
                try:
                    response = await self._judge(sentence.text, cited, chunks)
                except BudgetExhausted as exc:
                    reason = stopped = (
                        NotEvaluatedReason.DEADLINE
                        if exc.reason == "deadline"
                        else NotEvaluatedReason.BUDGET
                    )
                except (LLMResponseInvalid, ExternalServiceError) as exc:
                    reason = NotEvaluatedReason.TECHNICAL
                    warnings.append(f"Re-verification failed for sentence {key}: {exc.message}")

            if reason is not None:
                self.store.record_final_claim_check(
                    report_id,
                    FinalClaimCheckRecord(
                        **base,
                        pool_status=PoolStatus.NOT_EVALUATED,
                        claim_status=ClaimStatus.NOT_EVALUATED,
                        not_evaluated_reason=reason,
                    ),
                )
                results[key] = Reverification(
                    ClaimStatus.NOT_EVALUATED, PoolStatus.NOT_EVALUATED, frozenset()
                )
                audit[f"{key[0]}.{key[1]}"] = {"status": "not_evaluated", "reason": reason.value}
                continue

            verification = response.data
            labels = {f"P{i + 1}": chunks[a] for i, a in enumerate(cited)}
            relations = ClaimVerifier._enforced_relations(
                verification,
                labels,
                derived or uuid.uuid4(),
                warnings,  # type: ignore[arg-type]
            )
            verdict = PoolStatus(verification.verdict.value)
            outcomes = {
                a: passage_outcome(relations[f"P{i + 1}"][0], verdict) for i, a in enumerate(cited)
            }
            supported = frozenset(
                aliases.passages[a].chunk_id for a, o in outcomes.items() if o in _SUPPORTING
            )
            status = classify_claim(verdict, outcomes.values())
            self.store.record_final_claim_check(
                report_id,
                FinalClaimCheckRecord(
                    **base,
                    pool_status=verdict,
                    claim_status=status,
                    supported_chunk_ids=sorted(supported, key=str),
                    evidence={
                        "outcomes": {a: o.value for a, o in outcomes.items()},
                        "rationale": verification.rationale,
                    },
                    model=response.model,
                    prompt_version=CITATION_CHECK_VERSION,
                ),
            )
            results[key] = Reverification(status, verdict, supported)
            audit[f"{key[0]}.{key[1]}"] = {
                "status": status.value,
                "verdict": verdict.value,
                "outcomes": {a: o.value for a, o in outcomes.items()},
            }
        return results, audit

    async def _judge(self, text: str, cited: list[str], chunks):
        prompt = build_verification_prompt(
            text,
            [
                (
                    f"P{i + 1}",
                    chunks[a].source.title or chunks[a].source.url or "untitled",
                    chunks[a].text,
                )
                for i, a in enumerate(cited)
            ],
        )
        try:
            return await self.llm.generate_structured_output(
                prompt, ClaimVerificationModel, system=VERIFICATION_SYSTEM
            )
        except LLMResponseInvalid:
            return await self.llm.generate_structured_output(
                prompt + RETRY_SUFFIX, ClaimVerificationModel, system=VERIFICATION_SYSTEM
            )

    # --- 11 + 12: validate, render, persist ------------------------------------------------

    def validate(self, final, aliases, records, plan, reverified=None) -> ValidatedReport:
        evaluated = [r for r in records if r.pool_status is not PoolStatus.NOT_EVALUATED]
        coverage = len(evaluated) / len(records) if records else 1.0
        return validate_final(
            final,
            aliases,
            claim_infos(records, aliases),
            config=ValidationConfig.from_settings(self.settings),
            core_subquestions=frozenset(s.alias for s in plan.subquestions if s.is_core),
            verification_coverage=coverage,
            entities=plan.analysis.key_entities if plan.analysis else (),
            embed=self.embedder.embed_documents,
            reverified=reverified,
        )

    def render(self, validated, evidence, plan, assessments, conflicts) -> str:
        body = render_final(
            validated,
            assessments=[SubquestionAssessment(**a) for a in assessments],
            subquestion_texts={s.alias: s.text for s in plan.subquestions},
            conflicts=conflicts,
        )
        markers = {m for s in validated.sections for p in s.sentences for m in p.markers}
        sources: list[dict] = []
        for chunk in evidence.chunks.values():
            label = evidence.labels[chunk.source.fingerprint]
            if label in markers and label not in {s["label"] for s in sources}:
                meta = chunk.source
                sources.append(
                    {
                        "label": label,
                        "title": meta.title,
                        "url": meta.url,
                        "doi": meta.doi,
                        "venue": meta.venue,
                        "year": meta.year,
                    }
                )
        return body.rstrip() + ReportFinalizer._references_section(sources, markers) + "\n"

    def record_placements(self, validated: ValidatedReport, records: list[ClaimRecord]) -> dict:
        placement: dict[str, Placement] = {}
        for section in validated.sections:
            for sentence in section.sentences:
                for alias in sentence.claim_aliases:
                    placement.setdefault(alias, sentence.placement)
        for where, sentences in validated.labelled.items():
            for sentence in sentences:
                for alias in sentence.claim_aliases:
                    placement.setdefault(alias, where)
        for r in records:
            self.store.record_claim_audit(
                ClaimAuditRecord(
                    claim_id=r.claim_id,
                    pool_status=r.pool_status,
                    not_evaluated_reason=r.not_evaluated_reason,
                    claim_status=r.claim_status,
                    alignment_status=r.alignment_status,
                    aligned_sentence_index=r.sentence_index,
                    alignment_score=r.alignment_score,
                    priority_rank=r.priority_rank,
                    final_placement=placement.get(r.alias),
                )
            )
        return {alias: p.value for alias, p in placement.items()}


def terminal_status(status: str) -> ReportQualityStatus:
    return _TERMINAL[status]


__all__ = ["FINAL_REPORT_PROMPT_VERSION", "FinalOutcome", "Finalizer", "terminal_status"]
