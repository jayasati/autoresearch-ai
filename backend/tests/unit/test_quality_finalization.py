"""report-quality-v1 stages 10b–12 (stage 33, S5b): revision, re-verification, final
validation and finalization — and the whole path, end to end, on fakes.

The pipeline runs stage by stage (prepare → draft → verify → finalize) on throwaway
SQLite. The scripted reviser reads the real revision prompt — its claim table
(alias | status | passages | text) and draft lines — so what it may state is exactly
what verification established. No network, no tokens.
"""

import re

import pytest
from sqlalchemy import select

from app.core.constants import (
    ClaimStatus,
    NotEvaluatedReason,
    PoolStatus,
    ReportQualityStatus,
    RunStatus,
)
from app.models import FinalClaimCheck, Report, ReportQualityAudit, ResearchRun
from app.services.llm import LLMResponseInvalid
from app.services.quality.models import StructuredReport
from app.services.quality.pipeline import ReportQualityPipeline
from app.services.research_service import ResearchService
from tests.unit.test_orchestrator import settings
from tests.unit.test_quality_pipeline import request
from tests.unit.test_quality_planner_evidence import EMBED
from tests.unit.test_quality_verification import (
    ALPHA,
    FakeStore,
    VerifyingLLM,
    alpha_first,
    bravo_first,
    judge,
    providers,
)

CLAIM_LINE = re.compile(r"^(C\d+) \| ([A-Z_]+) \| ([^|]*) \| (.+)$", re.M)
DRAFT_LINE = re.compile(r"^(Q\d+|SYNTHESIS) \| (.+)$", re.M)


def reviser(change=None, cite=None, extra=None):
    """Build the final report from the revision prompt: each SUPPORTED/PARTIAL claim
    is stated (optionally rewritten by `change(text)` or re-cited by `cite`), every
    other claim is left out, uncertainty sentences are kept."""

    def revise(prompt):
        claims = {
            text: (
                alias,
                status,
                [p.strip() for p in passages.split(",") if p.strip() not in ("", "-")],
            )
            for alias, status, passages, text in CLAIM_LINE.findall(prompt)
        }
        sections: dict[str, list] = {}
        for subquestion, text in DRAFT_LINE.findall(prompt.split("Verified claims")[0]):
            sentences = sections.setdefault(subquestion, [])
            if text in claims:
                alias, status, passages = claims[text]
                if status not in ("SUPPORTED", "PARTIAL"):
                    continue  # never stated; the validator lists it
                sentences.append(
                    {
                        "text": change(text) if change else text,
                        "kind": "finding" if status == "SUPPORTED" else "qualification",
                        "claim_ids": [alias],
                        "finding_refs": [],
                        "evidence_ids": cite if cite is not None else passages,
                    }
                )
            else:
                sentences.append(
                    {
                        "text": text,
                        "kind": "uncertainty",
                        "claim_ids": [],
                        "finding_refs": [],
                        "evidence_ids": [],
                    }
                )
        body = [
            {"subquestion": q, "heading": f"Section {q}", "sentences": s}
            for q, s in sections.items()
        ]
        if extra:
            body.append(extra)
        return StructuredReport.model_validate({"title": "Coffee and health", "sections": body})

    return revise


class FinalLLM(VerifyingLLM):
    """VerifyingLLM plus the revision call (told apart by its claim table)."""

    def __init__(self, pool, check=None, revise=None):
        super().__init__(pool=pool, check=check)
        self.revise = revise or reviser()

    async def generate_structured_output(self, prompt, schema, **kwargs):
        if schema is StructuredReport and "Verified claims (alias" in prompt:
            self.calls.append(("revise", prompt))
            outcome = self.revise(prompt)
            if isinstance(outcome, Exception):
                raise outcome
            from tests.unit.test_quality_verification import Response

            return Response(outcome)
        return await super().generate_structured_output(prompt, schema, **kwargs)


async def full_run(service_session, llm, *, order=bravo_first, **overrides):
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
    )
    prepared = await pipeline.prepare_evidence(request())
    drafted = await pipeline.draft_and_align(prepared)
    verified = await pipeline.verify_claims(prepared, drafted)
    final = await pipeline.finalize(prepared, drafted, verified)
    return prepared, drafted, verified, final


def exact_support(prompt):
    """Pool: each claim supported by the passage it cites."""
    claim = prompt.split("Claim to verify:\n", 1)[1].split("\n", 1)[0]
    return judge(pick="Alpha" if "diabetes" in claim else "Bravo")(prompt)


def stored(session):
    [run] = session.execute(select(ResearchRun)).scalars()
    session.refresh(run)
    report = session.execute(select(Report)).scalars().one()
    session.refresh(report)
    audit = session.execute(select(ReportQualityAudit)).scalars().one()
    session.refresh(audit)
    return run, report, audit


class TestEndToEnd:
    async def test_a_fully_supported_draft_becomes_a_complete_final_report_without_revision(
        self, service_session
    ):
        llm = FinalLLM(pool=exact_support)
        _, drafted, verified, final = await full_run(service_session, llm, order=alpha_first)
        assert all(r.claim_status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE for r in verified.claims)
        assert final.status == "complete" and not final.revised
        assert "revise" not in [k for k, _ in llm.calls]  # revision only when necessary

        run, report, audit = stored(service_session)
        assert run.status is RunStatus.COMPLETED
        assert audit.quality_status is ReportQualityStatus.COMPLETE
        assert audit.stage == "finalized"
        assert report.final_markdown == final.final_markdown
        # every marker in the body is a stored label; References list exactly them
        body, _, references = report.final_markdown.partition("## References")
        markers = set(re.findall(r"\[(S\d+)\]", body))
        assert markers and markers <= set(drafted.aliases.sources)
        assert set(re.findall(r"\*\*\[(S\d+)\]\*\*", references)) == markers
        assert "## Evidence gaps" in report.final_markdown  # Q3's searches failed
        assert "C1" not in report.final_markdown and "E1" not in report.final_markdown

    async def test_an_unconfirmed_citation_is_revised_out_and_listed_not_dropped(
        self, service_session
    ):
        # Diabetes claim: pool support only from BRAVO (uncited) -> citation not confirmed.
        llm = FinalLLM(
            pool=judge(pick="Bravo"), check=judge(verdict="not_enough_evidence", relation="neutral")
        )
        _, _, verified, final = await full_run(service_session, llm)
        assert final.revised
        _, report, audit = stored(service_session)
        md = report.final_markdown or ""
        diabetes = next(r for r in verified.claims if "diabetes" in r.text)
        assert diabetes.claim_status is ClaimStatus.CITATION_NOT_CONFIRMED
        assert "## Supported by other evidence — citation not confirmed" in md
        listed = md.split("citation not confirmed", 1)[1]
        assert diabetes.text in listed
        assert any(
            a["action"] == "omission_listed" for a in audit.payload["finalization"]["actions"]
        )


class TestReverification:
    async def staged(self, service_session, revise, check, **overrides):
        llm = FinalLLM(pool=judge(pick="Bravo"), check=check, revise=revise)
        result = await full_run(service_session, llm, **overrides)
        return llm, result

    @pytest.mark.parametrize(
        "change",
        [
            lambda t: t.replace("is reported here", "is reported here at 25 percent"),  # number
            lambda t: t.replace("is reported", "is not reported"),  # negation
            lambda t: t.replace("is reported", "may be reported"),  # hedge
            lambda t: t.replace("in adults", "in children"),  # scope
        ],
    )
    async def test_a_meaning_change_triggers_reverification_on_the_exact_cited_passage(
        self, service_session, change
    ):
        def check(prompt):
            claim = prompt.split("Claim to verify:\n", 1)[1].split("\n", 1)[0]
            if "blood pressure" in claim:  # the rewritten sentence: support from BRAVO
                return judge(pick="Bravo")(prompt)
            return judge(verdict="not_enough_evidence", relation="neutral")(prompt)

        llm, (_, _, _, final) = await self.staged(service_session, reviser(change=change), check)
        assert final.reverified, "a changed sentence must be re-verified"

        def claim_of(prompt):
            return prompt.split("Claim to verify:\n", 1)[1].split("\n", 1)[0]

        reverify_prompts = [
            p for k, p in llm.calls if k == "check" and "blood pressure" in claim_of(p)
        ]
        assert reverify_prompts and all("Alpha" not in p for p in reverify_prompts)
        rows = service_session.execute(select(FinalClaimCheck)).scalars().all()
        assert rows and all(r.pool_status is not PoolStatus.NOT_EVALUATED for r in rows)

    async def test_a_rewritten_claim_supported_by_its_cited_passage_reaches_the_body(
        self, service_session
    ):
        change = lambda t: t.replace("is reported here", "is reported here at 2 mmHg")  # noqa: E731
        _, (_, _, _, final) = await self.staged(
            service_session, reviser(change=change), check=judge(pick="Bravo")
        )
        md = final.final_markdown
        assert md and "at 2 mmHg" in md.split("---")[0]
        [row] = service_session.execute(select(FinalClaimCheck)).scalars().all()
        assert row.claim_status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE
        assert row.model and row.prompt_version == "citation-check-v1"

    async def test_a_rewritten_claim_supported_only_by_an_unrelated_passage_is_rejected(
        self, service_session
    ):
        # The rewritten blood-pressure sentence now cites the ALPHA passage; only BRAVO
        # supports it. Re-verification sees ONLY Alpha, so it must not reach the body.
        change = lambda t: t.replace("is reported here", "is reported here at 2 mmHg")  # noqa: E731

        def check(prompt):
            return (
                judge(verdict="not_enough_evidence", relation="neutral")(prompt)
                if "Alpha" in prompt
                else judge(pick="Bravo")(prompt)
            )

        llm = FinalLLM(pool=judge(pick="Bravo"), check=check, revise=None)
        prepared_alpha = {}

        def revise_citing_alpha(prompt):
            report = reviser(change=change)(prompt)
            # re-cite the rewritten sentence to the Alpha passage (from the draft lines)
            alpha_passage = prepared_alpha["alias"]
            for section in report.sections:
                for sentence in section.sentences:
                    if "2 mmHg" in sentence.text:
                        sentence.evidence_ids = [alpha_passage]
            return report

        llm.revise = revise_citing_alpha
        test_settings = settings().model_copy(update={"REPORT_QUALITY_ENABLED": True})
        web, academic = providers()
        store = FakeStore(bravo_first)
        pipeline = ReportQualityPipeline(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            llm=llm,
            web_search=web,
            academic_search=academic,
            embedder=EMBED,
            store_factory=lambda n: store,
        )
        prepared = await pipeline.prepare_evidence(request())
        prepared_alpha["alias"] = next(
            a for a, c in prepared.evidence.chunks.items() if "Alpha" in c.text
        )
        drafted = await pipeline.draft_and_align(prepared)
        verified = await pipeline.verify_claims(prepared, drafted)
        final = await pipeline.finalize(prepared, drafted, verified)

        body = (final.final_markdown or "").split("---")[0]
        assert "2 mmHg" not in body
        reverify = [p for k, p in llm.calls if k == "check" and "2 mmHg" in p]
        assert reverify and all("Bravo" not in p for p in reverify)
        rows = service_session.execute(select(FinalClaimCheck)).scalars().all()
        assert [r.claim_status for r in rows] != [ClaimStatus.SUPPORTED_BY_CITED_SOURCE]


class TestPolicyAndFailures:
    async def test_contested_evidence_stays_visibly_contested(self, service_session):
        llm = FinalLLM(
            pool=judge(pick="Bravo"), check=judge(verdict="contradicted", relation="contradicts")
        )
        _, _, verified, final = await full_run(service_session, llm)
        diabetes = next(r for r in verified.claims if "diabetes" in r.text)
        assert diabetes.claim_status is ClaimStatus.CONTESTED
        md = final.final_markdown or ""
        assert "## Contested — sources disagree" in md
        assert diabetes.text in md.split("Contested — sources disagree", 1)[1]
        assert "does not by itself show that either statement is false" in md
        assert diabetes.text not in md.split("---")[0]  # never a finding

    async def test_unverified_claims_are_listed_as_not_evaluated(self, service_session):
        llm = FinalLLM(pool=lambda prompt: LLMResponseInvalid())
        _, _, verified, final = await full_run(service_session, llm)
        assert all(r.claim_status is ClaimStatus.NOT_EVALUATED for r in verified.claims)
        # nothing supported -> no honest body: finalization fails, draft kept
        assert final.status == "finalization_failed"
        run, report, audit = stored(service_session)
        assert report.final_markdown is None and report.markdown
        assert audit.quality_status is ReportQualityStatus.FINALIZATION_FAILED
        assert run.status is RunStatus.COMPLETED
        assert any("no body sentence" in r for r in audit.payload["finalization"]["reasons"])

    async def test_the_revision_runs_at_most_twice_and_reverification_once(self, service_session):
        calls = {"n": 0}

        def flaky(prompt):
            calls["n"] += 1
            return LLMResponseInvalid() if calls["n"] == 1 else reviser()(prompt)

        llm = FinalLLM(
            pool=judge(pick="Bravo"),
            check=judge(verdict="not_enough_evidence", relation="neutral"),
            revise=flaky,
        )
        _, _, _, final = await full_run(service_session, llm)
        kinds = [k for k, _ in llm.calls]
        assert kinds.count("revise") == 2  # one retry, no loop
        assert final.status in ("complete", "degraded")

    async def test_invalid_revision_twice_fails_finalization_and_keeps_the_draft(
        self, service_session
    ):
        llm = FinalLLM(
            pool=judge(pick="Bravo"),
            check=judge(verdict="not_enough_evidence", relation="neutral"),
            revise=lambda prompt: LLMResponseInvalid(),
        )
        _, _, _, final = await full_run(service_session, llm)
        assert final.status == "finalization_failed"
        assert final.reasons == ["revision not produced: invalid output twice"]
        run, report, audit = stored(service_session)
        assert report.final_markdown is None and report.markdown
        assert run.status is RunStatus.COMPLETED
        assert audit.quality_status is ReportQualityStatus.FINALIZATION_FAILED
        assert audit.payload["finalization"]["reasons"] == final.reasons
        assert audit.payload["budget"]["stages"]["revision"]["failures"] == 2

    async def test_budget_exhaustion_during_revision(self, service_session):
        # plan, draft, extraction, 2 pool, 1 citation check = 6; revision refused.
        llm = FinalLLM(
            pool=judge(pick="Bravo"), check=judge(verdict="not_enough_evidence", relation="neutral")
        )
        _, _, _, final = await full_run(
            service_session, llm, REPORT_QUALITY_MAX_LLM_ATTEMPTS_QUICK=6
        )
        assert final.status == "finalization_failed"
        assert final.reasons == ["revision not produced: budget (attempts)"]
        assert "revise" not in [k for k, _ in llm.calls]

    async def test_budget_exhaustion_during_reverification(self, service_session):
        change = lambda t: t.replace("is reported here", "is reported here at 2 mmHg")  # noqa: E731
        llm = FinalLLM(
            pool=judge(pick="Bravo"),
            check=judge(verdict="not_enough_evidence", relation="neutral"),
            revise=reviser(change=change),
        )
        _, _, _, final = await full_run(
            service_session, llm, REPORT_QUALITY_MAX_LLM_ATTEMPTS_QUICK=7
        )
        [row] = service_session.execute(select(FinalClaimCheck)).scalars().all()
        assert row.pool_status is PoolStatus.NOT_EVALUATED
        assert row.not_evaluated_reason is NotEvaluatedReason.BUDGET
        assert "2 mmHg" not in (final.final_markdown or "").split("---")[0]
        assert final.status in ("degraded", "finalization_failed")

    async def test_validation_rejects_invented_citations_from_the_revision(self, service_session):
        extra = {
            "subquestion": "Q1",
            "heading": "Invented",
            "sentences": [
                {
                    "text": "An invented finding.",
                    "kind": "finding",
                    "claim_ids": ["C99"],
                    "finding_refs": [],
                    "evidence_ids": ["E99"],
                }
            ],
        }
        llm = FinalLLM(
            pool=judge(pick="Bravo"),
            check=judge(verdict="not_enough_evidence", relation="neutral"),
            revise=reviser(extra=extra),
        )
        _, _, _, final = await full_run(service_session, llm)
        rejected = [a["detail"] for a in final.actions if a["action"] == "reference_rejected"]
        assert "unknown claim C99" in rejected and "unknown passage E99" in rejected
        assert "An invented finding." not in (final.final_markdown or "").split("---")[0]

    async def test_an_unexpected_error_fails_the_run(self, service_session):
        def boom(prompt):
            raise RuntimeError("renderer exploded")

        llm = FinalLLM(
            pool=judge(pick="Bravo"),
            check=judge(verdict="not_enough_evidence", relation="neutral"),
            revise=boom,
        )
        with pytest.raises(RuntimeError):
            await full_run(service_session, llm)
        [run] = service_session.execute(select(ResearchRun)).scalars()
        service_session.refresh(run)
        assert run.status is RunStatus.FAILED and run.error_code == "internal_error"
        assert ALPHA  # fixtures stay distinctive
