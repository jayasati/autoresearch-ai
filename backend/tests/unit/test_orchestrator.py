"""
The end-to-end pipeline, with fake providers and a real database and vector store.

The orchestration under test is the wiring: statuses advance, sources persist,
chunks index, evidence labels map to citations, failures stop the run honestly.
So the database is real (throwaway SQLite via `service_session`), ChromaDB is real
(tmp_path), and the *providers* are fakes: canned search results, a deterministic
embedder, a scripted LLM. No test touches the network or spends a token.

Placeholder data uses `example.invalid` URLs and `10.0000/invalid.*` DOIs throughout.
"""

import pytest

from app.core.config import Settings
from app.core.constants import EvidenceDepth, RunStatus
from app.schemas.research import ResearchStartRequest, ResearchStartResponse
from app.services.llm import LLMNotConfigured, LLMResponse, TokenUsage
from app.services.orchestration import (
    DEPTH_PROFILES,
    NoEvidenceError,
    ResearchOrchestrator,
    UncitedReportError,
)
from app.services.research_service import ResearchService
from app.services.retrieval import (
    AcademicCandidateSource,
    AcademicSearchResult,
    CandidateSource,
    SearchProviderUnavailable,
    WebSearchResult,
)
from app.services.vectorstore import VectorStore
from tests.unit.test_vector_store import BagOfWordsEmbedder

TOPIC = "does caffeine consumption improve working memory"

CITED_REPORT = (
    "# Caffeine and Working Memory\n\n"
    "## Findings\n\n"
    "Caffeine improved recall in several trials [S1]. One abstract reports a null "
    "result in older adults [S2].\n"
)

WEB_TEXT = (
    "A randomized controlled trial found that moderate caffeine intake improved "
    "working memory scores in adults. The effect was strongest for short-term recall "
    "tasks performed within two hours of consumption. "
) * 3

ABSTRACT_TEXT = (
    "We examined caffeine and working memory in a sample of older adults. "
    "No significant improvement in recall accuracy was observed, although reaction "
    "times improved modestly. These findings suggest the effect is age-dependent. "
) * 3


def settings() -> Settings:
    return Settings(
        APP_ENV="test",
        OPENAI_API_KEY="sk-test-key",
        AZURE_OPENAI_ENDPOINT=None,
        AZURE_OPENAI_API_KEY=None,
        AZURE_OPENAI_DEPLOYMENT_NAME=None,
    )


def web_candidate(index: int = 0) -> CandidateSource:
    candidate = CandidateSource.from_provider_result(
        {
            "url": f"https://example.invalid/web-{index}",
            "title": f"Web Article {index}",
            "content": WEB_TEXT,
            "score": 0.8,
        },
        rank=index,
        query=TOPIC,
    )
    assert candidate is not None
    return candidate


def academic_candidate(index: int = 0, *, abstract: str | None = ABSTRACT_TEXT):
    return AcademicCandidateSource(
        paper_id=f"paper-{index}",
        title=f"Paper Under Test {index}",
        abstract=abstract,
        doi=f"10.0000/invalid.{index:03d}",
        url=f"https://example.invalid/paper-{index}",
        authors=["A. Author"],
        year=2024,
        venue="Journal of Tests",
        citation_count=5,
        fingerprint=f"test-paper-{index}",
        rank=index,
        query=TOPIC,
        evidence_depth=EvidenceDepth.ABSTRACT if abstract else EvidenceDepth.METADATA,
    )


def web_result(*candidates) -> WebSearchResult:
    return WebSearchResult(query=TOPIC, candidates=list(candidates), requested_results=8)


def academic_result(*candidates) -> AcademicSearchResult:
    return AcademicSearchResult(query=TOPIC, candidates=list(candidates), requested_limit=8)


class FakeWebSearch:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[str] = []

    async def search(self, query, *, max_results=None):
        self.calls.append(query)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeAcademicSearch:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[str] = []

    async def search(self, query, *, limit=None):
        self.calls.append(query)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeLLM:
    """Scripted responses, with the usage-tracking surface the orchestrator reads."""

    def __init__(self, *texts: str, error: Exception | None = None):
        self.texts = list(texts) or [CITED_REPORT]
        self.error = error
        self.calls_made = 0
        self.total_usage = TokenUsage.zero()
        self.prompts: list[str] = []

    async def generate_text(self, prompt, *, system=None, model=None,
                            temperature=None, max_tokens=None) -> LLMResponse:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        self.calls_made += 1
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        self.total_usage = self.total_usage + usage
        return LLMResponse(
            text=text, model="fake-model-2026", finish_reason="stop",
            usage=usage, latency_seconds=0.01,
        )

    async def aclose(self):  # pragma: no cover - the orchestrator only closes what it built
        pass


@pytest.fixture
def orchestrator_factory(service_session, tmp_path):
    """An orchestrator over throwaway storage, with injectable providers."""

    def build(
        web=None,
        academic=None,
        llm=None,
    ) -> ResearchOrchestrator:
        test_settings = settings()
        return ResearchOrchestrator(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            web_search=web if web is not None else FakeWebSearch(web_result(web_candidate())),
            academic_search=(
                academic if academic is not None
                else FakeAcademicSearch(academic_result(academic_candidate()))
            ),
            store_factory=lambda name: VectorStore(
                embedder=BagOfWordsEmbedder(),
                persist_dir=str(tmp_path / "chroma"),
                collection_name=name,
            ),
            llm=llm if llm is not None else FakeLLM(),
        )

    return build


async def start(orchestrator, depth="medium") -> ResearchStartResponse:
    return await orchestrator.run(ResearchStartRequest(topic=TOPIC, depth=depth))


class TestHappyPath:
    async def test_the_full_pipeline_produces_a_cited_report(self, orchestrator_factory):
        orchestrator = orchestrator_factory()
        response = await start(orchestrator)

        assert response.topic == TOPIC
        assert response.status is RunStatus.COMPLETED
        assert "[S1]" in response.report
        assert "## Sources" in response.report  # appended by code, from real sources
        assert response.research_id is not None

    async def test_sources_are_labelled_and_resolvable(self, orchestrator_factory):
        response = await start(orchestrator_factory())

        labels = [source.label for source in response.sources]
        assert labels == [f"S{i + 1}" for i in range(len(labels))]
        for source in response.sources:
            assert source.source_id is not None  # persisted, not just reported
            assert source.fingerprint
            assert source.chunks_used >= 1
        assert any(source.cited_in_report for source in response.sources)

    async def test_the_run_and_report_are_persisted(self, orchestrator_factory):
        orchestrator = orchestrator_factory()
        response = await start(orchestrator)

        run = orchestrator.research.get_run(response.research_id)
        assert run.status is RunStatus.COMPLETED
        summary = orchestrator.research.run_summary(response.research_id)
        assert summary["report_id"] is not None
        assert summary["source_count"] == 2  # one web, one academic

    async def test_metadata_reports_what_actually_happened(self, orchestrator_factory):
        response = await start(orchestrator_factory())
        metadata = response.execution_metadata

        assert metadata.mode == "search_grounded"
        assert metadata.web_candidates == 1
        assert metadata.academic_candidates == 1
        assert metadata.documents_processed == 2
        assert metadata.chunks_indexed >= 2
        assert metadata.chunks_retrieved >= 1
        assert metadata.llm_calls == 1
        assert metadata.token_usage == {
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        }
        assert metadata.provider_failures == []
        for stage in ("retrieval", "processing", "indexing", "evidence_retrieval", "synthesis"):
            assert stage in metadata.timings_seconds

    async def test_depth_bounds_the_evidence(self, orchestrator_factory):
        web = FakeWebSearch(web_result(*(web_candidate(i) for i in range(4))))
        academic = FakeAcademicSearch(academic_result(*(academic_candidate(i) for i in range(4))))
        response = await start(orchestrator_factory(web=web, academic=academic), depth="quick")
        assert response.execution_metadata.chunks_retrieved <= DEPTH_PROFILES["quick"].top_k


class TestPartialRetrieval:
    async def test_one_provider_failing_is_recorded_not_fatal(self, orchestrator_factory):
        web = FakeWebSearch(SearchProviderUnavailable("tavily", "it is down"))
        response = await start(orchestrator_factory(web=web))

        assert response.status is RunStatus.COMPLETED
        assert len(response.execution_metadata.provider_failures) == 1
        failure = response.execution_metadata.provider_failures[0]
        assert failure.provider == "tavily"
        assert all(source.source_type == "academic" for source in response.sources)

    async def test_both_providers_failing_fails_the_run(self, orchestrator_factory):
        orchestrator = orchestrator_factory(
            web=FakeWebSearch(SearchProviderUnavailable("tavily", "down")),
            academic=FakeAcademicSearch(SearchProviderUnavailable("semantic_scholar", "down")),
        )
        with pytest.raises(SearchProviderUnavailable):
            await start(orchestrator)

        run = orchestrator.research.list_runs()[0]
        assert run.status is RunStatus.FAILED
        assert run.error_code == "external_service_error"


class TestNoEvidence:
    async def test_zero_candidates_fails_honestly(self, orchestrator_factory):
        orchestrator = orchestrator_factory(
            web=FakeWebSearch(web_result()), academic=FakeAcademicSearch(academic_result())
        )
        with pytest.raises(NoEvidenceError):
            await start(orchestrator)
        assert orchestrator.research.list_runs()[0].error_code == "no_evidence"

    async def test_metadata_only_sources_are_not_evidence(self, orchestrator_factory):
        """Papers with no abstract are recorded but cannot ground a report;
        no text anywhere means no report, not an empty-handed success."""
        academic = FakeAcademicSearch(
            academic_result(academic_candidate(0, abstract=None))
        )
        orchestrator = orchestrator_factory(web=FakeWebSearch(web_result()), academic=academic)
        with pytest.raises(NoEvidenceError):
            await start(orchestrator)


class TestSynthesisFailures:
    async def test_an_uncited_draft_is_retried_then_fails(self, orchestrator_factory):
        llm = FakeLLM("No citations here.", "Still no citations.")
        orchestrator = orchestrator_factory(llm=llm)
        with pytest.raises(UncitedReportError):
            await start(orchestrator)

        assert llm.calls_made == 2
        assert orchestrator.research.list_runs()[0].error_code == "uncited_report"

    async def test_a_retry_that_cites_succeeds_with_a_warning(self, orchestrator_factory):
        llm = FakeLLM("No citations here.", CITED_REPORT)
        response = await start(orchestrator_factory(llm=llm))

        assert response.status is RunStatus.COMPLETED
        assert llm.calls_made == 2
        assert any("no citations" in warning for warning in response.execution_metadata.warnings)

    async def test_unknown_labels_are_warned_about_not_hidden(self, orchestrator_factory):
        llm = FakeLLM("# T\n\nA finding [S1]. Another [S9].\n")
        response = await start(orchestrator_factory(llm=llm))

        assert response.status is RunStatus.COMPLETED
        assert any("S9" in warning for warning in response.execution_metadata.warnings)


class TestConfiguration:
    async def test_no_llm_provider_fails_before_anything_is_spent(
        self, orchestrator_factory
    ):
        orchestrator = orchestrator_factory()
        orchestrator.settings = settings().model_copy(update={"OPENAI_API_KEY": None})

        with pytest.raises(LLMNotConfigured):
            await start(orchestrator)
        assert orchestrator.research.list_runs() == []  # no run row, no searches
