"""
Claim extraction, with a scripted LLM and a real database.

The state under test is what gets *stored*: typed claims, resolved citations, the
fabricated-marker case, the retry policy, and the refusal to store anything twice
or partially. The run being extracted from is produced by the real orchestrator
over fakes (same approach as test_orchestrator), so the labels the extractor
resolves against were persisted by the real code path, not seeded by hand.
"""

import pytest

from app.core.constants import CitationStatus, ClaimImportance, ClaimType
from app.core.exceptions import ConflictError, NotFoundError
from app.repositories import ClaimRepository
from app.schemas.research import ResearchStartRequest
from app.services.claims import (
    MAX_CLAIMS_PER_REPORT,
    ClaimExtractionFailed,
    ClaimExtractionModel,
    ClaimExtractor,
    ExtractedClaimModel,
)
from app.services.llm import LLMResponseInvalid, StructuredLLMResponse, TokenUsage
from app.services.orchestration import ResearchOrchestrator
from app.services.research_service import ResearchService
from app.services.vectorstore import VectorStore
from tests.unit.test_orchestrator import (
    CITED_REPORT,
    TOPIC,
    FakeAcademicSearch,
    FakeLLM,
    FakeWebSearch,
    academic_candidate,
    academic_result,
    settings,
    web_candidate,
    web_result,
)
from tests.unit.test_vector_store import BagOfWordsEmbedder


def claim(
    text: str = "Caffeine improved recall in several controlled trials.",
    *,
    claim_type: ClaimType = ClaimType.FACTUAL,
    importance: ClaimImportance = ClaimImportance.HIGH,
    section: str | None = "Findings",
    labels: list[str] | None = None,
    requires_citation: bool = True,
) -> ExtractedClaimModel:
    return ExtractedClaimModel(
        claim_text=text,
        claim_type=claim_type,
        importance=importance,
        section=section,
        citation_labels=["S1"] if labels is None else labels,
        requires_citation=requires_citation,
    )


def extraction(*claims: ExtractedClaimModel) -> ClaimExtractionModel:
    return ClaimExtractionModel(claims=list(claims))


class FakeStructuredLLM:
    """Scripted structured outputs; an Exception in the queue is raised instead."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls_made = 0
        self.total_usage = TokenUsage.zero()
        self.prompts: list[str] = []

    async def generate_structured_output(self, prompt, schema, *, system=None, model=None,
                                         temperature=None, max_tokens=None):
        self.prompts.append(prompt)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        self.calls_made += 1
        usage = TokenUsage(prompt_tokens=400, completion_tokens=200, total_tokens=600)
        self.total_usage = self.total_usage + usage
        return StructuredLLMResponse[ClaimExtractionModel](
            data=outcome,
            text=outcome.model_dump_json(),
            model="fake-extractor-2026",
            finish_reason="stop",
            usage=usage,
            latency_seconds=0.01,
        )

    async def aclose(self):  # pragma: no cover - extractor only closes what it built
        pass


@pytest.fixture
async def completed_run(service_session, tmp_path):
    """A real completed search_grounded run (over fakes) whose labels are persisted."""
    test_settings = settings()
    orchestrator = ResearchOrchestrator(
        settings=test_settings,
        research_service=ResearchService(settings=test_settings),
        web_search=FakeWebSearch(web_result(web_candidate())),
        academic_search=FakeAcademicSearch(academic_result(academic_candidate())),
        store_factory=lambda name: VectorStore(
            embedder=BagOfWordsEmbedder(),
            persist_dir=str(tmp_path / "chroma"),
            collection_name=name,
        ),
        llm=FakeLLM(CITED_REPORT),
    )
    response = await orchestrator.run(ResearchStartRequest(topic=TOPIC, depth="medium"))
    return response, test_settings


def extractor_for(llm, test_settings) -> ClaimExtractor:
    return ClaimExtractor(
        settings=test_settings,
        research_service=ResearchService(settings=test_settings),
        llm=llm,
    )


class TestExtraction:
    async def test_claims_are_extracted_typed_and_stored(self, completed_run, service_session):
        run_response, test_settings = completed_run
        llm = FakeStructuredLLM(
            extraction(
                claim(labels=["S1"]),
                claim(
                    "Several trials reported a null result in older adults.",
                    claim_type=ClaimType.COMPARATIVE,
                    importance=ClaimImportance.MEDIUM,
                    labels=["S2"],
                ),
            )
        )
        result = await extractor_for(llm, test_settings).extract(run_response.research_id)

        assert result.claim_count == 2
        assert result.research_id == run_response.research_id
        assert result.retried is False
        first, second = result.claims
        assert first.claim_text.startswith("Caffeine improved recall")
        assert first.claim_type is ClaimType.FACTUAL
        assert first.importance is ClaimImportance.HIGH
        assert first.claim_id == first.id
        assert second.claim_type is ClaimType.COMPARATIVE

        stored = ClaimRepository(service_session).list_for_report(result.id)
        assert [c.position for c in stored] == [0, 1]
        assert stored[0].claim_type is ClaimType.FACTUAL
        assert stored[0].extractor_model == "fake-extractor-2026"

    async def test_resolvable_markers_become_unvalidated_citations(
        self, completed_run, service_session
    ):
        run_response, test_settings = completed_run
        known_label = run_response.sources[0].label  # a label the run really assigned
        llm = FakeStructuredLLM(extraction(claim(labels=[known_label])))
        result = await extractor_for(llm, test_settings).extract(run_response.research_id)

        [stored_claim] = ClaimRepository(service_session).list_for_report(result.id)
        [citation] = stored_claim.citations
        assert citation.status is CitationStatus.UNVALIDATED
        assert citation.source_id is not None
        assert citation.marker == known_label
        assert result.claims[0].citation_ids == [citation.id]
        assert result.claims[0].cited_source_ids == [citation.source_id]

    async def test_an_unknown_marker_is_a_fabricated_citation(
        self, completed_run, service_session
    ):
        """The structural case: the report cited a source the run never had."""
        run_response, test_settings = completed_run
        llm = FakeStructuredLLM(extraction(claim(labels=["S9"])))
        result = await extractor_for(llm, test_settings).extract(run_response.research_id)

        [stored_claim] = ClaimRepository(service_session).list_for_report(result.id)
        [citation] = stored_claim.citations
        assert citation.status is CitationStatus.FABRICATED
        assert citation.source_id is None
        assert "no source under that label" in citation.failure_reason
        assert result.claims[0].cited_source_ids == []

    async def test_claims_map_to_their_report_section(self, completed_run, service_session):
        run_response, test_settings = completed_run
        llm = FakeStructuredLLM(
            extraction(
                claim(section="Findings"),
                claim("A claim before any heading.", section=None, labels=[]),
                claim("A claim naming a section that does not exist.",
                      section="Imaginary", labels=[]),
            )
        )
        result = await extractor_for(llm, test_settings).extract(run_response.research_id)

        assert result.claims[0].section == "Findings"
        assert result.claims[1].section is None
        assert result.claims[2].section is None  # unmatched heading is not invented

        stored = ClaimRepository(service_session).list_for_report(result.id)
        assert stored[0].report_section_id is not None
        assert stored[1].report_section_id is None

    async def test_an_oversized_extraction_is_capped(self, completed_run):
        run_response, test_settings = completed_run
        many = [
            claim(f"Claim number {i} asserts a distinct finding.", labels=[])
            for i in range(MAX_CLAIMS_PER_REPORT + 5)
        ]
        llm = FakeStructuredLLM(extraction(*many))
        result = await extractor_for(llm, test_settings).extract(run_response.research_id)
        assert result.claim_count == MAX_CLAIMS_PER_REPORT


class TestRetryAndFailSafety:
    async def test_a_parse_failure_is_retried_once(self, completed_run):
        run_response, test_settings = completed_run
        llm = FakeStructuredLLM(
            LLMResponseInvalid("did not validate"),
            extraction(claim()),
        )
        result = await extractor_for(llm, test_settings).extract(run_response.research_id)

        assert result.retried is True
        assert result.claim_count == 1
        assert "IMPORTANT" in llm.prompts[1]  # the retry carried the reminder

    async def test_a_second_failure_fails_safely_storing_nothing(
        self, completed_run, service_session
    ):
        run_response, test_settings = completed_run
        llm = FakeStructuredLLM(LLMResponseInvalid("first"), LLMResponseInvalid("second"))
        with pytest.raises(ClaimExtractionFailed):
            await extractor_for(llm, test_settings).extract(run_response.research_id)

        assert ClaimRepository(service_session).count() == 0  # nothing half-stored

    async def test_extraction_cannot_run_twice(self, completed_run):
        run_response, test_settings = completed_run
        llm = FakeStructuredLLM(extraction(claim()))
        extractor = extractor_for(llm, test_settings)
        await extractor.extract(run_response.research_id)

        with pytest.raises(ConflictError, match="already has claims"):
            await extractor.extract(run_response.research_id)

    async def test_a_run_without_a_report_is_a_clear_404(self, service_session):
        test_settings = settings()
        research = ResearchService(settings=test_settings)
        from app.schemas.research import ResearchRequest

        run = research.create_run(ResearchRequest(topic=TOPIC))
        extractor = extractor_for(FakeStructuredLLM(extraction(claim())), test_settings)
        with pytest.raises(NotFoundError, match="no report"):
            await extractor.extract(run.id)


class TestExtractedClaimModel:
    def test_labels_are_normalised_and_deduplicated(self):
        model = claim(labels=["[s1]", " S2 ", "S1"])
        assert model.citation_labels == ["S1", "S2"]

    def test_heading_markers_are_stripped_from_sections(self):
        """Observed live: the model echoes '## Findings', the stored heading is bare,
        and an unnormalised value silently maps every claim to no section."""
        assert claim(section="## Findings").section == "Findings"
        assert claim(section="  ###  Findings ").section == "Findings"
        assert claim(section="##").section is None

    def test_whitespace_in_claim_text_is_collapsed(self):
        model = claim("A   claim\nwith   messy   spacing everywhere.")
        assert model.claim_text == "A claim with messy spacing everywhere."

    def test_a_trivial_fragment_is_rejected(self):
        with pytest.raises(ValueError):
            claim("Too")
