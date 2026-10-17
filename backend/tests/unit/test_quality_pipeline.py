"""report-quality-v1 stages 1–5 end to end (stage 30, S3).

Throwaway SQLite (`service_session`), the real document processor, and fakes for
everything external: a scripted planner LLM, per-query search providers, a
bag-of-words embedder. No network, no tokens, no shared database.
"""

import pytest
from sqlalchemy import func, select

from app.core.constants import PipelineVersion, ResearchMode, RunStatus
from app.models import ResearchRun, RunConfiguration
from app.schemas.research import ResearchStartRequest
from app.services.llm import LLMResponseInvalid
from app.services.orchestration.exceptions import NoEvidenceError
from app.services.quality.exceptions import ReportQualityDisabled, ReportQualityModeError
from app.services.quality.pipeline import ReportQualityPipeline
from app.services.quality.prompts import PLANNER_PROMPT_VERSION
from app.services.research_service import ResearchService
from app.services.retrieval import SearchProviderUnavailable
from tests.unit.test_orchestrator import (
    academic_candidate,
    academic_result,
    settings,
    web_candidate,
    web_result,
)
from tests.unit.test_quality_planner_evidence import EMBED, FakePlannerLLM, plan_payload

TOPIC = "What are the health effects of regular moderate coffee consumption in adults?"
PLAN = plan_payload(
    ("Does coffee lower type 2 diabetes risk in adults?", "core"),
    ("How does coffee affect blood pressure in adults?", "core"),
    ("Does evening coffee reduce sleep quality?", "supporting"),
)


class PerQuerySearch:
    """Answers by matching a fragment of the query; an exception value is raised."""

    def __init__(self, routes, *, academic):
        self.routes = routes
        self.academic = academic
        self.calls: list[str] = []

    async def search(self, query, *, max_results=None, limit=None):
        self.calls.append(query)
        for fragment, outcome in self.routes.items():
            if fragment in query.lower():
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return academic_result() if self.academic else web_result()


def providers():
    shared = web_candidate(9)  # found by Q1 and Q2
    web = PerQuerySearch(
        {
            "diabetes": web_result(web_candidate(0), shared),
            "blood pressure": web_result(web_candidate(1), shared),
            "sleep": SearchProviderUnavailable("tavily", "down"),
        },
        academic=False,
    )
    academic = PerQuerySearch(
        {
            "diabetes": academic_result(academic_candidate(0)),
            "sleep": SearchProviderUnavailable("semantic_scholar", "down"),
        },
        academic=True,
    )
    return web, academic, shared


def pipeline(llm=None, enabled=True, web=None, academic=None):
    test_settings = settings().model_copy(update={"REPORT_QUALITY_ENABLED": enabled})
    default_web, default_academic, _ = providers()
    return ReportQualityPipeline(
        settings=test_settings,
        research_service=ResearchService(settings=test_settings),
        llm=llm or FakePlannerLLM(PLAN),
        web_search=web or default_web,
        academic_search=academic or default_academic,
        embedder=EMBED,
    )


def request(mode=ResearchMode.SEARCH_GROUNDED):
    return ResearchStartRequest(topic=TOPIC, mode=mode, depth="quick")


def run_count(session):
    return session.execute(select(func.count()).select_from(ResearchRun)).scalar_one()


class TestGate:
    async def test_off_by_default_refuses_before_any_row_or_call(self, service_session):
        llm = FakePlannerLLM(PLAN)
        with pytest.raises(ReportQualityDisabled):
            await pipeline(llm=llm, enabled=False).prepare_evidence(request())
        assert run_count(service_session) == 0 and llm.prompts == []

    async def test_only_search_grounded_is_supported(self, service_session):
        with pytest.raises(ReportQualityModeError):
            await pipeline().prepare_evidence(request(ResearchMode.HYBRID))
        assert run_count(service_session) == 0


class TestEvidencePreparation:
    async def test_plan_sources_labels_and_gaps_are_persisted(self, service_session):
        web, academic, shared = providers()
        prepared = await pipeline(web=web, academic=academic).prepare_evidence(request())
        run = ResearchService().get_run(prepared.research_id)

        # configuration: opt-in pipeline, recorded and distinct from any legacy one
        configuration = run.configuration
        assert configuration.pipeline_version is PipelineVersion.REPORT_QUALITY_V1
        assert configuration.options["REPORT_QUALITY_SUBQUESTIONS_QUICK"] == 3
        assert "REPORT_QUALITY_ENABLED" not in configuration.options
        assert configuration.planner_prompt_version == PLANNER_PROMPT_VERSION
        assert run.status is RunStatus.RETRIEVING  # drafting is S4

        # plan rows, with their search queries
        texts = [s.text for s in sorted(run.subquestions, key=lambda s: s.position)]
        assert texts == [s.text for s in prepared.plan.subquestions]
        assert all(s.search_query.startswith("web: ") for s in run.subquestions)

        # sources carry the subquestion that first found them
        by_alias = dict(
            zip(
                [s.alias for s in prepared.plan.subquestions],
                sorted(run.subquestions, key=lambda s: s.position),
                strict=True,
            )
        )
        links = {link.source.fingerprint: link for link in run.research_sources}
        assert links[shared.fingerprint].subquestion_id == by_alias["Q1"].id
        assert prepared.evidence.found_by[shared.fingerprint] == ["Q1", "Q2"]
        assert links[web_candidate(1).fingerprint].subquestion_id == by_alias["Q2"].id

        # Q3's searches all failed: a listed gap, marked not addressed; the others undecided
        assert [(g.alias, g.gap_reason) for g in prepared.evidence.gaps] == [
            ("Q3", "provider_failure")
        ]
        assert by_alias["Q3"].addressed is False
        assert by_alias["Q1"].addressed is None and by_alias["Q2"].addressed is None
        assert any("failed for Q3" in w for w in prepared.warnings)

        # labels frozen at selection and persisted unchanged
        stored = {
            link.source.fingerprint: link.citation_label
            for link in run.research_sources
            if link.citation_label
        }
        assert stored == prepared.evidence.labels
        assert {
            label: links[fp].source.id for fp, label in stored.items()
        } == prepared.evidence.aliases.sources

    async def test_a_failed_plan_falls_back_and_still_runs(self, service_session):
        llm = FakePlannerLLM(LLMResponseInvalid(), LLMResponseInvalid())
        web = PerQuerySearch({"coffee": web_result(web_candidate(0))}, academic=False)
        academic = PerQuerySearch({}, academic=True)
        prepared = await pipeline(llm=llm, web=web, academic=academic).prepare_evidence(request())
        assert prepared.plan.fallback
        assert [s.text for s in prepared.plan.subquestions] == [TOPIC]
        assert not prepared.evidence.gaps

    async def test_every_provider_failing_fails_the_run_honestly(self, service_session):
        down = PerQuerySearch({"": SearchProviderUnavailable("tavily", "down")}, academic=False)
        down_academic = PerQuerySearch(
            {"": SearchProviderUnavailable("semantic_scholar", "down")}, academic=True
        )
        with pytest.raises(SearchProviderUnavailable):
            await pipeline(web=down, academic=down_academic).prepare_evidence(request())
        [run] = service_session.execute(select(ResearchRun)).scalars()
        service_session.refresh(run)
        assert run.status is RunStatus.FAILED and run.error_code

    async def test_nothing_retrieved_is_no_evidence(self, service_session):
        empty_web = PerQuerySearch({}, academic=False)
        empty_academic = PerQuerySearch({}, academic=True)
        with pytest.raises(NoEvidenceError):
            await pipeline(web=empty_web, academic=empty_academic).prepare_evidence(request())

    async def test_the_legacy_configuration_of_the_same_request_is_untouched(self, service_session):
        legacy_values = ResearchService(settings=settings()).configuration_values_for(
            __import__("app.schemas.research", fromlist=["ResearchRequest"]).ResearchRequest(
                topic=TOPIC, mode=ResearchMode.SEARCH_GROUNDED
            )
        )
        await pipeline().prepare_evidence(request())
        [configuration] = service_session.execute(select(RunConfiguration)).scalars()
        assert configuration.fingerprint != RunConfiguration.compute_fingerprint(legacy_values)
        assert configuration.pipeline_version is PipelineVersion.REPORT_QUALITY_V1
