"""report-quality-v1 stages 7–9 (stage 32, S5a): pool verification, citation-check-v1,
budgets, claim audits.

Deterministic throughout: distinctive passage texts ("Alpha…", "Bravo…") so a test
knows which chunk a claim cites and which is unrelated; a fake vector store whose
ranking the test controls; a scripted verifier that answers from the passages in the
real prompt. The legacy verifier itself (prompt, enforcement, persistence) is the
real, unmodified one. No network, no tokens; throwaway SQLite.
"""

import re

import pytest
from sqlalchemy import select

from app.core.constants import (
    CitationOutcome,
    ClaimStatus,
    EstablishedBy,
    NotEvaluatedReason,
    PoolStatus,
    ReportQualityStatus,
    ResearchMode,
    RunStatus,
)
from app.models import CitationCheck, ClaimAudit, ReportQualityAudit, ResearchRun
from app.services.claims import ClaimExtractionModel
from app.services.llm import LLMProviderUnavailable, LLMResponseInvalid, TokenUsage
from app.services.quality.budget import (
    BudgetedLLM,
    BudgetExhausted,
    BudgetInsufficient,
    RunBudget,
    estimate_tokens,
    no_sdk_retries,
)
from app.services.quality.models import CoveragePlan, StructuredReport
from app.services.quality.pipeline import ReportQualityPipeline
from app.services.quality.verification import initial_claim_cap
from app.services.research_service import ResearchService
from app.services.vectorstore import RetrievedChunk
from app.services.verification.models import ClaimVerificationModel
from tests.unit.test_orchestrator import settings, web_candidate, web_result
from tests.unit.test_quality_drafting import claims_from_prompt, draft_from_prompt
from tests.unit.test_quality_pipeline import PLAN, PerQuerySearch, request
from tests.unit.test_quality_planner_evidence import EMBED

ALPHA = "Alpha trial: moderate coffee lowered type 2 diabetes risk by 15 percent in adults."
BRAVO = "Bravo survey: coffee raised systolic blood pressure by 2 mmHg in adults."
PASSAGE = re.compile(r"\[(P\d+)\] \(from: [^)]*\)\n(.*?)(?=\n\n\[P\d+\]|\n\nVerify now\.)", re.S)


def candidate(index, text):
    return web_candidate(index).model_copy(update={"snippet": (text + " ") * 3})


def providers():
    web = PerQuerySearch(
        {
            "diabetes": web_result(candidate(0, ALPHA)),
            "blood pressure": web_result(candidate(1, BRAVO)),
        },
        academic=False,
    )
    return web, PerQuerySearch({}, academic=True)


class FakeStore:
    """The run collection the legacy verifier searches; the test orders the hits."""

    def __init__(self, order):
        self.order = order  # callable(claim_text, chunks) -> chunks
        self.chunks = []

    def count(self):
        return len(self.chunks)

    def index_chunks(self, chunks):
        self.chunks = list(chunks)

    def search(self, text, k):
        ranked = self.order(text, self.chunks)[:k]
        return [
            RetrievedChunk(
                **chunk.model_dump(),
                query=text,
                rank=i,
                similarity=0.9,
                distance=0.1,
                embedding_model="test/bag-of-words-64",
            )
            for i, chunk in enumerate(ranked)
        ]


def bravo_first(text, chunks):
    return sorted(chunks, key=lambda c: "Bravo" not in c.text)


def alpha_first(text, chunks):
    return sorted(chunks, key=lambda c: "Alpha" not in c.text)


def passages(prompt):
    return PASSAGE.findall(prompt)


def judge(verdict="supported", relation="supports", pick="Alpha", quote=None, conflict=None):
    """A verifier answer over the passages in the prompt: the passage containing
    `pick` gets `relation` with a verbatim quote (or `quote`), the rest neutral."""

    def answer(prompt):
        assessed = []
        for label, text in passages(prompt):
            if pick in text and relation != "neutral":
                exact = (
                    quote if quote is not None else text.split(" ")[0] + " " + text.split(" ")[1]
                )
                assessed.append({"passage": label, "relation": relation, "quote": exact})
            else:
                assessed.append({"passage": label, "relation": "neutral", "quote": None})
        return ClaimVerificationModel.model_validate(
            {
                "verdict": verdict,
                "confidence": 0.9,
                "rationale": "<scripted rationale>",
                "passages": assessed,
                "conflict": conflict,
            }
        )

    return answer


class Response:
    def __init__(self, data):
        self.data = data
        self.model = "fake-verifier-2026"
        self.usage = TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)


class VerifyingLLM:
    """Plan, draft and claims as in S4; verification by script. `pool` and `check`
    are callables (prompt -> model or exception) for the two kinds of call, told
    apart by whether the prompt holds more than the claim's cited passages."""

    def __init__(self, pool, check=None):
        self.pool, self.check = pool, check or pool
        self.calls: list[tuple[str, str]] = []

    async def generate_structured_output(self, prompt, schema, *, system=None, **_):
        if schema is CoveragePlan:
            self.calls.append(("plan", ""))
            return Response(PLAN)
        if schema is StructuredReport:
            self.calls.append(("draft", ""))
            return Response(draft_from_prompt(prompt))
        if schema is ClaimExtractionModel:
            self.calls.append(("claims", ""))
            return Response(claims_from_prompt(prompt))
        if schema is ClaimVerificationModel:
            kind = "pool" if len(passages(prompt)) > 1 else "check"
            self.calls.append((kind, prompt))
            outcome = (self.pool if kind == "pool" else self.check)(prompt)
            if isinstance(outcome, Exception):
                raise outcome
            return Response(outcome)
        raise AssertionError(schema)


async def run_pipeline(service_session, llm, *, order=bravo_first, clock=None, **overrides):
    test_settings = settings().model_copy(update={"REPORT_QUALITY_ENABLED": True, **overrides})
    web, academic = providers()
    store = FakeStore(order)
    pipeline = ReportQualityPipeline(
        settings=test_settings,
        research_service=ResearchService(settings=test_settings),
        llm=llm,
        web_search=web,
        academic_search=academic,
        embedder=EMBED,
        store_factory=lambda name: store,
        clock=clock,
    )
    prepared = await pipeline.prepare_evidence(request())
    drafted = await pipeline.draft_and_align(prepared)
    outcome = await pipeline.verify_claims(prepared, drafted)
    return prepared, drafted, outcome


def by_text(outcome, word):
    return next(r for r in outcome.claims if word.lower() in r.text.lower())


def checks(session, claim_id):
    return (
        session.execute(select(CitationCheck).where(CitationCheck.claim_id == claim_id))
        .scalars()
        .all()
    )


class TestExactCitation:
    async def test_pool_support_on_the_exact_cited_chunk_is_reused_without_a_call(
        self, service_session
    ):
        # Pool hits rank the cited passage first; each claim's own passage supports it.
        def pool(prompt):
            claim = prompt.split("Claim to verify:\n", 1)[1].split("\n", 1)[0]
            return judge(pick="Alpha" if "diabetes" in claim else "Bravo")(prompt)

        llm = VerifyingLLM(pool=pool)
        _, _, outcome = await run_pipeline(service_session, llm, order=alpha_first)
        diabetes = by_text(outcome, "diabetes")
        assert diabetes.pool_status is PoolStatus.SUPPORTED
        assert list(diabetes.outcomes.values()) == [CitationOutcome.SUPPORTS]
        assert diabetes.claim_status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE
        [row] = checks(service_session, diabetes.claim_id)
        assert row.established_by is EstablishedBy.POOL_VERIFICATION and row.model is None
        assert row.quoted_text in ALPHA
        assert [k for k, _ in llm.calls].count("check") == 0  # shortcut: no extra call

    async def test_support_from_an_unrelated_passage_never_validates_the_citation(
        self, service_session
    ):
        # The pool supports the diabetes claim from BRAVO (not what it cites); the
        # citation check sees ONLY the cited ALPHA passage and finds it silent.
        llm = VerifyingLLM(
            pool=judge(pick="Bravo"), check=judge(verdict="not_enough_evidence", relation="neutral")
        )
        _, _, outcome = await run_pipeline(service_session, llm)
        diabetes = by_text(outcome, "diabetes")
        assert diabetes.pool_status is PoolStatus.SUPPORTED
        assert list(diabetes.outcomes.values()) == [CitationOutcome.DOES_NOT_ESTABLISH]
        assert diabetes.claim_status is ClaimStatus.CITATION_NOT_CONFIRMED
        check_prompts = [p for k, p in llm.calls if k == "check"]
        diabetes_prompt = next(p for p in check_prompts if "diabetes" in p.split("\n")[1])
        assert "Alpha" in diabetes_prompt and "Bravo" not in diabetes_prompt
        [row] = checks(service_session, diabetes.claim_id)
        assert row.established_by is None and row.quoted_text is None

    async def test_a_non_verbatim_quote_never_establishes_support(self, service_session):
        llm = VerifyingLLM(
            pool=judge(pick="Bravo"),
            check=judge(pick="Alpha", quote="coffee cured diabetes outright"),
        )
        _, _, outcome = await run_pipeline(service_session, llm)
        diabetes = by_text(outcome, "diabetes")
        assert list(diabetes.outcomes.values()) == [CitationOutcome.DOES_NOT_ESTABLISH]
        assert diabetes.claim_status is ClaimStatus.CITATION_NOT_CONFIRMED

    async def test_a_contradicting_cited_passage_makes_the_claim_contested(self, service_session):
        llm = VerifyingLLM(
            pool=judge(pick="Bravo"), check=judge(verdict="contradicted", relation="contradicts")
        )
        _, _, outcome = await run_pipeline(service_session, llm)
        diabetes = by_text(outcome, "diabetes")
        assert list(diabetes.outcomes.values()) == [CitationOutcome.CONTRADICTS]
        assert diabetes.claim_status is ClaimStatus.CONTESTED

    async def test_insufficient_pool_evidence_stays_insufficient(self, service_session):
        llm = VerifyingLLM(
            pool=judge(verdict="not_enough_evidence", relation="neutral"),
            check=judge(verdict="supported"),
        )
        _, _, outcome = await run_pipeline(service_session, llm)
        for record in outcome.claims:
            assert record.pool_status is PoolStatus.NOT_ENOUGH_EVIDENCE
            assert record.claim_status is ClaimStatus.INSUFFICIENT_EVIDENCE


class TestFailuresAreNeverSupport:
    async def test_malformed_pool_output_twice_is_a_technical_not_evaluated(self, service_session):
        llm = VerifyingLLM(pool=lambda prompt: LLMResponseInvalid())
        _, _, outcome = await run_pipeline(service_session, llm)
        for record in outcome.claims:
            assert record.pool_status is PoolStatus.NOT_EVALUATED
            assert record.not_evaluated_reason is NotEvaluatedReason.TECHNICAL
            assert record.claim_status is ClaimStatus.NOT_EVALUATED
            assert set(record.outcomes.values()) == {CitationOutcome.NOT_EVALUATED}
        assert [k for k, _ in llm.calls].count("pool") == 2 * len(outcome.claims)  # one retry

    async def test_malformed_citation_check_is_technical_not_support(self, service_session):
        llm = VerifyingLLM(pool=judge(pick="Bravo"), check=lambda prompt: LLMResponseInvalid())
        _, _, outcome = await run_pipeline(service_session, llm)
        diabetes = by_text(outcome, "diabetes")
        assert diabetes.citation_reason is NotEvaluatedReason.TECHNICAL
        assert diabetes.claim_status is ClaimStatus.NOT_EVALUATED
        [row] = checks(service_session, diabetes.claim_id)
        assert row.outcome is CitationOutcome.NOT_EVALUATED
        assert row.not_evaluated_reason is NotEvaluatedReason.TECHNICAL

    async def test_a_provider_outage_on_one_claim_is_recorded_and_the_rest_continue(
        self, service_session
    ):
        def pool(prompt):
            if "diabetes" in prompt.split("Claim to verify:\n", 1)[1].split("\n", 1)[0]:
                return LLMProviderUnavailable()
            return judge(pick="Bravo")(prompt)

        _, _, outcome = await run_pipeline(service_session, VerifyingLLM(pool=pool))
        assert by_text(outcome, "diabetes").not_evaluated_reason is NotEvaluatedReason.TECHNICAL
        assert by_text(outcome, "blood pressure").pool_status is PoolStatus.SUPPORTED

    async def test_attempt_budget_exhaustion_is_not_evaluated_never_support(self, service_session):
        # 5 attempts: plan, draft, extraction, two pool verifications. The diabetes
        # claim's pool support came from BRAVO (uncited), so it needs a citation check —
        # which is refused. It must not be counted as supported by its cited source.
        llm = VerifyingLLM(pool=judge(pick="Bravo"), check=judge(pick="Alpha"))
        _, _, outcome = await run_pipeline(
            service_session, llm, REPORT_QUALITY_MAX_LLM_ATTEMPTS_QUICK=5
        )
        diabetes = by_text(outcome, "diabetes")
        assert diabetes.pool_status is PoolStatus.SUPPORTED
        assert diabetes.citation_reason is NotEvaluatedReason.BUDGET
        assert diabetes.claim_status is ClaimStatus.NOT_EVALUATED
        [row] = checks(service_session, diabetes.claim_id)
        assert row.outcome is CitationOutcome.NOT_EVALUATED
        assert row.not_evaluated_reason is NotEvaluatedReason.BUDGET
        assert outcome.budget["attempts"] == 5
        assert outcome.budget["stages"]["citation_check"]["refused"] == 1
        assert len(llm.calls) == 5  # the refused attempt was never sent

    async def test_a_retry_spends_an_attempt_and_pool_verification_is_refused(
        self, service_session
    ):
        # The planner's one retry is an attempt like any other: plan x2, draft,
        # extraction, one pool verification — the second claim's verification is refused.
        class RetryingPlanner(VerifyingLLM):
            failed = False

            async def generate_structured_output(self, prompt, schema, **kwargs):
                if schema is CoveragePlan and not self.failed:
                    self.failed = True
                    self.calls.append(("plan", ""))
                    raise LLMResponseInvalid()
                return await super().generate_structured_output(prompt, schema, **kwargs)

        llm = RetryingPlanner(pool=judge(pick="Bravo"))
        _, _, outcome = await run_pipeline(
            service_session, llm, REPORT_QUALITY_MAX_LLM_ATTEMPTS_QUICK=5
        )
        refused = [r for r in outcome.claims if r.not_evaluated_reason is NotEvaluatedReason.BUDGET]
        assert len(refused) == 1 and refused[0].pool_status is PoolStatus.NOT_EVALUATED
        assert refused[0].claim_status is ClaimStatus.NOT_EVALUATED
        assert outcome.budget["stages"]["planning"]["attempts"] == 2
        assert outcome.budget["attempts"] == 5 and len(llm.calls) == 5

    async def test_the_deadline_stops_verification_honestly(self, service_session):
        now = [0.0]

        def clock():
            return now[0]

        class Late(VerifyingLLM):
            async def generate_structured_output(self, prompt, schema, **kwargs):
                if schema is ClaimExtractionModel:
                    now[0] = 10_000.0  # the deadline passes during extraction
                return await super().generate_structured_output(prompt, schema, **kwargs)

        _, _, outcome = await run_pipeline(
            service_session, Late(pool=judge(pick="Bravo")), clock=clock
        )
        assert {r.not_evaluated_reason for r in outcome.claims} == {NotEvaluatedReason.DEADLINE}
        assert all(r.claim_status is ClaimStatus.NOT_EVALUATED for r in outcome.claims)

    async def test_the_claim_cap_leaves_lower_priority_claims_not_evaluated(self, service_session):
        # max 2 claims, 20% reserve -> 1 verified now
        _, _, outcome = await run_pipeline(
            service_session,
            VerifyingLLM(pool=judge(pick="Bravo")),
            REPORT_QUALITY_MAX_CLAIMS_VERIFIED_QUICK=2,
        )
        capped = [
            r for r in outcome.claims if r.not_evaluated_reason is NotEvaluatedReason.CLAIM_CAP
        ]
        assert len(capped) == len(outcome.claims) - 1
        assert all(r.priority_rank >= 1 for r in capped)

    async def test_an_unexpected_failure_fails_the_run(self, service_session):
        def order(text, chunks):
            raise RuntimeError("store exploded")

        with pytest.raises(RuntimeError):
            await run_pipeline(service_session, VerifyingLLM(pool=judge()), order=order)
        [run] = service_session.execute(select(ResearchRun)).scalars()
        service_session.refresh(run)
        assert run.status is RunStatus.FAILED and run.error_code == "internal_error"


class TestRecords:
    async def test_claim_audits_audit_row_and_run_status(self, service_session):
        _, drafted, outcome = await run_pipeline(
            service_session, VerifyingLLM(pool=judge(pick="Bravo"), check=judge(pick="Alpha"))
        )
        [run] = service_session.execute(select(ResearchRun)).scalars()
        service_session.refresh(run)
        assert run.status is RunStatus.VERIFYING  # revision and finalization are S5b
        audits = {a.claim_id: a for a in service_session.execute(select(ClaimAudit)).scalars()}
        for record in outcome.claims:
            assert audits[record.claim_id].pool_status is record.pool_status
            assert audits[record.claim_id].claim_status is record.claim_status
            assert audits[record.claim_id].alignment_status == record.alignment_status
        [audit] = service_session.execute(select(ReportQualityAudit)).scalars()
        assert audit.quality_status is ReportQualityStatus.RUNNING and audit.stage == "verified"
        assert {
            "verification",
            "budget",
            "quantitative_findings",
            "subquestion_assessments",
            "conflicts",
            "claim_cap",
        } <= set(audit.payload)
        assert audit.payload["budget"]["stages"]["verification"]["attempts"] >= 1
        assert "revision" in audit.payload["budget"]["reserved"]
        # a gap subquestion is assessed as such
        assert any(a["status"] == "evidence_gap" for a in audit.payload["subquestion_assessments"])

    async def test_decided_results_are_never_overwritten(self, service_session):
        from app.core.exceptions import ConflictError
        from app.services.quality.store import CitationCheckRecord, QualityStore
        from app.services.quality.verification import Verifier

        prepared, drafted, outcome = await run_pipeline(
            service_session, VerifyingLLM(pool=judge(pick="Bravo"), check=judge(pick="Alpha"))
        )
        test_settings = settings().model_copy(update={"REPORT_QUALITY_ENABLED": True})
        second_llm = VerifyingLLM(pool=judge(verdict="contradicted", relation="contradicts"))

        # 1. the run's status machine refuses a second verification pass outright
        pipeline = ReportQualityPipeline(
            settings=test_settings,
            llm=second_llm,
            embedder=EMBED,
            store_factory=lambda n: FakeStore(alpha_first),
        )
        with pytest.raises(ConflictError, match="verifying"):
            await pipeline.verify_claims(prepared, drafted)

        # 2. the verifier never re-judges a claim that already has a stored verdict
        records = [r for r in outcome.claims]
        before = {r.claim_id: r.pool_status for r in records}
        verifier = Verifier(
            settings=test_settings,
            research=ResearchService(settings=test_settings),
            store=QualityStore(),
            llm=BudgetedLLM(second_llm, RunBudget.for_depth(test_settings, "quick")),
            store_factory=lambda n: FakeStore(alpha_first),
            embedder=EMBED,
        )
        await verifier.verify_pool(prepared.research_id, records, cap=99, warnings=[])
        assert not [k for k, _ in second_llm.calls if k == "pool"]
        assert {r.claim_id: r.pool_status for r in records} == before

        # 3. the store refuses to record a second check for the same cited passage
        diabetes = by_text(outcome, "diabetes")
        alias = diabetes.cited[0]
        with pytest.raises(ConflictError, match="already has a check"):
            QualityStore().record_citation_check(
                CitationCheckRecord(
                    claim_id=diabetes.claim_id,
                    passage_alias=alias,
                    cited_chunk_id=drafted.aliases.passages[alias].chunk_id,
                    outcome=CitationOutcome.DOES_NOT_ESTABLISH,
                    procedure_version="citation-check-v1",
                )
            )


class TestBudget:
    def budget(self, **kwargs):
        values = dict(max_attempts=3, max_tokens=10_000, max_claims=5, deadline_seconds=60)
        values.update(kwargs)
        return RunBudget(**values, clock=lambda: 0.0)

    async def test_every_attempt_is_counted_and_refusals_are_never_sent(self):
        class Inner:
            calls = 0

            async def generate_structured_output(self, prompt, schema=None, **_):
                Inner.calls += 1
                raise LLMResponseInvalid()

        budget = self.budget(max_attempts=2)
        llm = BudgetedLLM(Inner(), budget, stage="x")
        for _ in range(2):
            with pytest.raises(LLMResponseInvalid):
                await llm.generate_structured_output("p", object)
        with pytest.raises(BudgetExhausted) as refused:
            await llm.generate_structured_output("p", object)
        assert refused.value.reason == "attempts" and Inner.calls == 2
        account = budget.accounts["x"]
        assert (account.attempts, account.failures, account.refused) == (2, 2, 1)
        assert account.measured_tokens == 0 and account.estimated_tokens > 0  # labelled

    async def test_measured_usage_is_used_when_reported(self):
        class Inner:
            async def generate_structured_output(self, prompt, schema=None, **_):
                return Response(None)

        budget = self.budget()
        await BudgetedLLM(Inner(), budget, stage="x").generate_structured_output("p", object)
        assert budget.accounts["x"].measured_tokens == 120
        assert budget.accounts["x"].estimated_tokens == 0

    def test_reservations_protect_later_stages(self):
        budget = self.budget(max_tokens=3000)
        budget.reserve("revision", 2000)
        with pytest.raises(BudgetExhausted) as exc:
            budget.check("verification", estimate_tokens("x" * 400, None, 1000))
        assert exc.value.reason == "tokens"
        budget.check("revision", 1500)  # the reserving stage may spend its share

    def test_deadline(self):
        budget = RunBudget(
            max_attempts=9, max_tokens=99_999, max_claims=5, deadline_seconds=10, clock=lambda: 0.0
        )
        budget.clock = lambda: 11.0
        with pytest.raises(BudgetExhausted) as exc:
            budget.check("x", 1)
        assert exc.value.reason == "deadline"

    def test_sdk_retries_are_off_on_a_copy(self):
        base = settings()
        assert no_sdk_retries(base).OPENAI_MAX_RETRIES == 0
        assert base.OPENAI_MAX_RETRIES == settings().OPENAI_MAX_RETRIES

    def test_claim_reserve(self):
        assert initial_claim_cap(30, 0.2) == 24
        assert initial_claim_cap(2, 0.2) == 1

    async def test_preflight_refuses_an_unfittable_budget_before_any_row(self, service_session):
        from tests.unit.test_quality_pipeline import pipeline, run_count

        quality = pipeline()
        quality.settings = quality.settings.model_copy(
            update={"REPORT_QUALITY_MAX_TOTAL_TOKENS_QUICK": 100}
        )
        with pytest.raises(BudgetInsufficient):
            await quality.prepare_evidence(request(ResearchMode.SEARCH_GROUNDED))
        assert run_count(service_session) == 0
