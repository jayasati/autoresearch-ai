"""
Verification, citation validation and conflict detection, with a scripted verifier
and a real database and vector store.

The chain under test is the stored one: Evidence rows with enforced quotes,
VerificationResults with the downgrade rule, Citation rows promoted valid only when
a supporting passage belongs to the cited source, Conflict rows only between
passages that actually disagree. The run is produced by the real orchestrator over
fakes (which now also persists document/chunk rows), claims by the real extractor
over a scripted LLM — so everything the verifier touches was written by real code.

The scripted verifier is prompt-aware: it parses the passage blocks out of the real
prompt and decides relations from passage *content*, because which chunk ranks P1
for a given claim is the embedder's business, not the test's.
"""

import re

import pytest
from sqlalchemy import select

from app.core.constants import (
    CitationStatus,
    ClaimImportance,
    ClaimType,
    ConflictType,
    EvidenceRelation,
    VerificationVerdict,
)
from app.models import Conflict, Evidence, VerificationResult
from app.repositories import ClaimRepository
from app.schemas.research import ResearchStartRequest
from app.services.claims import ClaimExtractor
from app.services.llm import LLMResponseInvalid, StructuredLLMResponse, TokenUsage
from app.services.orchestration import ResearchOrchestrator
from app.services.research_service import ResearchService
from app.services.vectorstore import VectorStore
from app.services.verification import (
    ClaimVerificationModel,
    ClaimVerifier,
    ConflictReport,
    NothingToVerify,
    PassageAssessment,
)
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

# Verbatim substrings of the two documents' chunk texts (see test_orchestrator).
WEB_QUOTE = "moderate caffeine intake improved working memory scores in adults"
ABSTRACT_QUOTE = "No significant improvement in recall accuracy was observed"

PASSAGE_PATTERN = re.compile(r"\[(P\d+)\] \(from: .*?\)\n(.*?)(?=\n\n\[P\d|\n\nVerify now)", re.S)
CLAIM_PATTERN = re.compile(r"Claim to verify:\n(.*?)\n\nEvidence passages:", re.S)


def parse_prompt(prompt: str) -> tuple[str, dict[str, str]]:
    claim_text = CLAIM_PATTERN.search(prompt).group(1)
    passages = {label: text for label, text in PASSAGE_PATTERN.findall(prompt)}
    return claim_text, passages


def judge(
    passages: dict[str, str],
    *,
    support_containing: str | None = None,
    contradict_containing: str | None = None,
    support_quote: str | None = None,
    contradict_quote: str | None = None,
    verdict: VerificationVerdict = VerificationVerdict.SUPPORTED,
    conflict: ConflictReport | None = None,
    make_conflict: ConflictType | None = None,
) -> ClaimVerificationModel:
    """Build a verification: relations assigned by passage content, not label."""
    assessments, support_label, contradict_label = [], None, None
    for label, text in passages.items():
        if support_containing and support_containing in text and support_label is None:
            support_label = label
            assessments.append(PassageAssessment(
                passage=label, relation=EvidenceRelation.SUPPORTS,
                quote=support_quote or support_containing,
            ))
        elif contradict_containing and contradict_containing in text and contradict_label is None:
            contradict_label = label
            assessments.append(PassageAssessment(
                passage=label, relation=EvidenceRelation.CONTRADICTS,
                quote=contradict_quote or contradict_containing,
            ))
        else:
            assessments.append(PassageAssessment(
                passage=label, relation=EvidenceRelation.NEUTRAL, quote=None
            ))
    if make_conflict is not None and support_label and contradict_label:
        conflict = ConflictReport(
            kind="direct",
            conflict_type=make_conflict,
            supporting_passage=support_label,
            contradicting_passage=contradict_label,
            description=(
                "One passage reports improved working memory in adults; the other "
                "reports no significant improvement in older adults."
            ),
        )
    return ClaimVerificationModel(
        verdict=verdict, confidence=0.85,
        rationale="Judged from the provided passages only.",
        passages=assessments, conflict=conflict,
    )


class FakeVerifierLLM:
    """Routes each real prompt to a handler by the claim text it carries."""

    def __init__(self):
        self.handlers: list[tuple[str, object]] = []  # (claim substring, fn | exception)
        self.calls_made = 0
        self.total_usage = TokenUsage.zero()

    def on(self, claim_substring: str, handler) -> "FakeVerifierLLM":
        self.handlers.append((claim_substring, handler))
        return self

    async def generate_structured_output(self, prompt, schema, *, system=None, model=None,
                                         temperature=None, max_tokens=None):
        claim_text, passages = parse_prompt(prompt)
        for substring, handler in self.handlers:
            if substring in claim_text:
                if isinstance(handler, Exception):
                    raise handler
                result = handler(passages)
                break
        else:  # pragma: no cover - a test wiring mistake, not a behaviour
            raise AssertionError(f"no handler for claim: {claim_text!r}")
        self.calls_made += 1
        usage = TokenUsage(prompt_tokens=300, completion_tokens=120, total_tokens=420)
        self.total_usage = self.total_usage + usage
        return StructuredLLMResponse[ClaimVerificationModel](
            data=result, text=result.model_dump_json(), model="fake-verifier-2026",
            finish_reason="stop", usage=usage, latency_seconds=0.01,
        )

    async def aclose(self):  # pragma: no cover
        pass


@pytest.fixture
async def pipeline(service_session, tmp_path):
    """A completed run with extracted claims, plus the pieces to verify it."""
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
    academic_label = next(
        (s.label for s in run_response.sources if s.source_type == "academic"), None
    )

    async def extract(*extracted_claims):
        extractor = ClaimExtractor(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            llm=FakeStructuredLLM(extraction(*extracted_claims)),
        )
        return await extractor.extract(run_response.research_id)

    def verifier(llm) -> ClaimVerifier:
        return ClaimVerifier(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            store_factory=store_factory,
            llm=llm,
        )

    return run_response, web_label, academic_label, extract, verifier


SUPPORTED_CLAIM = "Moderate caffeine intake improved working memory scores in adult trials."


class TestVerificationAndCitationValidation:
    async def test_a_supported_claim_gets_a_valid_citation(self, pipeline, service_session):
        run_response, web_label, _, extract, verifier = pipeline
        await extract(claim(SUPPORTED_CLAIM, labels=[web_label]))
        llm = FakeVerifierLLM().on(
            "caffeine intake", lambda p: judge(p, support_containing=WEB_QUOTE)
        )

        result = await verifier(llm).verify(run_response.research_id)

        assert result.claims_verified == 1 and result.claims_skipped == 0
        [outcome] = result.results
        assert outcome.verdict is VerificationVerdict.SUPPORTED
        assert outcome.downgraded is False
        assert outcome.citation_assessment == "valid"
        assert outcome.citations == {web_label: "valid"}
        assert result.citation_statuses.get("valid") == 1

        [stored] = service_session.execute(select(VerificationResult)).scalars().all()
        assert stored.verdict is VerificationVerdict.SUPPORTED
        assert stored.supporting_evidence_id is not None
        evidence = service_session.execute(select(Evidence)).scalars().all()
        assert len(evidence) == outcome.evidence_count >= 1
        supporting = [e for e in evidence if e.relation is EvidenceRelation.SUPPORTS]
        assert supporting and supporting[0].quoted_text == WEB_QUOTE
        assert supporting[0].quote_start_char is not None

    async def test_citation_validity_requires_support_from_the_cited_source(
        self, pipeline, service_session
    ):
        """The spec's rule: the source existing and being topical count for nothing.
        The claim cites the ACADEMIC source, but only the WEB passage supports."""
        run_response, web_label, academic_label, extract, verifier = pipeline
        await extract(claim(SUPPORTED_CLAIM, labels=[academic_label]))
        llm = FakeVerifierLLM().on(
            "caffeine intake", lambda p: judge(p, support_containing=WEB_QUOTE)
        )

        result = await verifier(llm).verify(run_response.research_id)

        [outcome] = result.results
        assert outcome.citations == {academic_label: "misattributed"}
        assert outcome.citation_assessment == "invalid"
        claim_row = ClaimRepository(service_session).list_for_report(result.report_id)[0]
        [citation] = claim_row.citations
        assert citation.status is CitationStatus.MISATTRIBUTED
        assert "support" in citation.failure_reason
        assert citation.validated_at is not None

    async def test_a_fabricated_quote_downgrades_the_verdict(self, pipeline, service_session):
        """The verifier asserts support but its quote is not in the passage: the
        relation is neutralised and the stored verdict is the documented downgrade."""
        run_response, web_label, _, extract, verifier = pipeline
        await extract(claim(SUPPORTED_CLAIM, labels=[web_label]))
        llm = FakeVerifierLLM().on(
            "caffeine intake",
            lambda p: judge(
                p, support_containing=WEB_QUOTE,
                support_quote="words that appear in no passage at all",
            ),
        )

        result = await verifier(llm).verify(run_response.research_id)

        [outcome] = result.results
        assert outcome.verdict is VerificationVerdict.NOT_ENOUGH_EVIDENCE
        assert outcome.downgraded is True
        assert outcome.citations == {web_label: "misattributed"}
        assert any("not verbatim" in warning for warning in result.warnings)

    async def test_a_claim_without_citations_is_missing(self, pipeline):
        run_response, _, _, extract, verifier = pipeline
        await extract(claim(SUPPORTED_CLAIM, labels=[]))
        llm = FakeVerifierLLM().on(
            "caffeine intake", lambda p: judge(p, support_containing=WEB_QUOTE)
        )
        result = await verifier(llm).verify(run_response.research_id)
        assert result.results[0].citation_assessment == "missing"


class TestConflictDetection:
    async def test_a_real_disagreement_is_stored(self, pipeline, service_session):
        run_response, web_label, _, extract, verifier = pipeline
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

        result = await verifier(llm).verify(run_response.research_id)

        assert result.conflicts_detected == 1
        [conflict_read] = result.conflicts
        assert conflict_read.kind == "direct"
        assert conflict_read.conflict_type is ConflictType.DIRECTIONAL_DISAGREEMENT
        assert result.results[0].citation_assessment == "conflicting"

        [stored] = service_session.execute(select(Conflict)).scalars().all()
        assert stored.claim_id == result.results[0].claim_id
        assert stored.evidence_a_id != stored.evidence_b_id
        assert "no significant improvement" in stored.description.lower()
        assert stored.detector_model == "fake-verifier-2026"

    async def test_a_conflict_not_matching_enforced_relations_is_dropped(
        self, pipeline, service_session
    ):
        """The verifier names a conflict, but its 'contradicting' passage was judged
        neutral: stored conflicts must reflect enforced relations, so it is dropped."""
        run_response, web_label, _, extract, verifier = pipeline
        await extract(claim(SUPPORTED_CLAIM, labels=[web_label]))

        def phantom_conflict(passages):
            verification = judge(passages, support_containing=WEB_QUOTE)
            support_label = next(
                a.passage for a in verification.passages
                if a.relation is EvidenceRelation.SUPPORTS
            )
            neutral_label = next(
                a.passage for a in verification.passages
                if a.relation is EvidenceRelation.NEUTRAL
            )
            verification.conflict = ConflictReport(
                kind="nuanced",
                conflict_type=ConflictType.SCOPE_MISMATCH,
                supporting_passage=support_label,
                contradicting_passage=neutral_label,
                description="P-labels disagree about something they do not.",
            )
            return verification

        llm = FakeVerifierLLM().on("caffeine intake", phantom_conflict)
        result = await verifier(llm).verify(run_response.research_id)

        assert result.conflicts_detected == 0
        assert any("did not match the enforced" in warning for warning in result.warnings)
        assert service_session.execute(select(Conflict)).scalars().all() == []


class TestSpecScenarios:
    """The spec's required claim-level outcomes, each stored end to end.

    Together with the citation-misattribution, conflict and no-evidence tests
    above, these pin every scenario the verifier must distinguish: numerical
    mismatch, partial support, contradiction, insufficient evidence, and that
    unsupported/insufficient verdicts are never silently converted to anything
    stronger — nor contradiction manufactured from mere absence.
    """

    async def test_a_numerical_mismatch_is_stored_as_contradicted_with_the_quote(
        self, pipeline, service_session
    ):
        """The claim asserts a figure the passage opposes: contradicted, anchored to
        the contradicting passage with its verbatim quote and offsets — and the cited
        source, having supported nothing, is misattributed."""
        run_response, web_label, _, extract, verifier = pipeline
        await extract(
            claim("Recall accuracy improved by 6.5% in older adults.", labels=[web_label])
        )
        llm = FakeVerifierLLM().on(
            "6.5%",
            lambda p: judge(
                p,
                contradict_containing=ABSTRACT_QUOTE,
                verdict=VerificationVerdict.CONTRADICTED,
            ),
        )

        result = await verifier(llm).verify(run_response.research_id)

        [outcome] = result.results
        assert outcome.verdict is VerificationVerdict.CONTRADICTED
        assert outcome.downgraded is False
        assert outcome.citation_assessment == "invalid"
        [stored] = service_session.execute(select(VerificationResult)).scalars().all()
        assert stored.supporting_evidence_id is not None  # anchored to the contradiction
        contradicting = [
            e for e in service_session.execute(select(Evidence)).scalars().all()
            if e.relation is EvidenceRelation.CONTRADICTS
        ]
        assert contradicting and contradicting[0].quoted_text == ABSTRACT_QUOTE
        assert contradicting[0].quote_start_char is not None

    async def test_partial_support_keeps_the_verdict_and_grades_the_citation(
        self, pipeline
    ):
        """A compound claim only partly established: the verdict stays
        partially_supported (not inflated to supported, not collapsed to
        contradicted) and the citation is partially_valid, not valid."""
        run_response, web_label, _, extract, verifier = pipeline
        await extract(
            claim(
                "Caffeine improved working memory and eliminated age-related decline.",
                labels=[web_label],
            )
        )
        llm = FakeVerifierLLM().on(
            "eliminated age-related decline",
            lambda p: judge(
                p,
                support_containing=WEB_QUOTE,
                verdict=VerificationVerdict.PARTIALLY_SUPPORTED,
            ),
        )

        result = await verifier(llm).verify(run_response.research_id)

        [outcome] = result.results
        assert outcome.verdict is VerificationVerdict.PARTIALLY_SUPPORTED
        assert outcome.downgraded is False
        assert outcome.citations == {web_label: "valid"}
        assert outcome.citation_assessment == "partially_valid"

    async def test_partial_support_grounded_only_by_the_negating_quote_survives(
        self, pipeline, service_session
    ):
        """One passage establishes part of a compound claim and negates the rest;
        the schema allows it one relation, and the model quotes the negated part.
        The verbatim quote grounds the verdict: partially_supported must survive,
        anchored to the contradicting evidence — not be downgraded into
        not_enough_evidence (the val-partial-02 failure, observed live)."""
        run_response, web_label, _, extract, verifier = pipeline
        await extract(
            claim(
                "Caffeine was studied in older adults and improved their recall.",
                labels=[web_label],
            )
        )
        llm = FakeVerifierLLM().on(
            "improved their recall",
            lambda p: judge(
                p,
                contradict_containing=ABSTRACT_QUOTE,
                verdict=VerificationVerdict.PARTIALLY_SUPPORTED,
            ),
        )

        result = await verifier(llm).verify(run_response.research_id)

        [outcome] = result.results
        assert outcome.verdict is VerificationVerdict.PARTIALLY_SUPPORTED
        assert outcome.downgraded is False
        [stored] = service_session.execute(select(VerificationResult)).scalars().all()
        anchor = next(
            e for e in service_session.execute(select(Evidence)).scalars().all()
            if e.id == stored.supporting_evidence_id
        )
        assert anchor.relation is EvidenceRelation.CONTRADICTS
        assert anchor.quoted_text == ABSTRACT_QUOTE

    async def test_insufficient_evidence_is_stored_without_any_downgrade(
        self, pipeline, service_session
    ):
        """All passages judged neutral: not_enough_evidence is the verifier's own
        verdict, stored verbatim with downgraded=False — visible uncertainty, not a
        converted one — and every evidence row stays quoteless and neutral."""
        run_response, web_label, _, extract, verifier = pipeline
        await extract(claim(SUPPORTED_CLAIM, labels=[web_label]))
        llm = FakeVerifierLLM().on(
            "caffeine intake",
            lambda p: judge(p, verdict=VerificationVerdict.NOT_ENOUGH_EVIDENCE),
        )

        result = await verifier(llm).verify(run_response.research_id)

        [outcome] = result.results
        assert outcome.verdict is VerificationVerdict.NOT_ENOUGH_EVIDENCE
        assert outcome.downgraded is False
        assert outcome.citation_assessment == "invalid"  # cited source supported nothing
        evidence = service_session.execute(select(Evidence)).scalars().all()
        assert evidence and all(e.relation is EvidenceRelation.NEUTRAL for e in evidence)
        assert all(e.quoted_text is None for e in evidence)

    async def test_an_unsupported_verdict_needs_no_quote_and_is_not_downgraded(
        self, pipeline
    ):
        """unsupported asserts the absence of support, not evidence of falsity: it
        is not evidence-bearing, so it stands without a quoted passage."""
        run_response, web_label, _, extract, verifier = pipeline
        await extract(claim(SUPPORTED_CLAIM, labels=[web_label]))
        llm = FakeVerifierLLM().on(
            "caffeine intake",
            lambda p: judge(p, verdict=VerificationVerdict.UNSUPPORTED),
        )

        result = await verifier(llm).verify(run_response.research_id)

        [outcome] = result.results
        assert outcome.verdict is VerificationVerdict.UNSUPPORTED
        assert outcome.downgraded is False


class TestPromptV2:
    """Pins of the verification-v2 prompt, in the style of the metrics terminology
    guards: each measured v1 failure mode must stay named in the rules."""

    def test_the_version_is_v2_and_the_external_judge_follows(self):
        from app.services.benchmark.external_judge import EXTERNAL_JUDGE_VERSION
        from app.services.verification.prompts import VERIFICATION_PROMPT_VERSION

        assert VERIFICATION_PROMPT_VERSION == "verification-v2"
        assert EXTERNAL_JUDGE_VERSION == "external-verification-v2"

    def test_the_rules_name_each_measured_failure_mode(self):
        from app.services.verification.prompts import VERIFICATION_SYSTEM

        # stage-20: compound claims with one negated conjunct fell to contradicted
        assert "Compound claims" in VERIFICATION_SYSTEM
        assert "partially_supported" in VERIFICATION_SYSTEM
        # stage-21: hedged agreement judged as contradiction
        assert "hedging" in VERIFICATION_SYSTEM
        # stage-21: an in-range value judged as contradiction
        assert "in-range value" in VERIFICATION_SYSTEM

    def test_the_rules_cover_the_spec_checklist(self):
        from app.services.verification.prompts import VERIFICATION_SYSTEM

        assert "numbers, dates, units, and named entities" in VERIFICATION_SYSTEM
        assert "CAUSES" in VERIFICATION_SYSTEM  # causal vs correlational wording
        assert "Absence is not falsity" in VERIFICATION_SYSTEM
        assert "verbatim" in VERIFICATION_SYSTEM
        # conflicting evidence is distinguished from insufficient evidence
        assert "Conflicting evidence is not" in VERIFICATION_SYSTEM
        assert "never call it not_enough_evidence" in VERIFICATION_SYSTEM
        for verdict in VerificationVerdict:
            assert verdict.value in VERIFICATION_SYSTEM

    def test_the_prompt_format_is_unchanged_from_v1(self):
        """The enforcement and the scripted tests parse this exact shape; v2 changed
        the rules, not the envelope."""
        from app.services.verification.prompts import build_verification_prompt

        prompt = build_verification_prompt("A claim.", [("P1", "A title", "Some text.")])
        assert prompt.startswith("Claim to verify:\nA claim.\n\nEvidence passages:")
        assert "[P1] (from: A title)\nSome text." in prompt
        assert prompt.endswith("Verify now.")


class TestResumabilityAndGuards:
    async def test_a_twice_failing_claim_is_skipped_then_resumable(self, pipeline):
        run_response, web_label, _, extract, verifier = pipeline
        await extract(
            claim(SUPPORTED_CLAIM, labels=[web_label]),
            claim("Reaction times improved modestly in older adults.",
                  claim_type=ClaimType.FACTUAL, importance=ClaimImportance.MEDIUM,
                  labels=[]),
        )
        llm = (
            FakeVerifierLLM()
            .on("caffeine intake", lambda p: judge(p, support_containing=WEB_QUOTE))
            .on("Reaction times", LLMResponseInvalid("will not parse"))
        )
        first = await verifier(llm).verify(run_response.research_id)
        assert first.claims_verified == 1 and first.claims_skipped == 1
        assert any("skipped" in warning for warning in first.warnings)

        retry_llm = FakeVerifierLLM().on(
            "Reaction times", lambda p: judge(p, support_containing=ABSTRACT_QUOTE)
        )
        second = await verifier(retry_llm).verify(run_response.research_id)
        assert second.claims_verified == 1 and second.claims_skipped == 0

    async def test_verifying_before_extraction_says_so(self, pipeline):
        run_response, _, _, _, verifier = pipeline
        with pytest.raises(NothingToVerify, match="extraction first"):
            await verifier(FakeVerifierLLM()).verify(run_response.research_id)

    async def test_verifying_twice_says_everything_is_done(self, pipeline):
        run_response, web_label, _, extract, verifier = pipeline
        await extract(claim(SUPPORTED_CLAIM, labels=[web_label]))
        llm = FakeVerifierLLM().on(
            "caffeine intake", lambda p: judge(p, support_containing=WEB_QUOTE)
        )
        await verifier(llm).verify(run_response.research_id)
        with pytest.raises(NothingToVerify, match="already verified"):
            await verifier(llm).verify(run_response.research_id)

    async def test_hybrid_runs_verify_against_a_rebuilt_collection(
        self, service_session, tmp_path
    ):
        """Hybrid never built a vector collection; verification rebuilds one on
        demand from the persisted chunk rows, under the same content-derived ids."""
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
        hybrid = await orchestrator.run(
            ResearchStartRequest(topic=TOPIC, depth="quick", mode="hybrid")
        )
        web_label = next(s.label for s in hybrid.sources if s.source_type == "web")
        await ClaimExtractor(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            llm=FakeStructuredLLM(extraction(claim(SUPPORTED_CLAIM, labels=[web_label]))),
        ).extract(hybrid.research_id)

        verifier = ClaimVerifier(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            store_factory=store_factory,
            llm=FakeVerifierLLM().on(
                "caffeine intake", lambda p: judge(p, support_containing=WEB_QUOTE)
            ),
        )
        result = await verifier.verify(hybrid.research_id)

        assert result.claims_verified == 1
        assert result.results[0].evidence_count >= 1  # rebuilt collection had chunks
        assert result.results[0].citations == {web_label: "valid"}

    async def test_model_only_runs_verify_to_not_enough_evidence(
        self, service_session, tmp_path
    ):
        """MODEL_ONLY retrieved nothing: every claim takes the no-evidence path with
        no LLM calls — the baseline's emptiness is measured, not errored."""
        test_settings = settings()
        orchestrator = ResearchOrchestrator(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            web_search=FakeWebSearch(web_result(web_candidate())),
            academic_search=FakeAcademicSearch(academic_result(academic_candidate())),
            llm=FakeLLM("# T\n\n## Claims\n\nAn ungrounded assertion of fact.\n"),
        )
        run = await orchestrator.run(
            ResearchStartRequest(topic=TOPIC, depth="quick", mode="model_only")
        )
        await ClaimExtractor(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            llm=FakeStructuredLLM(
                extraction(claim("An ungrounded assertion of fact is made.", labels=[]))
            ),
        ).extract(run.research_id)

        verifier_llm = FakeVerifierLLM()  # must never be called
        result = await ClaimVerifier(
            settings=test_settings,
            research_service=ResearchService(settings=test_settings),
            llm=verifier_llm,
        ).verify(run.research_id)

        assert result.claims_verified == 1
        assert result.verdicts == {"not_enough_evidence": 1}
        assert verifier_llm.calls_made == 0
        assert result.results[0].evidence_count == 0
