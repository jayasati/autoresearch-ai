"""
Schema validation.

Placeholder values throughout are deliberately non-plausible -- `example.invalid`
is a reserved TLD that can never resolve, and text reads like `<claim under test>`.
Nothing here could be mistaken for a real source or a real research result if it
leaked into a screenshot or a log.
"""

import uuid
from datetime import UTC

import pytest
from pydantic import ValidationError

from app.core.constants import (
    CitationStatus,
    ConflictType,
    EvidenceDepth,
    EvidenceRelation,
    MetricKey,
    ResearchMode,
    SourceType,
    VerificationVerdict,
)
from app.schemas import (
    CitationCreate,
    ClaimCreate,
    ConflictCreate,
    DocumentChunkCreate,
    EvaluationResultCreate,
    EvidenceCreate,
    ResearchRequest,
    ResearchResponse,
    RunConfigurationCreate,
    SourceCreate,
    VerificationResultCreate,
)

ID = uuid.uuid4


def retrieval_config(**overrides) -> dict:
    base = {
        "mode": ResearchMode.SEARCH_GROUNDED,
        "model": "gpt-4o-mini",
        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        "chunk_size": 900,
        "chunk_overlap": 150,
        "top_k": 6,
        "max_subquestions": 6,
        "max_sources": 25,
    }
    return {**base, **overrides}


class TestResearchRequest:
    def test_accepts_a_substantive_topic(self):
        request = ResearchRequest(topic="Does retrieval reduce hallucination?")
        assert request.mode is ResearchMode.SEARCH_GROUNDED

    def test_collapses_internal_whitespace(self):
        request = ResearchRequest(topic="  Does   retrieval    reduce errors?  ")
        assert request.topic == "Does retrieval reduce errors?"

    def test_rejects_a_topic_that_is_too_short(self):
        with pytest.raises(ValidationError, match="at least 12 characters|too_short"):
            ResearchRequest(topic="RAG")

    def test_rejects_a_topic_that_is_only_whitespace_padded(self):
        """Length must be judged after collapsing, not before."""
        with pytest.raises(ValidationError, match="at least 12 characters"):
            ResearchRequest(topic="RAG" + " " * 40)

    def test_rejects_a_topic_with_no_words(self):
        """Long enough but meaningless: it would still cost a model call."""
        with pytest.raises(ValidationError, match="must contain words"):
            ResearchRequest(topic="1234567890123456")

    def test_rejects_an_overlong_topic(self):
        with pytest.raises(ValidationError):
            ResearchRequest(topic="a" * 501)

    def test_rejects_an_unknown_mode(self):
        with pytest.raises(ValidationError):
            ResearchRequest(topic="Does retrieval help?", mode="deep_thought")

    def test_rejects_a_misspelled_field_rather_than_ignoring_it(self):
        """A silently ignored typo would run the wrong configuration."""
        with pytest.raises(ValidationError, match="extra_forbidden|topik"):
            ResearchRequest(topik="Does retrieval reduce hallucination?")

    def test_rejects_budget_overrides_outside_their_bounds(self):
        with pytest.raises(ValidationError):
            ResearchRequest(topic="Does retrieval help at all?", max_sources=5000)
        with pytest.raises(ValidationError):
            ResearchRequest(topic="Does retrieval help at all?", max_subquestions=0)

    def test_normalized_topic_is_the_benchmark_grouping_key(self):
        a = ResearchRequest(topic="RAG   and Hallucination")
        b = ResearchRequest(topic="rag and hallucination")
        assert a.normalized_topic == b.normalized_topic


class TestRunConfiguration:
    def test_accepts_a_retrieval_configuration(self):
        config = RunConfigurationCreate(**retrieval_config())
        assert config.top_k == 6

    def test_model_only_must_not_carry_retrieval_settings(self):
        """A chunk size on a no-retrieval run implies retrieval that never happened."""
        with pytest.raises(ValidationError, match="performs no retrieval"):
            RunConfigurationCreate(
                mode=ResearchMode.MODEL_ONLY,
                model="gpt-4o-mini",
                chunk_size=900,
                max_subquestions=6,
                max_sources=25,
            )

    def test_model_only_is_valid_without_them(self):
        config = RunConfigurationCreate(
            mode=ResearchMode.MODEL_ONLY,
            model="gpt-4o-mini",
            max_subquestions=6,
            max_sources=25,
        )
        assert config.top_k is None

    def test_retrieving_modes_require_retrieval_settings(self):
        with pytest.raises(ValidationError, match="requires"):
            RunConfigurationCreate(
                mode=ResearchMode.HYBRID,
                model="gpt-4o-mini",
                max_subquestions=6,
                max_sources=25,
            )

    def test_overlap_must_be_smaller_than_the_chunk(self):
        with pytest.raises(ValidationError, match="smaller than chunk_size"):
            RunConfigurationCreate(**retrieval_config(chunk_size=500, chunk_overlap=500))

    def test_identical_configurations_fingerprint_identically(self):
        assert (
            RunConfigurationCreate(**retrieval_config()).fingerprint
            == RunConfigurationCreate(**retrieval_config()).fingerprint
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("model", "gpt-4o"),
            ("temperature", 0.7),
            ("top_k", 10),
            ("chunk_size", 1200),
            ("max_sources", 30),
            ("synthesizer_prompt_version", "v2"),
        ],
    )
    def test_any_meaningful_change_changes_the_fingerprint(self, field, value):
        """A changed prompt or parameter must make runs non-comparable."""
        baseline = RunConfigurationCreate(**retrieval_config()).fingerprint
        changed = RunConfigurationCreate(**retrieval_config(**{field: value})).fingerprint
        assert changed != baseline

    def test_mode_changes_the_fingerprint(self):
        grounded = RunConfigurationCreate(**retrieval_config()).fingerprint
        model_only = RunConfigurationCreate(
            mode=ResearchMode.MODEL_ONLY,
            model="gpt-4o-mini",
            max_subquestions=6,
            max_sources=25,
        ).fingerprint
        assert grounded != model_only


class TestSourceCreate:
    def test_a_web_source_requires_a_url(self):
        with pytest.raises(ValidationError, match="web sources require a url"):
            SourceCreate(source_type=SourceType.WEB, title="<untitled>")

    def test_an_academic_source_may_be_identified_by_doi_alone(self):
        source = SourceCreate(source_type=SourceType.ACADEMIC, doi="10.0000/invalid.test")
        assert source.fingerprint is not None

    def test_an_academic_source_needs_some_identifier(self):
        with pytest.raises(ValidationError, match="require at least one of"):
            SourceCreate(source_type=SourceType.ACADEMIC, title="<untitled>")

    def test_a_model_asserted_source_has_no_identifier(self):
        """The representable case for an uncited claim, rather than dropping it."""
        source = SourceCreate(source_type=SourceType.MODEL)
        assert source.fingerprint is None

    def test_a_model_source_must_not_carry_a_url(self):
        with pytest.raises(ValidationError, match="no external identifier"):
            SourceCreate(source_type=SourceType.MODEL, url="https://example.invalid/a")

    def test_doi_wins_over_url_for_identity(self):
        """The same paper at two URLs must be one source."""
        a = SourceCreate(
            source_type=SourceType.ACADEMIC,
            doi="10.0000/invalid.test",
            url="https://example.invalid/a",
        )
        b = SourceCreate(
            source_type=SourceType.ACADEMIC,
            doi="10.0000/invalid.test",
            url="https://other.invalid/b",
        )
        assert a.fingerprint == b.fingerprint

    def test_url_fingerprints_ignore_case_and_a_trailing_slash(self):
        a = SourceCreate(source_type=SourceType.WEB, url="https://Example.invalid/Page/")
        b = SourceCreate(source_type=SourceType.WEB, url="https://example.invalid/page")
        assert a.fingerprint == b.fingerprint

    def test_rejects_an_implausible_publication_year(self):
        with pytest.raises(ValidationError):
            SourceCreate(
                source_type=SourceType.ACADEMIC, doi="10.0000/invalid.test", publication_year=3000
            )


class TestDocumentChunkCreate:
    def test_accepts_a_span_matching_its_text(self):
        chunk = DocumentChunkCreate(
            document_id=ID(), position=0, text="abcde", start_char=10, end_char=15
        )
        assert chunk.content_hash

    def test_rejects_an_inverted_span(self):
        with pytest.raises(ValidationError):
            DocumentChunkCreate(
                document_id=ID(), position=0, text="abcde", start_char=20, end_char=15
            )

    def test_rejects_a_span_that_does_not_match_the_text_length(self):
        """The offsets would point at the wrong characters of the document."""
        with pytest.raises(ValidationError, match="span is 3 characters but text is 5"):
            DocumentChunkCreate(
                document_id=ID(), position=0, text="abcde", start_char=0, end_char=3
            )

    def test_rejects_a_negative_position(self):
        with pytest.raises(ValidationError):
            DocumentChunkCreate(
                document_id=ID(), position=-1, text="abc", start_char=0, end_char=3
            )

    def test_identical_text_hashes_identically(self):
        a = DocumentChunkCreate(document_id=ID(), position=0, text="xyz", start_char=0, end_char=3)
        b = DocumentChunkCreate(document_id=ID(), position=7, text="xyz", start_char=9, end_char=12)
        assert a.content_hash == b.content_hash


class TestClaimCreate:
    def test_offsets_must_be_given_together(self):
        with pytest.raises(ValidationError, match="must be given together"):
            ClaimCreate(report_id=ID(), position=0, text="<claim under test>", start_char=5)

    def test_a_claim_without_offsets_is_allowed(self):
        claim = ClaimCreate(report_id=ID(), position=0, text="<claim under test>")
        assert claim.start_char is None

    def test_rejects_an_inverted_span(self):
        with pytest.raises(ValidationError, match="greater than start_char"):
            ClaimCreate(
                report_id=ID(), position=0, text="<claim>", start_char=20, end_char=10
            )


class TestEvidenceCreate:
    def test_a_neutral_passage_needs_no_quote(self):
        evidence = EvidenceCreate(claim_id=ID(), document_chunk_id=ID())
        assert evidence.relation is EvidenceRelation.NEUTRAL

    def test_a_supporting_passage_must_quote_the_words_it_relies_on(self):
        with pytest.raises(ValidationError, match="requires quoted_text"):
            EvidenceCreate(
                claim_id=ID(), document_chunk_id=ID(), relation=EvidenceRelation.SUPPORTS
            )

    def test_a_contradicting_passage_must_quote_too(self):
        with pytest.raises(ValidationError, match="requires quoted_text"):
            EvidenceCreate(
                claim_id=ID(), document_chunk_id=ID(), relation=EvidenceRelation.CONTRADICTS
            )

    def test_quote_offsets_must_match_the_quote_length(self):
        with pytest.raises(ValidationError, match="quote span is"):
            EvidenceCreate(
                claim_id=ID(),
                document_chunk_id=ID(),
                relation=EvidenceRelation.SUPPORTS,
                quoted_text="abcde",
                quote_start_char=0,
                quote_end_char=2,
            )

    def test_similarity_must_be_a_fraction(self):
        with pytest.raises(ValidationError):
            EvidenceCreate(claim_id=ID(), document_chunk_id=ID(), similarity=1.4)


class TestCitationCreate:
    """The four-way distinction the project's main result depends on."""

    def test_a_valid_citation_names_a_source_and_a_passage(self):
        citation = CitationCreate(
            claim_id=ID(),
            source_id=ID(),
            marker="S1",
            status=CitationStatus.VALID,
            supporting_chunk_id=ID(),
        )
        assert citation.status is CitationStatus.VALID

    def test_a_valid_citation_without_a_passage_is_rejected(self):
        with pytest.raises(ValidationError, match="must name the passage"):
            CitationCreate(
                claim_id=ID(), source_id=ID(), marker="S1", status=CitationStatus.VALID
            )

    def test_a_fabricated_citation_must_not_reference_a_source(self):
        with pytest.raises(ValidationError, match="source_id must be null"):
            CitationCreate(
                claim_id=ID(),
                source_id=ID(),
                marker="S9",
                status=CitationStatus.FABRICATED,
                failure_reason="no such paper exists",
            )

    def test_a_fabricated_citation_is_valid_with_no_source(self):
        citation = CitationCreate(
            claim_id=ID(),
            marker="S9",
            status=CitationStatus.FABRICATED,
            raw_reference="<reference the model invented>",
            failure_reason="no such paper exists",
        )
        assert citation.source_id is None

    def test_a_misattributed_citation_does_reference_a_real_source(self):
        """The distinction from fabricated: the source exists, the support does not."""
        citation = CitationCreate(
            claim_id=ID(),
            source_id=ID(),
            marker="S3",
            status=CitationStatus.MISATTRIBUTED,
            failure_reason="source does not support this claim",
        )
        assert citation.source_id is not None

    def test_a_misattributed_citation_cannot_omit_its_source(self):
        with pytest.raises(ValidationError, match="requires a source_id"):
            CitationCreate(
                claim_id=ID(),
                marker="S3",
                status=CitationStatus.MISATTRIBUTED,
                failure_reason="does not support the claim",
            )

    @pytest.mark.parametrize(
        "status",
        [CitationStatus.BROKEN, CitationStatus.MISATTRIBUTED, CitationStatus.FABRICATED],
    )
    def test_every_failure_must_be_explained(self, status):
        kwargs = {"claim_id": ID(), "marker": "S1", "status": status}
        if status is not CitationStatus.FABRICATED:
            kwargs["source_id"] = ID()
        with pytest.raises(ValidationError, match="requires a failure_reason"):
            CitationCreate(**kwargs)

    def test_a_valid_citation_has_no_failure_reason(self):
        with pytest.raises(ValidationError, match="has no failure_reason"):
            CitationCreate(
                claim_id=ID(),
                source_id=ID(),
                marker="S1",
                status=CitationStatus.VALID,
                supporting_chunk_id=ID(),
                failure_reason="contradictory",
            )

    def test_http_status_belongs_only_to_a_broken_citation(self):
        with pytest.raises(ValidationError, match="only to a broken citation"):
            CitationCreate(
                claim_id=ID(),
                source_id=ID(),
                marker="S1",
                status=CitationStatus.MISATTRIBUTED,
                failure_reason="does not support the claim",
                http_status=404,
            )


class TestVerificationResultCreate:
    """The documented mitigation for a hallucinating verifier, as a type rule."""

    def test_a_supported_verdict_with_evidence_is_kept(self):
        result = VerificationResultCreate(
            claim_id=ID(), verdict=VerificationVerdict.SUPPORTED, supporting_evidence_id=ID()
        )
        assert result.verdict is VerificationVerdict.SUPPORTED
        assert result.downgraded is False

    @pytest.mark.parametrize(
        "verdict",
        [
            VerificationVerdict.SUPPORTED,
            VerificationVerdict.PARTIALLY_SUPPORTED,
            VerificationVerdict.CONTRADICTED,
        ],
    )
    def test_an_unquoted_verdict_is_downgraded_not_rejected(self, verdict):
        """Downgraded, because the verifier's overclaim is itself data worth keeping."""
        result = VerificationResultCreate(claim_id=ID(), verdict=verdict)
        assert result.verdict is VerificationVerdict.NOT_ENOUGH_EVIDENCE
        assert result.downgraded is True

    def test_unsupported_needs_no_evidence(self):
        """Nothing was found, so there is nothing to quote."""
        result = VerificationResultCreate(
            claim_id=ID(), verdict=VerificationVerdict.UNSUPPORTED
        )
        assert result.verdict is VerificationVerdict.UNSUPPORTED
        assert result.downgraded is False

    def test_confidence_must_be_a_fraction(self):
        with pytest.raises(ValidationError):
            VerificationResultCreate(
                claim_id=ID(),
                verdict=VerificationVerdict.UNSUPPORTED,
                confidence=1.5,
            )

    @pytest.mark.parametrize(
        "verdict,expected",
        [
            (VerificationVerdict.UNSUPPORTED, True),
            (VerificationVerdict.NOT_ENOUGH_EVIDENCE, False),
        ],
    )
    def test_hallucination_classification(self, verdict, expected):
        result = VerificationResultCreate(claim_id=ID(), verdict=verdict)
        assert result.is_hallucination is expected

    def test_a_downgraded_verdict_does_not_count_as_a_hallucination(self):
        """It means we could not tell, which is not the same as being wrong."""
        result = VerificationResultCreate(
            claim_id=ID(), verdict=VerificationVerdict.SUPPORTED
        )
        assert result.downgraded is True
        assert result.is_hallucination is False


class TestConflictCreate:
    def test_requires_two_different_passages(self):
        same = ID()
        with pytest.raises(ValidationError, match="cannot conflict with itself"):
            ConflictCreate(
                research_run_id=ID(),
                evidence_a_id=same,
                evidence_b_id=same,
                conflict_type=ConflictType.NUMERIC_DISAGREEMENT,
                description="<description under test>",
            )

    def test_accepts_two_distinct_passages(self):
        conflict = ConflictCreate(
            research_run_id=ID(),
            evidence_a_id=ID(),
            evidence_b_id=ID(),
            conflict_type=ConflictType.TEMPORAL_STALENESS,
            description="<description under test>",
        )
        assert conflict.conflict_type is ConflictType.TEMPORAL_STALENESS


class TestEvaluationResultCreate:
    def test_accepts_a_ratio_matching_its_terms(self):
        result = EvaluationResultCreate(
            research_run_id=ID(),
            metric_key=MetricKey.CLAIM_SUPPORT_RATE,
            value=0.8,
            numerator=4,
            denominator=5,
            metrics_version="v1",
        )
        assert result.value == 0.8

    def test_rejects_a_ratio_above_one(self):
        with pytest.raises(ValidationError, match=r"must be in \[0, 1\]"):
            EvaluationResultCreate(
                research_run_id=ID(),
                metric_key=MetricKey.CLAIM_SUPPORT_RATE,
                value=1.2,
                metrics_version="v1",
            )

    def test_rejects_a_value_its_terms_do_not_support(self):
        """The number and its justification must not drift apart."""
        with pytest.raises(ValidationError, match="does not match 4/5"):
            EvaluationResultCreate(
                research_run_id=ID(),
                metric_key=MetricKey.CLAIM_SUPPORT_RATE,
                value=0.95,
                numerator=4,
                denominator=5,
                metrics_version="v1",
            )

    def test_rejects_a_numerator_larger_than_its_denominator(self):
        with pytest.raises(ValidationError, match="cannot exceed denominator"):
            EvaluationResultCreate(
                research_run_id=ID(),
                metric_key=MetricKey.CITATION_PRECISION,
                value=1.0,
                numerator=7,
                denominator=5,
                metrics_version="v1",
            )

    def test_rejects_a_zero_denominator(self):
        """A run with no claims has an undefined support rate, not a zero one."""
        with pytest.raises(ValidationError):
            EvaluationResultCreate(
                research_run_id=ID(),
                metric_key=MetricKey.CLAIM_SUPPORT_RATE,
                value=0.0,
                numerator=0,
                denominator=0,
                metrics_version="v1",
            )

    def test_terms_must_be_given_together(self):
        with pytest.raises(ValidationError, match="must be given together"):
            EvaluationResultCreate(
                research_run_id=ID(),
                metric_key=MetricKey.COVERAGE,
                value=0.5,
                numerator=1,
                metrics_version="v1",
            )

    def test_a_non_ratio_metric_may_exceed_one(self):
        result = EvaluationResultCreate(
            research_run_id=ID(),
            metric_key=MetricKey.TOTAL_TOKENS,
            value=18450.0,
            unit="tokens",
            metrics_version="v1",
        )
        assert result.value == 18450.0

    def test_cost_cannot_be_negative(self):
        with pytest.raises(ValidationError, match="cannot be negative"):
            EvaluationResultCreate(
                research_run_id=ID(),
                metric_key=MetricKey.COST_USD,
                value=-1.0,
                metrics_version="v1",
            )

    def test_an_unknown_metric_key_is_rejected(self):
        """A typo would split one metric into two columns of the comparison table."""
        with pytest.raises(ValidationError):
            EvaluationResultCreate(
                research_run_id=ID(),
                metric_key="clam_support_rate",
                value=0.5,
                metrics_version="v1",
            )


class TestResearchResponse:
    def test_claims_cannot_exist_without_a_report(self):
        from app.schemas import ResearchRunRead  # noqa: F401  (documents the shape)

        with pytest.raises(ValidationError, match="must be 0 when there is no report"):
            ResearchResponse.model_construct and ResearchResponse(
                run=_minimal_run_read(), report_id=None, claim_count=3
            )

    def test_a_run_with_no_report_reports_no_claims(self):
        response = ResearchResponse(run=_minimal_run_read())
        assert response.claim_count == 0
        assert response.report_id is None


def _minimal_run_read():
    """A ResearchRunRead built from literals, for validating the wrapper only."""
    from datetime import datetime

    from app.core.constants import RunStatus
    from app.schemas import ResearchRunRead, RunConfigurationRead

    now = datetime(2026, 1, 1, tzinfo=UTC)
    config = RunConfigurationRead(
        id=ID(),
        fingerprint="0" * 64,
        mode=ResearchMode.MODEL_ONLY,
        model="gpt-4o-mini",
        temperature=0.0,
        embedding_model=None,
        chunk_size=None,
        chunk_overlap=None,
        top_k=None,
        max_subquestions=6,
        max_sources=25,
        planner_prompt_version=None,
        synthesizer_prompt_version=None,
        claim_prompt_version=None,
        verifier_prompt_version=None,
        created_at=now,
    )
    return ResearchRunRead(
        id=ID(),
        created_at=now,
        updated_at=now,
        topic="<topic under test>",
        normalized_topic="<topic under test>",
        status=RunStatus.PENDING,
        configuration_id=config.id,
        benchmark_run_id=None,
        started_at=None,
        completed_at=None,
        error_code=None,
        error_message=None,
        configuration=config,
    )


class TestPlaceholderDiscipline:
    """The test data itself must never look like real research output."""

    def test_source_urls_use_a_reserved_non_resolvable_domain(self):
        source = SourceCreate(source_type=SourceType.WEB, url="https://example.invalid/page")
        assert source.url.endswith(".invalid/page")

    def test_evidence_depth_vocabulary_is_closed(self):
        assert {d.value for d in EvidenceDepth} == {"snippet", "abstract", "full_text"}
