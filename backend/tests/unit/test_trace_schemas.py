"""
The three traceability chains as response models.

These validators exist so a *broken* chain cannot be serialised and handed to a
reader as though it were intact. Each test below is one way a trace could lie.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.constants import (
    CitationStatus,
    EvidenceDepth,
    EvidenceRelation,
    MetricKey,
    ResearchMode,
    VerificationVerdict,
)
from app.schemas import (
    BenchmarkComparison,
    ChunkPointer,
    CitationTrace,
    ConfigurationMetrics,
    EvaluationResultRead,
    EvaluationTrace,
    EvidenceLink,
    EvidenceTrace,
    RunConfigurationRead,
    SourcePointer,
    VerificationTrace,
)

ID = uuid.uuid4
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def chunk(text: str = "<passage under test>") -> ChunkPointer:
    return ChunkPointer(
        chunk_id=ID(), document_id=ID(), start_char=1840, end_char=1840 + len(text), text=text
    )


def source_pointer() -> SourcePointer:
    return SourcePointer(
        source_id=ID(),
        title="<source title under test>",
        url="https://example.invalid/paper",
        doi=None,
        evidence_depth=EvidenceDepth.FULL_TEXT,
    )


def configuration(mode: ResearchMode = ResearchMode.SEARCH_GROUNDED) -> RunConfigurationRead:
    return RunConfigurationRead(
        id=ID(),
        fingerprint="a" * 64,
        mode=mode,
        model="<model under test>",
        temperature=0.0,
        embedding_model="<embedding model>" if mode is not ResearchMode.MODEL_ONLY else None,
        chunk_size=900 if mode is not ResearchMode.MODEL_ONLY else None,
        chunk_overlap=150 if mode is not ResearchMode.MODEL_ONLY else None,
        top_k=6 if mode is not ResearchMode.MODEL_ONLY else None,
        max_subquestions=6,
        max_sources=25,
        planner_prompt_version=None,
        synthesizer_prompt_version=None,
        claim_prompt_version=None,
        verifier_prompt_version=None,
        created_at=NOW,
    )


def metric(key: MetricKey, value: float, version: str = "v1") -> EvaluationResultRead:
    return EvaluationResultRead(
        id=ID(),
        research_run_id=ID(),
        metric_key=key,
        value=value,
        numerator=None,
        denominator=None,
        unit=None,
        metrics_version=version,
        notes=None,
        computed_at=NOW,
    )


class TestChunkPointer:
    def test_the_chain_ends_at_a_character_span(self):
        pointer = chunk("abc")
        assert pointer.span == "[1840:1843]"


class TestCitationTrace:
    def test_a_valid_citation_resolves_to_a_source_and_a_passage(self):
        trace = CitationTrace(
            citation_id=ID(),
            marker="S1",
            status=CitationStatus.VALID,
            source=source_pointer(),
            supporting_chunk=chunk(),
        )
        assert trace.source is not None

    def test_a_fabricated_citation_cannot_resolve_to_a_source(self):
        """Serialising one would assert that a nonexistent paper exists."""
        with pytest.raises(ValidationError, match="cannot resolve to a source"):
            CitationTrace(
                citation_id=ID(),
                marker="S9",
                status=CitationStatus.FABRICATED,
                source=source_pointer(),
            )

    def test_a_fabricated_citation_with_no_source_is_representable(self):
        trace = CitationTrace(
            citation_id=ID(),
            marker="S9",
            status=CitationStatus.FABRICATED,
            failure_reason="no such paper exists",
        )
        assert trace.source is None


class TestEvidenceTrace:
    """Chain 1: Research -> Report -> Claim -> Citation -> Source -> chunk."""

    def _trace(self, citations) -> EvidenceTrace:
        return EvidenceTrace(
            research_run_id=ID(),
            topic="<topic under test>",
            mode=ResearchMode.SEARCH_GROUNDED,
            report_id=ID(),
            claim_id=ID(),
            claim_text="<claim under test>",
            claim_position=0,
            citations=citations,
            evidence_chunks=[chunk()],
        )

    def test_a_claim_with_a_resolved_citation_is_traceable(self):
        trace = self._trace(
            [
                CitationTrace(
                    citation_id=ID(),
                    marker="S1",
                    status=CitationStatus.VALID,
                    source=source_pointer(),
                    supporting_chunk=chunk(),
                )
            ]
        )
        assert trace.is_fully_traceable is True

    def test_a_claim_whose_only_citation_is_fabricated_is_not_traceable(self):
        """It looks cited. That is exactly why this has to be computed, not assumed."""
        trace = self._trace(
            [
                CitationTrace(
                    citation_id=ID(),
                    marker="S9",
                    status=CitationStatus.FABRICATED,
                    failure_reason="no such paper exists",
                )
            ]
        )
        assert trace.is_fully_traceable is False

    def test_a_misattributed_citation_does_not_make_a_claim_traceable(self):
        trace = self._trace(
            [
                CitationTrace(
                    citation_id=ID(),
                    marker="S3",
                    status=CitationStatus.MISATTRIBUTED,
                    source=source_pointer(),
                    failure_reason="source does not support this claim",
                )
            ]
        )
        assert trace.is_fully_traceable is False

    def test_an_uncited_claim_is_not_traceable(self):
        assert self._trace([]).is_fully_traceable is False

    def test_one_valid_citation_among_failures_is_enough(self):
        trace = self._trace(
            [
                CitationTrace(
                    citation_id=ID(),
                    marker="S9",
                    status=CitationStatus.FABRICATED,
                    failure_reason="no such paper exists",
                ),
                CitationTrace(
                    citation_id=ID(),
                    marker="S1",
                    status=CitationStatus.VALID,
                    source=source_pointer(),
                    supporting_chunk=chunk(),
                ),
            ]
        )
        assert trace.is_fully_traceable is True


class TestVerificationTrace:
    """Chain 2: Claim -> Evidence -> VerificationResult."""

    def test_the_decisive_passage_must_appear_in_the_evidence(self):
        """Otherwise the trace points at something the response does not contain."""
        link = EvidenceLink(
            evidence_id=ID(),
            relation=EvidenceRelation.SUPPORTS,
            quoted_text="<quote under test>",
            chunk=chunk(),
        )
        with pytest.raises(ValidationError, match="must refer to one of the listed"):
            VerificationTrace(
                claim_id=ID(),
                claim_text="<claim under test>",
                verdict=VerificationVerdict.SUPPORTED,
                evidence=[link],
                decisive_evidence_id=ID(),  # not the link above
            )

    def test_a_consistent_trace_is_accepted(self):
        link = EvidenceLink(
            evidence_id=ID(),
            relation=EvidenceRelation.SUPPORTS,
            quoted_text="<quote under test>",
            chunk=chunk(),
        )
        trace = VerificationTrace(
            claim_id=ID(),
            claim_text="<claim under test>",
            verdict=VerificationVerdict.SUPPORTED,
            evidence=[link],
            decisive_evidence_id=link.evidence_id,
        )
        assert trace.decisive_evidence_id == link.evidence_id

    def test_a_verdict_with_no_decisive_passage_is_allowed(self):
        """`unsupported` found nothing, so there is nothing to point at."""
        trace = VerificationTrace(
            claim_id=ID(),
            claim_text="<claim under test>",
            verdict=VerificationVerdict.UNSUPPORTED,
        )
        assert trace.decisive_evidence_id is None

    def test_contradicting_evidence_is_listed_not_hidden(self):
        """A reader needs the evidence that was weighed and rejected."""
        supports = EvidenceLink(
            evidence_id=ID(),
            relation=EvidenceRelation.SUPPORTS,
            quoted_text="<supporting quote>",
            chunk=chunk(),
        )
        contradicts = EvidenceLink(
            evidence_id=ID(),
            relation=EvidenceRelation.CONTRADICTS,
            quoted_text="<contradicting quote>",
            chunk=chunk(),
        )
        trace = VerificationTrace(
            claim_id=ID(),
            claim_text="<claim under test>",
            verdict=VerificationVerdict.PARTIALLY_SUPPORTED,
            evidence=[supports, contradicts],
            decisive_evidence_id=supports.evidence_id,
        )
        assert len(trace.supporting) == 1
        assert len(trace.contradicting) == 1


class TestEvaluationTrace:
    """Chain 3: Research run -> Configuration -> Metrics."""

    def test_metrics_reach_the_configuration_that_produced_them(self):
        trace = EvaluationTrace(
            research_run_id=ID(),
            topic="<topic under test>",
            configuration=configuration(ResearchMode.HYBRID),
            metrics=[metric(MetricKey.CLAIM_SUPPORT_RATE, 0.8)],
        )
        assert trace.configuration.mode is ResearchMode.HYBRID
        assert trace.by_key == {"claim_support_rate": 0.8}

    def test_the_metrics_version_is_derived_from_the_metrics(self):
        trace = EvaluationTrace(
            research_run_id=ID(),
            topic="<topic under test>",
            configuration=configuration(),
            metrics=[metric(MetricKey.COVERAGE, 0.5, "v3")],
        )
        assert trace.metrics_version == "v3"

    def test_mixing_metric_versions_is_rejected(self):
        """Two definitions of a metric are not one set of measurements."""
        with pytest.raises(ValidationError, match="must share a metrics_version"):
            EvaluationTrace(
                research_run_id=ID(),
                topic="<topic under test>",
                configuration=configuration(),
                metrics=[
                    metric(MetricKey.COVERAGE, 0.5, "v1"),
                    metric(MetricKey.CLAIM_SUPPORT_RATE, 0.8, "v2"),
                ],
            )

    def test_a_trace_with_no_metrics_yet_is_valid(self):
        trace = EvaluationTrace(
            research_run_id=ID(), topic="<topic under test>", configuration=configuration()
        )
        assert trace.metrics_version is None
        assert trace.by_key == {}


class TestBenchmarkComparison:
    def _comparison(self, counts: dict[ResearchMode, int]) -> BenchmarkComparison:
        return BenchmarkComparison(
            benchmark_run_id=ID(),
            name="<benchmark under test>",
            topic_set="<topic set under test>",
            topic_set_version="v1",
            metrics_version="v1",
            configurations=[
                ConfigurationMetrics(
                    configuration_id=ID(),
                    mode=mode,
                    fingerprint=f"{mode.value:*<64}",
                    run_count=count,
                )
                for mode, count in counts.items()
            ],
        )

    def test_a_balanced_comparison_is_comparable(self):
        comparison = self._comparison(
            {
                ResearchMode.MODEL_ONLY: 10,
                ResearchMode.HYBRID: 10,
                ResearchMode.SEARCH_GROUNDED: 10,
            }
        )
        assert comparison.comparable is True
        assert comparison.incomparable_reason is None

    def test_unequal_run_counts_are_flagged_not_hidden(self):
        """The difference between the numbers would include a difference in inputs."""
        comparison = self._comparison(
            {ResearchMode.MODEL_ONLY: 10, ResearchMode.HYBRID: 7}
        )
        assert comparison.comparable is False
        assert "model_only=10" in comparison.incomparable_reason
        assert "hybrid=7" in comparison.incomparable_reason

    def test_an_empty_comparison_is_not_flagged(self):
        comparison = self._comparison({})
        assert comparison.comparable is True
