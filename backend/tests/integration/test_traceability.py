"""
The three required traceability chains, walked end to end against a real schema.

Each test builds the minimum graph and then *traverses* it by following
relationships, rather than asserting on the ids it just set. Traversal is the only
thing that proves the chain is navigable in the direction a user needs it.
"""

from app.core.constants import (
    CitationStatus,
    EvidenceRelation,
    MetricKey,
    ResearchMode,
    SourceType,
    VerificationVerdict,
)
from app.models import EvaluationResult
from tests.integration.builders import (
    NOW,
    link_source,
    make_chunk,
    make_citation,
    make_claim,
    make_configuration,
    make_document,
    make_evidence,
    make_report,
    make_run,
    make_source,
    make_subquestion,
    make_verification,
)


class TestChainOneEvidence:
    """Research -> Report -> Claim -> Citation -> Source -> Evidence chunk."""

    def test_the_whole_chain_is_navigable_from_the_run(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        link_source(run, source, "S1")
        document = make_document(source)
        chunk = make_chunk(document)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, document, chunk, report, claim])
        db.flush()  # ids exist before this, but the chunk FK needs the row

        citation = make_citation(claim, source, "S1", CitationStatus.VALID, chunk)
        db.add(citation)
        db.commit()

        # Now walk it, without reusing any local variable as a shortcut.
        from app.models import ResearchRun

        fetched = db.get(ResearchRun, run.id)
        assert fetched is not None

        step_report = fetched.report
        assert step_report is not None

        step_claim = step_report.claims[0]
        step_citation = step_claim.citations[0]
        step_source = step_citation.source
        assert step_source is not None

        step_chunk = db.get(type(chunk), step_citation.supporting_chunk_id)
        assert step_chunk is not None
        assert step_chunk.document.source.id == step_source.id

        # The end of the chain is a character span, not just a source.
        assert step_chunk.end_char > step_chunk.start_char

    def test_a_claim_reaches_its_source_through_the_citation(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source(source_type=SourceType.WEB)
        link_source(run, source)
        document = make_document(source)
        chunk = make_chunk(document)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, document, chunk, report, claim])
        db.flush()
        db.add(make_citation(claim, source, "S1", CitationStatus.VALID, chunk))
        db.commit()

        assert claim.citations[0].source.url.endswith(tuple("0123456789abcdef"))

    def test_a_fabricated_citation_breaks_the_chain_honestly(self, db):
        """The chain must be able to *end* at a citation that resolves to nothing.

        This is the case the project exists to count, so the schema has to
        represent it rather than making it unexpressible.
        """
        config = make_configuration()
        run = make_run(config)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, report, claim])
        db.flush()
        db.add(make_citation(claim, None, "S9", CitationStatus.FABRICATED))
        db.commit()

        citation = claim.citations[0]
        assert citation.status is CitationStatus.FABRICATED
        assert citation.source is None
        assert citation.failure_reason is not None

    def test_one_source_shared_by_two_runs_is_one_row(self, db):
        """Deduplication is what makes source diversity measurable."""
        config = make_configuration()
        run_a = make_run(config, topic="<topic A under test>")
        run_b = make_run(config, topic="<topic B under test>")
        source = make_source(doi="10.0000/invalid.shared")
        link_source(run_a, source, "S1")
        link_source(run_b, source, "S1")
        db.add_all([config, run_a, run_b, source])
        db.commit()

        assert len(source.research_sources) == 2
        assert {rs.research_run_id for rs in source.research_sources} == {run_a.id, run_b.id}

    def test_evidence_depth_is_per_run_not_per_source(self, db):
        """One run may read full text where another saw only an abstract."""
        from app.core.constants import EvidenceDepth

        config = make_configuration()
        run_a = make_run(config, topic="<topic A under test>")
        run_b = make_run(config, topic="<topic B under test>")
        source = make_source(doi="10.0000/invalid.depth")
        link_source(run_a, source, "S1", evidence_depth=EvidenceDepth.FULL_TEXT)
        link_source(run_b, source, "S1", evidence_depth=EvidenceDepth.ABSTRACT)
        db.add_all([config, run_a, run_b, source])
        db.commit()

        depths = {rs.research_run_id: rs.evidence_depth for rs in source.research_sources}
        assert depths[run_a.id] is EvidenceDepth.FULL_TEXT
        assert depths[run_b.id] is EvidenceDepth.ABSTRACT


class TestChainTwoVerification:
    """Claim -> Evidence -> VerificationResult."""

    def test_a_verdict_leads_back_to_the_passage_it_rests_on(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        link_source(run, source)
        document = make_document(source, "<document body under test>")
        chunk = make_chunk(document)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, document, chunk, report, claim])
        db.flush()

        evidence = make_evidence(claim, chunk, EvidenceRelation.SUPPORTS)
        db.add(evidence)
        db.flush()
        db.add(make_verification(claim, VerificationVerdict.SUPPORTED, evidence))
        db.commit()

        result = claim.verification_result
        assert result is not None
        assert result.verdict is VerificationVerdict.SUPPORTED

        # Follow the verdict to the evidence, the chunk, the document, the source.
        decisive = result.supporting_evidence
        assert decisive is not None
        assert decisive.quoted_text is not None
        assert decisive.document_chunk.document.source.id == source.id

    def test_a_claim_can_hold_supporting_and_contradicting_evidence_at_once(self, db):
        """The input conflict detection needs; a single boolean would erase it."""
        config = make_configuration()
        run = make_run(config)
        source_a = make_source(doi="10.0000/invalid.a")
        source_b = make_source(doi="10.0000/invalid.b")
        link_source(run, source_a, "S1")
        link_source(run, source_b, "S2")
        chunk_a = make_chunk(make_document(source_a, "<doc A>"))
        chunk_b = make_chunk(make_document(source_b, "<doc B>"))
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source_a, source_b, chunk_a, chunk_b, report, claim])
        db.flush()
        db.add_all(
            [
                make_evidence(claim, chunk_a, EvidenceRelation.SUPPORTS),
                make_evidence(claim, chunk_b, EvidenceRelation.CONTRADICTS),
            ]
        )
        db.commit()

        assert len(claim.supporting_evidence) == 1
        assert len(claim.contradicting_evidence) == 1

    def test_an_unsupported_verdict_needs_no_evidence(self, db):
        """Nothing was found, so there is nothing to point at."""
        config = make_configuration()
        run = make_run(config)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, report, claim])
        db.flush()
        db.add(make_verification(claim, VerificationVerdict.UNSUPPORTED, None))
        db.commit()

        assert claim.verification_result.supporting_evidence is None
        assert claim.verification_result.is_hallucination is True

    def test_not_enough_evidence_is_not_a_hallucination(self, db):
        """"We could not tell" is a different finding from "this is wrong"."""
        config = make_configuration()
        run = make_run(config)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, report, claim])
        db.flush()
        db.add(
            make_verification(claim, VerificationVerdict.NOT_ENOUGH_EVIDENCE, None, downgraded=True)
        )
        db.commit()

        assert claim.verification_result.is_hallucination is False
        assert claim.verification_result.downgraded is True


class TestChainThreeEvaluation:
    """Research run -> Configuration -> Metrics."""

    def test_metrics_reach_the_configuration_that_produced_them(self, db):
        config = make_configuration(ResearchMode.HYBRID)
        run = make_run(config)
        db.add_all([config, run])
        db.flush()
        db.add(
            EvaluationResult(
                research_run=run,
                metric_key=MetricKey.CLAIM_SUPPORT_RATE,
                value=0.75,
                numerator=3,
                denominator=4,
                unit="ratio",
                metrics_version="v1",
                computed_at=NOW,
            )
        )
        db.commit()

        metric = run.evaluation_results[0]
        assert metric.research_run.configuration.mode is ResearchMode.HYBRID
        assert metric.research_run.configuration.fingerprint == config.fingerprint

    def test_a_ratio_keeps_the_terms_behind_it(self, db):
        """0.75 alone is not reportable; 3/4 says how much it is worth."""
        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.flush()
        result = EvaluationResult(
            research_run=run,
            metric_key=MetricKey.CITATION_PRECISION,
            value=0.5,
            numerator=1,
            denominator=2,
            metrics_version="v1",
            computed_at=NOW,
        )
        db.add(result)
        db.commit()

        assert (result.numerator, result.denominator) == (1, 2)

    def test_two_configurations_of_the_same_topic_are_comparable_by_fingerprint(self, db):
        """What a benchmark comparison joins on."""
        model_only = make_configuration(ResearchMode.MODEL_ONLY)
        grounded = make_configuration(ResearchMode.SEARCH_GROUNDED)
        run_a = make_run(model_only, topic="<shared topic under test>")
        run_b = make_run(grounded, topic="<shared topic under test>")
        db.add_all([model_only, grounded, run_a, run_b])
        db.commit()

        assert run_a.normalized_topic == run_b.normalized_topic
        assert run_a.configuration.fingerprint != run_b.configuration.fingerprint

    def test_coverage_is_computable_because_the_plan_is_stored(self, db):
        config = make_configuration()
        run = make_run(config)
        db.add_all(
            [config, run]
            + [make_subquestion(run, i, addressed=(i < 2)) for i in range(3)]
        )
        db.commit()

        addressed = sum(1 for sq in run.subquestions if sq.addressed)
        assert (addressed, len(run.subquestions)) == (2, 3)
