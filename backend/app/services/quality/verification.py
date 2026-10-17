"""
Claim verification and citation checking — design stages 7, 7b, 8 and 9
(stage 32, S5a; docs/REPORT_QUALITY_PIPELINE.md §1, §3, §4, §6).

Two results per claim, kept separate end to end:

- **Pool result (verification-v2, unchanged).** The legacy `ClaimVerifier._verify_claim`
  is called per claim — its retrieval over the whole run collection, prompt, quote
  enforcement, downgrade and persistence untouched — inside this module's own loop,
  so the claim cap, the deadline and the budget are checked between claims.
- **Citation result (citation-check-v1).** For a claim aligned to its draft sentence,
  the cited passages are exactly the passages that sentence names whose source is
  among the claim's own markers — what its rendered `[S#]` points to. Only those
  passages are judged, with the verification-v2 system prompt, schema and verbatim
  quote enforcement. Support from any other passage in the run can never validate
  the citation.

A cited passage's relation is reused from the pool result ONLY when verification-v2
recorded an enforced, verbatim-quoted relation on that exact chunk for that claim
(the store re-checks the evidence row). Outcomes are conservative: a passage
`supports` only with a verbatim supporting quote AND a supporting verdict; anything
less is `does_not_establish`. Missing evidence, skipped verification, malformed output
and budget exhaustion are recorded as such (`not_enough_evidence`, `not_evaluated`
with a reason) — never as support.

Quantitative findings and subquestion/conflict assessments are computed here as
audit-only records (no verdict changes, nothing in report text).
"""

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from app.core.constants import (
    CitationOutcome,
    ClaimStatus,
    EstablishedBy,
    EvidenceRelation,
    NotEvaluatedReason,
    PoolStatus,
    ResearchMode,
)
from app.core.exceptions import ExternalServiceError
from app.services.llm import LLMResponseInvalid, LLMService
from app.services.quality.assessment import (
    SubquestionInput,
    assess_subquestions,
    classify_claim,
    conflict_context,
)
from app.services.quality.budget import BudgetedLLM, BudgetExhausted
from app.services.quality.evidence import split_sentences
from app.services.quality.quantities import (
    TextPair,
    audit_quantities,
    claim_pairs_by_metric,
)
from app.services.quality.store import CitationCheckRecord, ClaimAuditRecord, QualityStore
from app.services.verification import ClaimVerifier
from app.services.verification.models import ClaimVerificationModel
from app.services.verification.prompts import VERIFICATION_SYSTEM, build_verification_prompt

CITATION_CHECK_VERSION = "citation-check-v1"
RETRY_SUFFIX = "\n\nReturn ONLY the structured object, every field present."

_BUDGET_REASON = {"deadline": NotEvaluatedReason.DEADLINE}


@dataclass
class ClaimRecord:
    alias: str
    claim_id: uuid.UUID
    text: str
    markers: frozenset[str]
    important: bool
    subquestion: str | None
    is_core: bool
    priority_rank: int
    alignment_status: str
    sentence_index: int | None
    alignment_score: float | None
    pool_status: PoolStatus = PoolStatus.NOT_EVALUATED
    not_evaluated_reason: NotEvaluatedReason | None = None
    cited: list[str] = field(default_factory=list)  # E# aliases the citation points to
    outcomes: dict[str, CitationOutcome] = field(default_factory=dict)
    citation_reason: NotEvaluatedReason | None = None
    claim_status: ClaimStatus = ClaimStatus.NOT_EVALUATED
    quotes: dict[str, str] = field(default_factory=dict)  # E# -> verbatim quote it rests on

    def as_record(self) -> dict:
        return {
            "alias": self.alias,
            "claim_id": str(self.claim_id),
            "priority_rank": self.priority_rank,
            "pool_status": self.pool_status.value,
            "not_evaluated_reason": self.not_evaluated_reason.value
            if self.not_evaluated_reason
            else None,
            "alignment": self.alignment_status,
            "cited": self.cited,
            "citation_outcomes": {k: v.value for k, v in self.outcomes.items()},
            "citation_quotes": dict(self.quotes),
            "citation_not_evaluated_reason": self.citation_reason.value
            if self.citation_reason
            else None,
            "claim_status": self.claim_status.value,
            "subquestion": self.subquestion,
        }


def priority_order(records: Sequence[ClaimRecord]) -> list[ClaimRecord]:
    """When the cap binds: high importance, then core subquestion, then reading order
    (design decision D8, recorded on every claim as `priority_rank`)."""
    ordered = sorted(records, key=lambda r: (not r.important, not r.is_core, r.priority_rank))
    for rank, record in enumerate(ordered):
        record.priority_rank = rank
    return ordered


def initial_claim_cap(max_claims: int, reserve_fraction: float) -> int:
    """Claims verified now; the reserve is held back for re-verifying rewritten
    sentences in the revision stage (design §6)."""
    return max(0, max_claims - math.ceil(reserve_fraction * max_claims))


def passage_outcome(relation: EvidenceRelation, verdict: PoolStatus) -> CitationOutcome:
    """Conservative: support needs a verbatim supporting quote AND a supporting verdict."""
    if relation is EvidenceRelation.SUPPORTS:
        if verdict is PoolStatus.SUPPORTED:
            return CitationOutcome.SUPPORTS
        if verdict is PoolStatus.PARTIALLY_SUPPORTED:
            return CitationOutcome.PARTIALLY_SUPPORTS
    if relation is EvidenceRelation.CONTRADICTS and verdict in (
        PoolStatus.CONTRADICTED,
        PoolStatus.PARTIALLY_SUPPORTED,
    ):
        return CitationOutcome.CONTRADICTS
    return CitationOutcome.DOES_NOT_ESTABLISH


def _budget_reason(exc: BudgetExhausted) -> NotEvaluatedReason:
    return _BUDGET_REASON.get(exc.reason, NotEvaluatedReason.BUDGET)


class Verifier:
    """Runs stages 7–9 for one drafted run. All writes go through `QualityStore`
    (claim audits, citation checks) or the legacy verifier (verification results)."""

    def __init__(
        self,
        *,
        settings,
        research,
        store: QualityStore,
        llm: BudgetedLLM,
        store_factory=None,
        embedder=None,
    ) -> None:
        self.settings = settings
        self.research = research
        self.store = store
        self.llm = llm
        # The budgeted wrapper is duck-compatible with LLMService for what the verifier
        # calls (generate_structured_output, calls_made, total_usage, aclose).
        self.legacy = ClaimVerifier(
            settings=settings,
            research_service=research,
            store_factory=store_factory,
            llm=cast(LLMService, llm),
        )
        self.embedder = embedder

    # --- stage 7: pool verification -------------------------------------------------

    async def verify_pool(
        self, run_id: uuid.UUID, records: list[ClaimRecord], cap: int, warnings: list[str]
    ) -> None:
        _, rows = self.research.claims_awaiting_verification(run_id)
        rows_by_id = {row["claim_id"]: row for row in rows}
        store = self.legacy._evidence_store(run_id, ResearchMode.SEARCH_GROUNDED)
        self.llm.stage = "verification"
        stopped: NotEvaluatedReason | None = None
        for record in priority_order(records):
            row = rows_by_id.get(record.claim_id)
            if row is None:  # already verified (never re-judged, never overwritten)
                continue
            if record.priority_rank >= cap:
                record.not_evaluated_reason = NotEvaluatedReason.CLAIM_CAP
                continue
            if stopped is None and self.llm.budget.expired():
                stopped = NotEvaluatedReason.DEADLINE
            if stopped is not None:
                record.not_evaluated_reason = stopped
                continue
            try:
                result = await self.legacy._verify_claim(
                    run_id, row, store, cast(LLMService, self.llm), warnings
                )
            except BudgetExhausted as exc:
                stopped = _budget_reason(exc)
                record.not_evaluated_reason = stopped
                warnings.append(f"Verification stopped at {record.alias}: {exc.message}")
                continue
            except ExternalServiceError as exc:
                record.not_evaluated_reason = NotEvaluatedReason.TECHNICAL
                warnings.append(f"{record.alias} not verified (technical): {exc.message}")
                continue
            if result is None:  # the verifier's output failed validation twice
                record.not_evaluated_reason = NotEvaluatedReason.TECHNICAL

        state = self.store.verification_state([r.claim_id for r in records])
        for record in records:
            verdict = state[record.claim_id]["verdict"]
            if verdict is not None:
                record.pool_status = PoolStatus(verdict.value)
                record.not_evaluated_reason = None
            else:
                record.pool_status = PoolStatus.NOT_EVALUATED
                record.not_evaluated_reason = (
                    record.not_evaluated_reason or NotEvaluatedReason.TECHNICAL
                )
        self._state = state

    # --- stage 7b: citation-check-v1 ------------------------------------------------

    async def check_citations(
        self,
        records: list[ClaimRecord],
        sentence_evidence: list[list[str]],
        aliases,
        chunks,
        warnings: list[str],
    ) -> None:
        self.llm.stage = "citation_check"
        stopped: NotEvaluatedReason | None = None
        for record in priority_order(records):
            if record.alignment_status != "aligned" or record.sentence_index is None:
                if record.markers:
                    record.citation_reason = NotEvaluatedReason.UNALIGNED
                continue
            record.cited = [
                alias
                for alias in sentence_evidence[record.sentence_index]
                if alias in aliases.passages
                and aliases.passages[alias].source_label in record.markers
            ]
            if not record.cited:
                continue
            if record.pool_status is PoolStatus.NOT_EVALUATED:
                self._record_not_evaluated(
                    record, record.not_evaluated_reason or NotEvaluatedReason.TECHNICAL, aliases
                )
                continue
            state = self._state[record.claim_id]
            pending = []
            for alias in record.cited:
                chunk_id = aliases.passages[alias].chunk_id
                relation, quote = state["relations"].get(chunk_id, (None, None))
                reused = (
                    passage_outcome(relation, record.pool_status)
                    if relation in (EvidenceRelation.SUPPORTS, EvidenceRelation.CONTRADICTS)
                    else None
                )
                if reused is not None and reused is not CitationOutcome.DOES_NOT_ESTABLISH:
                    self._write(
                        record,
                        alias,
                        aliases,
                        reused,
                        EstablishedBy.POOL_VERIFICATION,
                        quote=quote,
                        model=None,
                        rationale="reused verification-v2 relation on this exact chunk",
                    )
                else:
                    pending.append(alias)
            if not pending:
                continue
            if stopped is None and self.llm.budget.expired():
                stopped = NotEvaluatedReason.DEADLINE
            if stopped is not None:
                self._record_not_evaluated(record, stopped, aliases, only=pending)
                continue
            try:
                response = await self._judge(record, pending, chunks)
            except BudgetExhausted as exc:
                stopped = _budget_reason(exc)
                self._record_not_evaluated(record, stopped, aliases, only=pending)
                warnings.append(f"Citation checks stopped at {record.alias}: {exc.message}")
                continue
            except (LLMResponseInvalid, ExternalServiceError) as exc:
                self._record_not_evaluated(
                    record, NotEvaluatedReason.TECHNICAL, aliases, only=pending
                )
                warnings.append(f"{record.alias} citation check failed (technical): {exc.message}")
                continue
            verification = response.data
            labels = {f"P{i + 1}": chunks[alias] for i, alias in enumerate(pending)}
            relations = ClaimVerifier._enforced_relations(
                verification,
                labels,
                record.claim_id,
                warnings,  # type: ignore[arg-type]
            )
            verdict = PoolStatus(verification.verdict.value)
            for label, alias in zip(labels, pending, strict=True):
                relation, quote = relations[label]
                outcome = passage_outcome(relation, verdict)
                established = (
                    EstablishedBy.CITATION_CHECK
                    if outcome is not CitationOutcome.DOES_NOT_ESTABLISH
                    else None
                )
                self._write(
                    record,
                    alias,
                    aliases,
                    outcome,
                    established,
                    quote=quote if established else None,
                    model=response.model,
                    rationale=verification.rationale,
                )

    async def _judge(self, record: ClaimRecord, pending: list[str], chunks):
        prompt = build_verification_prompt(
            record.text,
            [
                (
                    f"P{i + 1}",
                    chunks[a].source.title or chunks[a].source.url or "untitled",
                    chunks[a].text,
                )
                for i, a in enumerate(pending)
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

    def _write(self, record, alias, aliases, outcome, established, *, quote, model, rationale):
        ref = aliases.passages[alias]
        self.store.record_citation_check(
            CitationCheckRecord(
                claim_id=record.claim_id,
                passage_alias=alias,
                cited_chunk_id=ref.chunk_id,
                outcome=outcome,
                procedure_version=CITATION_CHECK_VERSION,
                citation_id=self._state[record.claim_id]["citations"].get(ref.source_label),
                established_by=established,
                quoted_text=quote,
                rationale=rationale,
                model=model,
            )
        )
        record.outcomes[alias] = outcome
        if quote and outcome is not CitationOutcome.DOES_NOT_ESTABLISH:
            record.quotes[alias] = quote

    def _record_not_evaluated(self, record, reason, aliases, *, only=None):
        record.citation_reason = reason
        for alias in only if only is not None else record.cited:
            ref = aliases.passages[alias]
            self.store.record_citation_check(
                CitationCheckRecord(
                    claim_id=record.claim_id,
                    passage_alias=alias,
                    cited_chunk_id=ref.chunk_id,
                    outcome=CitationOutcome.NOT_EVALUATED,
                    procedure_version=CITATION_CHECK_VERSION,
                    citation_id=self._state.get(record.claim_id, {})
                    .get("citations", {})
                    .get(ref.source_label),
                    not_evaluated_reason=reason,
                )
            )
            record.outcomes[alias] = CitationOutcome.NOT_EVALUATED

    # --- claim status and audit rows ------------------------------------------------------

    def finalize_claims(self, records: list[ClaimRecord]) -> None:
        for record in records:
            outcomes = list(record.outcomes.values())
            if record.markers and not record.cited and record.citation_reason is not None:
                outcomes = [CitationOutcome.NOT_EVALUATED]  # unaligned: citation unchecked
            record.claim_status = classify_claim(record.pool_status, outcomes)
            self.store.record_claim_audit(
                ClaimAuditRecord(
                    claim_id=record.claim_id,
                    pool_status=record.pool_status,
                    not_evaluated_reason=record.not_evaluated_reason,
                    claim_status=record.claim_status,
                    alignment_status=record.alignment_status,
                    aligned_sentence_index=record.sentence_index,
                    alignment_score=record.alignment_score,
                    priority_rank=record.priority_rank,
                )
            )


# --- stages 8–9: audit-only computations --------------------------------------------------


def quantitative_findings(
    records, state, aliases, chunks, embedder, *, entities, approx_rel, group_cosine
) -> list[dict]:
    pairs: list[TextPair] = claim_pairs_by_metric(
        [(r.alias, r.text) for r in records], embedder.embed_documents, min_cosine=group_cosine
    )
    alias_of_chunk = {ref.chunk_id: alias for alias, ref in aliases.passages.items()}
    for record in records:
        for chunk_id, (relation, quote) in state[record.claim_id]["relations"].items():
            if relation is EvidenceRelation.SUPPORTS and quote:
                pairs.append(
                    TextPair(
                        "claim_quote",
                        record.alias,
                        record.text,
                        alias_of_chunk.get(chunk_id, str(chunk_id)),
                        quote,
                    )
                )
        for alias in record.cited:
            for sentence in split_sentences(chunks[alias].text):
                pairs.append(
                    TextPair("claim_cited_passage", record.alias, record.text, alias, sentence)
                )
    findings = audit_quantities(pairs, entities=entities, approx_rel=approx_rel)
    return [
        {
            "alias": f.alias,
            "relation": f.relation,
            "category": f.category.value,
            "a": f.owner_a,
            "b": f.owner_b,
            "value_a": f.mention_a.raw,
            "value_b": f.mention_b.raw,
            "reason": f.reason,
        }
        for f in findings
    ]


def subquestion_assessments(evidence, records) -> list[dict]:
    by_subquestion: dict[str, list[ClaimStatus]] = {}
    for record in records:
        if record.subquestion:
            by_subquestion.setdefault(record.subquestion, []).append(record.claim_status)
    inputs = [
        SubquestionInput(
            s.alias,
            s.is_core,
            len(s.passages),
            s.academic_passages,
            retrieval_failed=s.gap_reason == "provider_failure",
        )
        for s in evidence.per_subquestion
    ]
    return [a.__dict__ for a in assess_subquestions(inputs, by_subquestion)]


def conflict_contexts(conflicts, aliases, profiles) -> list:
    """Stored conflicts with each side's provider metadata and the fixed framing."""
    label_of_source = {source_id: label for label, source_id in aliases.sources.items()}
    return [
        conflict_context(
            row["conflict_type"],
            row["description"],
            profiles.get(label_of_source.get(row["supporting_source_id"])),
            profiles.get(label_of_source.get(row["contradicting_source_id"])),
        )
        for row in conflicts
    ]


def conflict_records(conflicts, aliases, profiles) -> list[dict]:
    result = []
    for context in conflict_contexts(conflicts, aliases, profiles):
        result.append(
            {
                "conflict_type": context.conflict_type,
                "description": context.description,
                "supporting": context.side_supporting.label if context.side_supporting else None,
                "contradicting": context.side_contradicting.label
                if context.side_contradicting
                else None,
                "framing": context.framing,
            }
        )
    return result
