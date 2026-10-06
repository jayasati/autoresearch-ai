"""
Database constraints.

Pydantic validates what arrives over HTTP; these constraints hold for anything that
reaches the database, including a script, a migration or a psql session. The
invariants that matter most are enforced in both places on purpose -- a rule that
only lives in the API layer is a rule the next data-loading script will break.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.constants import (
    CitationStatus,
    EvidenceRelation,
    MetricKey,
    ResearchMode,
    RunStatus,
    SourceType,
    VerificationVerdict,
)
from app.models import (
    Conflict,
    DocumentChunk,
    EvaluationResult,
    LlmCallLog,
    Report,
    ResearchSource,
    Source,
)
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


def expect_integrity_error(db, *instances):
    db.add_all(instances)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


class TestIdentifiers:
    def test_an_id_exists_before_the_row_is_inserted(self, db):
        """What makes the id *stable*: it does not change on persist.

        An orchestrator can therefore build a whole claim/evidence/citation graph
        in memory, referencing ids across it, and persist it in one transaction.
        """
        config = make_configuration()
        assigned = config.id
        assert isinstance(assigned, uuid.UUID)

        db.add(config)
        db.commit()
        db.refresh(config)
        assert config.id == assigned

    def test_ids_are_unique_across_instances(self, db):
        a, b = make_configuration(), make_configuration(ResearchMode.HYBRID)
        assert a.id != b.id

    def test_a_configuration_fingerprint_is_unique(self, db):
        """Two rows for one configuration would split a benchmark group in half."""
        first = make_configuration()
        duplicate = make_configuration()
        assert first.fingerprint == duplicate.fingerprint
        expect_integrity_error(db, first, duplicate)

    def test_a_source_fingerprint_is_unique(self, db):
        a = make_source(doi="10.0000/invalid.same")
        b = make_source(doi="10.0000/invalid.same")
        b.fingerprint = a.fingerprint
        expect_integrity_error(db, a, b)


class TestSpansAndOffsets:
    """The offsets traceability depends on."""

    def test_a_chunk_span_must_be_non_empty(self, db):
        source = make_source()
        document = make_document(source)
        db.add_all([source, document])
        db.flush()
        bad = DocumentChunk(
            document=document,
            position=0,
            text="<text>",
            content_hash="0" * 64,
            start_char=100,
            end_char=100,
            created_at=NOW,
        )
        expect_integrity_error(db, bad)

    def test_a_chunk_span_cannot_be_inverted(self, db):
        source = make_source()
        document = make_document(source)
        db.add_all([source, document])
        db.flush()
        bad = DocumentChunk(
            document=document,
            position=0,
            text="<text>",
            content_hash="0" * 64,
            start_char=200,
            end_char=100,
            created_at=NOW,
        )
        expect_integrity_error(db, bad)

    def test_chunk_positions_are_unique_within_a_document(self, db):
        source = make_source()
        document = make_document(source)
        db.add_all([source, document])
        db.flush()
        expect_integrity_error(db, make_chunk(document, 0), make_chunk(document, 0))

    def test_the_same_position_in_two_documents_is_fine(self, db):
        source = make_source()
        doc_a = make_document(source, "<doc A>")
        doc_b = make_document(source, "<doc B>")
        db.add_all([source, doc_a, doc_b])
        db.flush()
        db.add_all([make_chunk(doc_a, 0), make_chunk(doc_b, 0)])
        db.commit()
        assert len(doc_a.chunks) == 1 and len(doc_b.chunks) == 1


class TestSourceIdentity:
    def test_an_external_source_must_be_identifiable(self, db):
        """A web page with no URL could never be re-checked or deduplicated."""
        orphan = Source(source_type=SourceType.WEB, created_at=NOW, updated_at=NOW)
        expect_integrity_error(db, orphan)

    def test_a_model_asserted_source_needs_no_identifier(self, db):
        """How an uncited claim is recorded rather than silently dropped."""
        asserted = Source(source_type=SourceType.MODEL, created_at=NOW, updated_at=NOW)
        db.add(asserted)
        db.commit()
        assert asserted.url is None

    def test_a_source_is_linked_to_a_run_only_once(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        db.add_all([config, run, source])
        db.flush()
        expect_integrity_error(
            db, link_source(run, source, "S1"), link_source(run, source, "S2")
        )

    def test_a_citation_label_is_unique_within_a_run(self, db):
        """Two sources both labelled S1 would make the report's markers ambiguous."""
        config = make_configuration()
        run = make_run(config)
        a = make_source(doi="10.0000/invalid.one")
        b = make_source(doi="10.0000/invalid.two")
        db.add_all([config, run, a, b])
        db.flush()
        expect_integrity_error(db, link_source(run, a, "S1"), link_source(run, b, "S1"))

    def test_re_fetching_identical_content_is_rejected(self, db):
        """Same bytes, same document -- no second row."""
        source = make_source()
        db.add(source)
        db.flush()
        expect_integrity_error(
            db, make_document(source, "<identical body>"), make_document(source, "<identical body>")
        )

    def test_changed_content_is_a_second_document(self, db):
        """A page that now says something different is a new document, same source."""
        source = make_source()
        db.add(source)
        db.flush()
        db.add_all([make_document(source, "<body v1>"), make_document(source, "<body v2>")])
        db.commit()
        assert len(source.documents) == 2


class TestCitationIntegrity:
    """The four-way distinction, enforced at the database level too."""

    def test_a_fabricated_citation_cannot_name_a_source(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, report, claim])
        db.flush()
        bad = make_citation(claim, source, "S9", CitationStatus.FABRICATED)
        expect_integrity_error(db, bad)

    def test_a_non_fabricated_citation_must_name_one(self, db):
        config = make_configuration()
        run = make_run(config)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, report, claim])
        db.flush()
        bad = make_citation(claim, None, "S1", CitationStatus.MISATTRIBUTED)
        expect_integrity_error(db, bad)

    def test_a_valid_citation_must_name_a_passage(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, report, claim])
        db.flush()
        bad = make_citation(claim, source, "S1", CitationStatus.VALID, chunk=None)
        expect_integrity_error(db, bad)

    def test_a_marker_is_unique_within_a_claim(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        document = make_document(source)
        chunk = make_chunk(document)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, document, chunk, report, claim])
        db.flush()
        expect_integrity_error(
            db,
            make_citation(claim, source, "S1", CitationStatus.VALID, chunk),
            make_citation(claim, source, "S1", CitationStatus.VALID, chunk),
        )


class TestVerificationIntegrity:
    def test_an_evidence_bearing_verdict_must_cite_evidence(self, db):
        """The ARCHITECTURE mitigation, enforced in the schema itself."""
        config = make_configuration()
        run = make_run(config)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, report, claim])
        db.flush()
        bad = make_verification(claim, VerificationVerdict.SUPPORTED, evidence=None)
        expect_integrity_error(db, bad)

    def test_one_verdict_per_claim(self, db):
        config = make_configuration()
        run = make_run(config)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, report, claim])
        db.flush()
        expect_integrity_error(
            db,
            make_verification(claim, VerificationVerdict.UNSUPPORTED, None),
            make_verification(claim, VerificationVerdict.NOT_ENOUGH_EVIDENCE, None),
        )

    def test_a_chunk_is_linked_to_a_claim_only_once(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        document = make_document(source)
        chunk = make_chunk(document)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, document, chunk, report, claim])
        db.flush()
        expect_integrity_error(
            db,
            make_evidence(claim, chunk, EvidenceRelation.SUPPORTS),
            make_evidence(claim, chunk, EvidenceRelation.NEUTRAL),
        )


class TestConflictIntegrity:
    def test_a_passage_cannot_conflict_with_itself(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        document = make_document(source)
        chunk = make_chunk(document)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, document, chunk, report, claim])
        db.flush()
        evidence = make_evidence(claim, chunk)
        db.add(evidence)
        db.flush()

        from app.core.constants import ConflictType

        bad = Conflict(
            research_run=run,
            evidence_a_id=evidence.id,
            evidence_b_id=evidence.id,
            conflict_type=ConflictType.NUMERIC_DISAGREEMENT,
            description="<description under test>",
            detected_at=NOW,
        )
        expect_integrity_error(db, bad)


class TestRunIntegrity:
    def test_a_failed_run_must_record_why(self, db):
        """A silently failed run would corrupt every metric computed over the set."""
        config = make_configuration()
        run = make_run(config, status=RunStatus.FAILED)
        run.error_code = None
        expect_integrity_error(db, config, run)

    def test_a_failed_run_with_an_error_code_is_accepted(self, db):
        config = make_configuration()
        run = make_run(config, status=RunStatus.FAILED)
        run.error_code = "external_service_error"
        run.error_message = "<message under test>"
        db.add_all([config, run])
        db.commit()
        assert run.status is RunStatus.FAILED

    def test_a_run_cannot_complete_before_it_starts(self, db):
        from datetime import timedelta

        config = make_configuration()
        run = make_run(config)
        run.completed_at = NOW - timedelta(minutes=5)
        expect_integrity_error(db, config, run)

    def test_at_most_one_report_per_run(self, db):
        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.flush()
        first = make_report(run)
        db.add(first)
        db.commit()

        second = Report(
            research_run_id=run.id,
            title="<second report>",
            markdown="<body>",
            word_count=1,
            generated_at=NOW,
        )
        expect_integrity_error(db, second)

    def test_subquestion_positions_are_unique_within_a_run(self, db):
        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.flush()
        expect_integrity_error(db, make_subquestion(run, 0), make_subquestion(run, 0))

    def test_a_failed_llm_call_must_record_why(self, db):
        from app.core.constants import PipelineStage

        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.flush()
        bad = LlmCallLog(
            research_run=run,
            stage=PipelineStage.SYNTHESIS,
            model="<model under test>",
            succeeded=False,
            error_code=None,
            created_at=NOW,
        )
        expect_integrity_error(db, bad)


class TestEvaluationIntegrity:
    def test_a_metric_is_recorded_once_per_version(self, db):
        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.flush()

        def metric(version: str) -> EvaluationResult:
            return EvaluationResult(
                research_run=run,
                metric_key=MetricKey.CLAIM_SUPPORT_RATE,
                value=0.5,
                numerator=1,
                denominator=2,
                metrics_version=version,
                computed_at=NOW,
            )

        expect_integrity_error(db, metric("v1"), metric("v1"))

    def test_the_same_metric_under_two_versions_coexists(self, db):
        """Changing a definition does not make the old number wrong, only different."""
        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.flush()
        db.add_all(
            [
                EvaluationResult(
                    research_run=run,
                    metric_key=MetricKey.CLAIM_SUPPORT_RATE,
                    value=0.5,
                    metrics_version=version,
                    computed_at=NOW,
                )
                for version in ("v1", "v2")
            ]
        )
        db.commit()
        assert len(run.evaluation_results) == 2

    def test_a_zero_denominator_is_rejected(self, db):
        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.flush()
        bad = EvaluationResult(
            research_run=run,
            metric_key=MetricKey.CLAIM_SUPPORT_RATE,
            value=0.0,
            numerator=0,
            denominator=0,
            metrics_version="v1",
            computed_at=NOW,
        )
        expect_integrity_error(db, bad)

    def test_a_numerator_cannot_exceed_its_denominator(self, db):
        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.flush()
        bad = EvaluationResult(
            research_run=run,
            metric_key=MetricKey.CITATION_PRECISION,
            value=1.0,
            numerator=9,
            denominator=4,
            metrics_version="v1",
            computed_at=NOW,
        )
        expect_integrity_error(db, bad)


class TestCascades:
    def test_deleting_a_run_removes_everything_it_produced(self, db):
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        link_source(run, source, "S1")
        document = make_document(source)
        chunk = make_chunk(document)
        report = make_report(run)
        claim = make_claim(report)
        db.add_all([config, run, source, document, chunk, report, claim])
        db.flush()
        evidence = make_evidence(claim, chunk)
        db.add(evidence)
        db.flush()
        db.add_all(
            [
                make_citation(claim, source, "S1", CitationStatus.VALID, chunk),
                make_verification(claim, VerificationVerdict.SUPPORTED, evidence),
            ]
        )
        db.commit()

        from app.models import Citation, Claim, Evidence, VerificationResult

        db.delete(run)
        db.commit()

        assert db.query(Claim).count() == 0
        assert db.query(Evidence).count() == 0
        assert db.query(Citation).count() == 0
        assert db.query(VerificationResult).count() == 0
        assert db.query(ResearchSource).count() == 0

    def test_deleting_a_run_does_not_delete_the_source(self, db):
        """Sources are shared: another run may still cite the same paper."""
        config = make_configuration()
        run = make_run(config)
        source = make_source()
        link_source(run, source, "S1")
        db.add_all([config, run, source])
        db.commit()

        db.delete(run)
        db.commit()

        assert db.query(Source).count() == 1

    def test_a_configuration_cannot_be_deleted_while_runs_reference_it(self, db):
        """Losing it would make every metric computed under it uninterpretable."""
        config = make_configuration()
        run = make_run(config)
        db.add_all([config, run])
        db.commit()

        db.delete(config)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestEnumStorage:
    def test_enum_values_are_stored_not_member_names(self, db):
        """SQLAlchemy stores the *name* by default; the API speaks in values.

        If this regressed, raw SQL and every export would disagree with the API
        about what a mode is called.
        """
        from sqlalchemy import text

        config = make_configuration(ResearchMode.SEARCH_GROUNDED)
        run = make_run(config)
        db.add_all([config, run])
        db.commit()

        stored = db.execute(text("SELECT mode FROM run_configuration")).scalar_one()
        assert stored == "search_grounded"

        status = db.execute(text("SELECT status FROM research_run")).scalar_one()
        assert status == "completed"

    def test_an_invalid_enum_value_is_rejected(self, db):
        """`validate_strings=True` catches a value outside the enum on write."""
        from sqlalchemy.exc import StatementError

        config = make_configuration()
        db.add(config)
        db.commit()

        config.mode = "telepathy"
        with pytest.raises(StatementError) as caught:
            db.commit()
        assert isinstance(caught.value.__cause__, LookupError)
        db.rollback()
