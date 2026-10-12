"""
The final report and its evidence audit.

What must hold however the model behaves — unsupported claims listed rather than
silently deleted, conflicts mentioned, references only for validated sources, audit
numbers counted from rows — is exactly what these tests pin. The pipeline state is
built by the real orchestrator → extractor → verifier chain over fakes, as in
test_verification; only the finalizer's LLM is scripted per test.
"""

import pytest
from sqlalchemy import select

from app.core.constants import ConflictType, VerificationVerdict
from app.core.exceptions import ConflictError
from app.models import Report
from app.schemas.research import ResearchStartRequest
from app.services.claims import ClaimExtractor
from app.services.orchestration import ReportFinalizer, ResearchOrchestrator
from app.services.research_service import ResearchService
from app.services.vectorstore import VectorStore
from tests.unit.test_claim_extraction import FakeStructuredLLM, claim, extraction
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
from tests.unit.test_verification import (
    ABSTRACT_QUOTE,
    SUPPORTED_CLAIM,
    WEB_QUOTE,
    FakeVerifierLLM,
    judge,
)

UNSUPPORTED_CLAIM = "Standing desks double programmer salaries within a year."
FINAL_BODY = "# Final Report\n\n## Findings\n\nCaffeine aided recall in trials [S1].\n"


@pytest.fixture
async def chain(service_session, tmp_path):
    """Run → extract → verify → a finalizer builder, with scripted collaborators."""
    test_settings = settings()
    store_factory = lambda name: VectorStore(  # noqa: E731
        embedder=BagOfWordsEmbedder(),
        persist_dir=str(tmp_path / "chroma"),
        collection_name=name,
    )
    orchestrator = ResearchOrchestrator(
        settings=test_settings,
        research_service=ResearchService(settings=test_settings),
        web_search=FakeWebSearch(web_result(web_candidate())),
        academic_search=FakeAcademicSearch(academic_result(academic_candidate())),
        store_factory=store_factory,
        llm=FakeLLM(CITED_REPORT),
    )
    run_response = await orchestrator.run(ResearchStartRequest(topic=TOPIC, depth="medium"))
    web_label = next(s.label for s in run_response.sources if s.source_type == "web")

    async def extract(*extracted):
        await ClaimExtractor(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            llm=FakeStructuredLLM(extraction(*extracted)),
        ).extract(run_response.research_id)

    async def verify(verifier_llm):
        from app.services.verification import ClaimVerifier

        return await ClaimVerifier(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            store_factory=store_factory,
            llm=verifier_llm,
        ).verify(run_response.research_id)

    def finalizer(body: str = FINAL_BODY) -> ReportFinalizer:
        return ReportFinalizer(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            llm=FakeLLM(body),
        )

    return run_response, web_label, extract, verify, finalizer


async def seed_mixed_verification(web_label, extract, verify):
    """One supported claim (valid citation) and one that nothing supports."""
    await extract(
        claim(SUPPORTED_CLAIM, labels=[web_label]),
        claim(UNSUPPORTED_CLAIM, labels=[]),
    )
    llm = (
        FakeVerifierLLM()
        .on("caffeine intake", lambda p: judge(p, support_containing=WEB_QUOTE))
        .on(
            "Standing desks",
            lambda p: judge(p, verdict=VerificationVerdict.UNSUPPORTED),
        )
    )
    await verify(llm)


class TestFinalReport:
    async def test_unsupported_claims_are_listed_not_deleted(self, chain, service_session):
        run_response, web_label, extract, verify, finalizer = chain
        await seed_mixed_verification(web_label, extract, verify)

        result = await finalizer().finalize(run_response.research_id)

        assert "## Unverified and contested statements" in result.final_report
        assert UNSUPPORTED_CLAIM in result.final_report  # present, with its reason
        assert "Why:" in result.final_report
        [downgraded] = result.unverified_claims
        assert downgraded.claim_text == UNSUPPORTED_CLAIM
        assert downgraded.verdict == "unsupported"
        assert downgraded.reason

    async def test_references_list_only_validated_sources(self, chain):
        run_response, web_label, extract, verify, finalizer = chain
        await seed_mixed_verification(web_label, extract, verify)

        result = await finalizer().finalize(run_response.research_id)

        assert "## References" in result.final_report
        references = result.final_report.split("## References")[1]
        assert f"[{web_label}]" in references
        # The academic source had no valid citation, so it earns no reference.
        assert references.count("- **[S") == 1

    async def test_the_final_report_is_persisted_without_touching_the_draft(
        self, chain, service_session
    ):
        run_response, web_label, extract, verify, finalizer = chain
        await seed_mixed_verification(web_label, extract, verify)
        await finalizer().finalize(run_response.research_id)

        [report] = service_session.execute(select(Report)).scalars().all()
        assert report.final_markdown is not None
        assert "## Unverified and contested statements" in report.final_markdown
        assert report.markdown == CITED_REPORT.rstrip() + "\n" or CITED_REPORT in report.markdown
        assert report.finalized_at is not None

    async def test_audit_numbers_are_counted_from_rows(self, chain):
        run_response, web_label, extract, verify, finalizer = chain
        await seed_mixed_verification(web_label, extract, verify)

        audit = (await finalizer().finalize(run_response.research_id)).evidence_audit

        assert audit.total_claims == 2
        assert audit.supported == 1
        assert audit.unsupported == 1
        assert audit.partially_supported == audit.contradicted == 0
        assert audit.insufficient_evidence == 0
        assert audit.claims_requiring_citation == 2
        assert audit.claims_with_citations == 1
        assert audit.citation_coverage == 0.5
        assert audit.citations_total == 1
        assert audit.citations_by_status == {"valid": 1}
        assert audit.citation_validity == 1.0
        assert audit.conflicts_detected == 0

    async def test_conflicting_evidence_gets_its_own_section(self, chain):
        run_response, web_label, extract, verify, finalizer = chain
        await extract(claim(SUPPORTED_CLAIM, labels=[web_label]))
        llm = FakeVerifierLLM().on(
            "caffeine intake",
            lambda p: judge(
                p,
                support_containing=WEB_QUOTE,
                contradict_containing=ABSTRACT_QUOTE,
                verdict=VerificationVerdict.PARTIALLY_SUPPORTED,
                make_conflict=ConflictType.DIRECTIONAL_DISAGREEMENT,
            ),
        )
        await verify(llm)

        result = await finalizer().finalize(run_response.research_id)

        assert "## Conflicting evidence" in result.final_report
        assert "no significant improvement" in result.final_report.lower()
        assert result.evidence_audit.conflicts_detected == 1
        assert result.evidence_audit.partially_supported == 1

    async def test_rogue_citations_in_the_body_are_warned_about(self, chain):
        run_response, web_label, extract, verify, finalizer = chain
        await seed_mixed_verification(web_label, extract, verify)

        result = await finalizer(
            "# Final\n\n## Findings\n\nA finding citing nothing valid [S9].\n"
        ).finalize(run_response.research_id)

        assert any("S9" in warning for warning in result.warnings)


class TestFinalizeGuards:
    async def test_finalizing_before_verification_is_refused_with_the_count(self, chain):
        run_response, web_label, extract, _, finalizer = chain
        await extract(claim(SUPPORTED_CLAIM, labels=[web_label]))
        with pytest.raises(ConflictError, match="not verified yet"):
            await finalizer().finalize(run_response.research_id)

    async def test_finalizing_without_claims_is_refused(self, chain):
        run_response, _, _, _, finalizer = chain
        with pytest.raises(ConflictError, match="extract and verify first"):
            await finalizer().finalize(run_response.research_id)

    async def test_finalizing_twice_is_refused(self, chain):
        run_response, web_label, extract, verify, finalizer = chain
        await seed_mixed_verification(web_label, extract, verify)
        await finalizer().finalize(run_response.research_id)
        with pytest.raises(ConflictError, match="already has a final report"):
            await finalizer().finalize(run_response.research_id)
