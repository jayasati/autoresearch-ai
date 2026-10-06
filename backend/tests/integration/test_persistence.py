"""
Persistence: connection, the repository layer, and the entities this stage requires.

Every test runs against a real database (throwaway SQLite with foreign keys enforced)
through the real repository and service code — nothing is mocked. Placeholder values
stay deliberately non-plausible: `<topic under test>`, `example.invalid`.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.constants import (
    CitationStatus,
    EvidenceDepth,
    EvidenceRelation,
    MetricKey,
    PipelineStage,
    ResearchMode,
    RunStatus,
    SourceType,
    VerificationVerdict,
)
from app.core.exceptions import ConflictError, NotFoundError
from app.db.session import build_engine, check_connection, unit_of_work
from app.repositories import (
    CitationRepository,
    ClaimRepository,
    DocumentChunkRepository,
    DocumentRepository,
    EvaluationResultRepository,
    EvidenceRepository,
    LlmCallLogRepository,
    ReportRepository,
    ResearchRunRepository,
    RunConfigurationRepository,
    SourceRepository,
    VerificationResultRepository,
)
from app.schemas.research import ResearchRequest
from app.services.research_service import ResearchService

TOPIC = "<topic under test, long enough>"


# --------------------------------------------------------------------------- #
# 1. Database connection
# --------------------------------------------------------------------------- #


class TestDatabaseConnection:
    def test_the_engine_connects_and_answers(self, db_engine):
        with db_engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1

    def test_check_connection_reports_a_reachable_database(self, db_engine):
        reachable, latency_ms, error = check_connection(db_engine)
        assert reachable is True
        assert latency_ms >= 0
        assert error is None

    def test_check_connection_reports_an_unreachable_one_without_raising(self):
        """A health endpoint needs a fact, not an exception to turn into a 500."""
        from app.core.config import Settings

        engine = build_engine(
            Settings(DATABASE_URL="postgresql+psycopg://nobody:nothing@127.0.0.1:1/absent")
        )
        reachable, latency_ms, error = check_connection(engine)
        assert reachable is False
        assert error is not None
        assert latency_ms >= 0

    def test_a_connection_error_never_leaks_the_password(self):
        from app.core.config import Settings

        engine = build_engine(
            Settings(DATABASE_URL="postgresql+psycopg://user:sup3rs3cret@127.0.0.1:1/absent")
        )
        _, _, error = check_connection(engine)
        assert "sup3rs3cret" not in (error or "")

    def test_foreign_keys_are_actually_enforced(self, db):
        """Without the SQLite pragma this passes while PostgreSQL would reject it."""
        from app.models import Report

        orphan = Report(
            research_run_id=uuid.uuid4(),
            title="<title>",
            markdown="<body>",
            word_count=1,
            generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        db.add(orphan)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_the_schema_is_present(self, db):
        from app.models import Base

        for table in Base.metadata.sorted_tables:
            db.execute(text(f"SELECT COUNT(*) FROM {table.name}"))  # noqa: S608 - fixed names


# --------------------------------------------------------------------------- #
# 2. Create research
# --------------------------------------------------------------------------- #


class TestCreateResearch:
    def test_a_run_is_created_in_pending_with_its_configuration(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC, mode=ResearchMode.SEARCH_GROUNDED))

        assert run.status is RunStatus.PENDING
        assert run.configuration.mode is ResearchMode.SEARCH_GROUNDED
        assert len(run.configuration.fingerprint) == 64

    def test_the_run_is_actually_committed(self, service_session, db_engine):
        """Read it back on a fresh connection, not from the identity map."""
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))

        with db_engine.connect() as connection:
            stored = connection.execute(
                text("SELECT topic, status FROM research_run WHERE id = :id"),
                {"id": str(run.id).replace("-", "")},
            ).first()
        assert stored is not None
        assert stored[1] == "pending"

    def test_the_topic_is_normalized_for_benchmark_grouping(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic="  RAG   And   Hallucination Rates  "))
        assert run.normalized_topic == "rag and hallucination rates"

    def test_two_runs_of_one_mode_share_a_configuration_row(self, service_session):
        """Two configuration rows would split a benchmark group in half."""
        service = ResearchService()
        a = service.create_run(ResearchRequest(topic=TOPIC, mode=ResearchMode.HYBRID))
        b = service.create_run(
            ResearchRequest(topic="<another topic under test>", mode=ResearchMode.HYBRID)
        )
        assert a.configuration_id == b.configuration_id

    def test_different_modes_get_different_configurations(self, service_session):
        service = ResearchService()
        a = service.create_run(ResearchRequest(topic=TOPIC, mode=ResearchMode.MODEL_ONLY))
        b = service.create_run(ResearchRequest(topic=TOPIC, mode=ResearchMode.SEARCH_GROUNDED))
        assert a.configuration.fingerprint != b.configuration.fingerprint

    def test_model_only_carries_no_retrieval_settings(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC, mode=ResearchMode.MODEL_ONLY))
        assert run.configuration.chunk_size is None
        assert run.configuration.top_k is None

    def test_a_retrieving_mode_does_carry_them(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC, mode=ResearchMode.HYBRID))
        assert run.configuration.chunk_size is not None
        assert run.configuration.embedding_model is not None

    def test_a_missing_benchmark_is_reported_as_not_found(self, service_session):
        service = ResearchService()
        with pytest.raises(NotFoundError):
            service.create_run(
                ResearchRequest(topic=TOPIC, benchmark_run_id=uuid.uuid4())
            )

    def test_a_rejected_run_leaves_no_configuration_behind(self, service_session, db_engine):
        """One transaction: a failed creation must not strand a settings row."""
        service = ResearchService()
        before = RunConfigurationRepository(service_session).count()
        with pytest.raises(NotFoundError):
            service.create_run(ResearchRequest(topic=TOPIC, benchmark_run_id=uuid.uuid4()))

        with build_engine(
            __import__("app.core.config", fromlist=["Settings"]).Settings(
                DATABASE_URL=str(db_engine.url)
            )
        ).connect() as connection:
            after = connection.execute(text("SELECT COUNT(*) FROM run_configuration")).scalar_one()
        assert after == before

    def test_a_run_can_be_read_back_with_its_relations(self, service_session):
        service = ResearchService()
        created = service.create_run(ResearchRequest(topic=TOPIC))
        fetched = service.get_run(created.id)
        assert fetched.id == created.id
        assert fetched.configuration is not None

    def test_reading_a_missing_run_raises_not_found(self, service_session):
        with pytest.raises(NotFoundError, match="No research run"):
            ResearchService().get_run(uuid.uuid4())


class TestStatusTransitions:
    def test_a_legal_transition_is_applied_and_sets_started_at(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        advanced = service.advance_status(run.id, RunStatus.PLANNING)
        assert advanced.status is RunStatus.PLANNING
        assert advanced.started_at is not None

    def test_an_illegal_transition_is_refused(self, service_session):
        """A completed run going back to retrieving would produce a second report."""
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        with pytest.raises(ConflictError, match="Cannot move a run"):
            service.advance_status(run.id, RunStatus.COMPLETED)

    def test_a_completed_run_is_final(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        for status in (RunStatus.PLANNING, RunStatus.RETRIEVING, RunStatus.DRAFTING):
            service.advance_status(run.id, status)
        service.advance_status(run.id, RunStatus.COMPLETED)
        with pytest.raises(ConflictError):
            service.advance_status(run.id, RunStatus.VERIFYING)

    def test_completing_a_run_records_when(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        for status in (RunStatus.PLANNING, RunStatus.DRAFTING):
            service.advance_status(run.id, status)
        completed = service.advance_status(run.id, RunStatus.COMPLETED)
        assert completed.completed_at is not None

    def test_failing_a_run_records_the_cause(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        failed = service.fail_run(run.id, code="external_service_error", message="<detail>")
        assert failed.status is RunStatus.FAILED
        assert failed.error_code == "external_service_error"

    def test_the_plan_is_recorded_once(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        service.record_plan(run.id, ["<q0>", "<q1>", "<q2>"])
        reloaded = service.get_run(run.id)
        assert [q.position for q in reloaded.subquestions] == [0, 1, 2]

    def test_recording_a_plan_twice_is_refused(self, service_session):
        """A second plan would change coverage's denominator after the fact."""
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        service.record_plan(run.id, ["<q0>"])
        with pytest.raises(ConflictError, match="already has a plan"):
            service.record_plan(run.id, ["<q0>", "<q1>"])


# --------------------------------------------------------------------------- #
# 3. Create source
# --------------------------------------------------------------------------- #


class TestCreateSource:
    def test_a_source_is_created_and_linked_to_a_run(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        source, link = service.attach_source(
            run.id,
            fingerprint="f" * 64,
            source_type=SourceType.ACADEMIC,
            doi="10.0000/invalid.one",
            title="<source title under test>",
            citation_label="S1",
        )
        assert source.id is not None
        assert link.citation_label == "S1"

    def test_the_same_source_in_two_runs_is_one_row(self, service_session):
        """Deduplication is what makes source diversity measurable."""
        service = ResearchService()
        run_a = service.create_run(ResearchRequest(topic=TOPIC))
        run_b = service.create_run(ResearchRequest(topic="<second topic under test>"))
        shared = "e" * 64

        source_a, _ = service.attach_source(
            run_a.id, fingerprint=shared, source_type=SourceType.WEB,
            url="https://example.invalid/shared", citation_label="S1",
        )
        source_b, _ = service.attach_source(
            run_b.id, fingerprint=shared, source_type=SourceType.WEB,
            url="https://example.invalid/shared", citation_label="S1",
        )
        assert source_a.id == source_b.id
        assert SourceRepository(service_session).count() == 1

    def test_an_unidentifiable_source_is_never_deduplicated(self, service_session):
        """A model assertion has no external reference, so each is its own row."""
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        first, _ = service.attach_source(
            run.id, fingerprint=None, source_type=SourceType.MODEL, citation_label="S1"
        )
        run_b = service.create_run(ResearchRequest(topic="<other topic under test>"))
        second, _ = service.attach_source(
            run_b.id, fingerprint=None, source_type=SourceType.MODEL, citation_label="S1"
        )
        assert first.id != second.id

    def test_evidence_depth_is_recorded_per_run(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))
        with unit_of_work() as session:
            runs = ResearchRunRepository(session)
            sources = SourceRepository(session)
            stored_run = runs.get_or_raise(run.id)
            source, _ = sources.get_or_create(
                "d" * 64, source_type=SourceType.ACADEMIC, doi="10.0000/invalid.depth"
            )
            link = sources.link_to_run(
                stored_run, source, citation_label="S1", evidence_depth=EvidenceDepth.FULL_TEXT
            )
        assert link.evidence_depth is EvidenceDepth.FULL_TEXT

    def test_a_document_and_its_chunks_are_stored_with_offsets(self, service_session):
        with unit_of_work() as session:
            sources = SourceRepository(session)
            source, _ = sources.get_or_create(
                "c" * 64, source_type=SourceType.WEB, url="https://example.invalid/doc"
            )
            body = "<first passage><second passage>"
            document, created = DocumentRepository(session).get_or_create(
                source=source, text=body, depth=EvidenceDepth.FULL_TEXT
            )
            chunks = DocumentChunkRepository(session).add_chunks(
                document, [(0, 15, body[0:15]), (15, 31, body[15:31])]
            )
        assert created is True
        assert [(c.start_char, c.end_char) for c in chunks] == [(0, 15), (15, 31)]

    def test_an_unchanged_refetch_does_not_create_a_second_document(self, service_session):
        with unit_of_work() as session:
            sources = SourceRepository(session)
            documents = DocumentRepository(session)
            source, _ = sources.get_or_create(
                "b" * 64, source_type=SourceType.WEB, url="https://example.invalid/stable"
            )
            first, created_first = documents.get_or_create(
                source=source, text="<identical body>", depth=EvidenceDepth.FULL_TEXT
            )
            second, created_second = documents.get_or_create(
                source=source, text="<identical body>", depth=EvidenceDepth.FULL_TEXT
            )
        assert created_first is True and created_second is False
        assert first.id == second.id

    def test_changed_content_is_a_second_document(self, service_session):
        """A claim verified against the old text must keep pointing at it."""
        with unit_of_work() as session:
            sources = SourceRepository(session)
            documents = DocumentRepository(session)
            source, _ = sources.get_or_create(
                "a" * 64, source_type=SourceType.WEB, url="https://example.invalid/changing"
            )
            v1, _ = documents.get_or_create(
                source=source, text="<body v1>", depth=EvidenceDepth.FULL_TEXT
            )
            v2, created = documents.get_or_create(
                source=source, text="<body v2>", depth=EvidenceDepth.FULL_TEXT
            )
        assert created is True
        assert v1.id != v2.id


# --------------------------------------------------------------------------- #
# 4 & 5. Create claim, create verification result
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded_claim(service_session):
    """A run with a report, one claim, one chunk and one piece of evidence.

    Built per test and discarded with it; nothing is seeded into a shipped database.
    """
    service = ResearchService()
    run = service.create_run(ResearchRequest(topic=TOPIC))

    # unit_of_work() with no argument opens its own transaction and commits it, so
    # the rows are visible to any later session -- including the ones the service
    # opens for itself. Joining service_session instead would leave everything
    # uncommitted and invisible outside that one session.
    with unit_of_work() as session:
        runs = ResearchRunRepository(session)
        stored_run = runs.get_or_raise(run.id)

        source, _ = SourceRepository(session).get_or_create(
            "9" * 64, source_type=SourceType.ACADEMIC, doi="10.0000/invalid.claim"
        )
        SourceRepository(session).link_to_run(stored_run, source, citation_label="S1")

        body = "<a passage that would support the claim>"
        document, _ = DocumentRepository(session).get_or_create(
            source=source, text=body, depth=EvidenceDepth.FULL_TEXT
        )
        chunk = DocumentChunkRepository(session).add_chunks(
            document, [(0, len(body), body)]
        )[0]
        session.flush()

        report = ReportRepository(session).create(
            run=stored_run, title="<report title under test>", markdown="<report body under test>"
        )
        claim = ClaimRepository(session).create(
            report=report, position=0, text="<claim 0 under test>"
        )
        evidence = EvidenceRepository(session).create(
            claim=claim,
            chunk=chunk,
            relation=EvidenceRelation.SUPPORTS,
            quoted_text=body,
            quote_start_char=0,
            quote_end_char=len(body),
            similarity=0.77,
            rank=0,
        )

    return {
        "run": run,
        "source": source,
        "chunk": chunk,
        "report": report,
        "claim": claim,
        "evidence": evidence,
    }


class TestCreateClaim:
    def test_a_claim_is_stored_against_its_report(self, seeded_claim, service_session):
        claim = seeded_claim["claim"]
        assert claim.report_id == seeded_claim["report"].id
        assert ClaimRepository(service_session).count_for_report(seeded_claim["report"].id) == 1

    def test_claim_positions_are_unique_within_a_report(self, seeded_claim, service_session):
        with pytest.raises(IntegrityError), unit_of_work(service_session) as session:
            ClaimRepository(session).create(
                report=seeded_claim["report"], position=0, text="<duplicate position>"
            )

    def test_a_claim_not_requiring_a_citation_is_excluded_from_recall(
        self, seeded_claim, service_session
    ):
        """The denominator of citation recall, which must not count framing sentences."""
        with unit_of_work() as session:
            ClaimRepository(session).create(
                report=seeded_claim["report"],
                position=1,
                text="<definition needing no citation>",
                requires_citation=False,
            )
        service_session.expire_all()
        claims = ClaimRepository(service_session)
        report_id = seeded_claim["report"].id
        assert claims.count_for_report(report_id) == 2
        assert claims.count_requiring_citation(report_id) == 1

    def test_a_citation_is_stored_with_its_outcome(self, seeded_claim, service_session):
        with unit_of_work() as session:
            citation = CitationRepository(session).create(
                claim=seeded_claim["claim"],
                marker="S1",
                status=CitationStatus.VALID,
                source_id=seeded_claim["source"].id,
                supporting_chunk_id=seeded_claim["chunk"].id,
            )
        assert citation.status is CitationStatus.VALID

    def test_a_fabricated_citation_is_stored_with_no_source(self, seeded_claim, service_session):
        """The case the project exists to count has to be representable."""
        with unit_of_work() as session:
            citation = CitationRepository(session).create(
                claim=seeded_claim["claim"],
                marker="S9",
                status=CitationStatus.FABRICATED,
                failure_reason="no such paper exists",
            )
        assert citation.source_id is None

    def test_citation_outcomes_are_counted_by_status(self, seeded_claim, service_session):
        with unit_of_work() as session:
            claims = ClaimRepository(session)
            citations = CitationRepository(session)
            second = claims.create(report=seeded_claim["report"], position=1, text="<claim 1>")
            citations.create(
                claim=seeded_claim["claim"],
                marker="S1",
                status=CitationStatus.VALID,
                source_id=seeded_claim["source"].id,
                supporting_chunk_id=seeded_claim["chunk"].id,
            )
            citations.create(
                claim=second,
                marker="S9",
                status=CitationStatus.FABRICATED,
                failure_reason="no such paper exists",
            )

        service_session.expire_all()
        counts = CitationRepository(service_session).count_by_status(seeded_claim["report"].id)
        assert counts[CitationStatus.VALID] == 1
        assert counts[CitationStatus.FABRICATED] == 1


class TestCreateVerificationResult:
    def test_a_verdict_is_stored_with_the_evidence_it_rests_on(self, seeded_claim, service_session):
        with unit_of_work() as session:
            result = VerificationResultRepository(session).create(
                claim=seeded_claim["claim"],
                verdict=VerificationVerdict.SUPPORTED,
                supporting_evidence=seeded_claim["evidence"],
                confidence=0.9,
                rationale="<rationale under test>",
            )
        assert result.verdict is VerificationVerdict.SUPPORTED
        assert result.supporting_evidence_id == seeded_claim["evidence"].id
        assert result.downgraded is False

    def test_an_unquoted_supported_verdict_is_downgraded(self, seeded_claim, service_session):
        """The ARCHITECTURE mitigation, applied in the repository too.

        This path is reachable from a script that never touches HTTP, so the rule
        cannot live only in the schema layer.
        """
        with unit_of_work() as session:
            result = VerificationResultRepository(session).create(
                claim=seeded_claim["claim"],
                verdict=VerificationVerdict.SUPPORTED,
                supporting_evidence=None,
            )
        assert result.verdict is VerificationVerdict.NOT_ENOUGH_EVIDENCE
        assert result.downgraded is True

    def test_an_unsupported_verdict_needs_no_evidence(self, seeded_claim, service_session):
        with unit_of_work() as session:
            result = VerificationResultRepository(session).create(
                claim=seeded_claim["claim"], verdict=VerificationVerdict.UNSUPPORTED
            )
        assert result.verdict is VerificationVerdict.UNSUPPORTED
        assert result.downgraded is False
        assert result.is_hallucination is True

    def test_one_verdict_per_claim(self, seeded_claim, service_session):
        with unit_of_work() as session:
            VerificationResultRepository(session).create(
                claim=seeded_claim["claim"], verdict=VerificationVerdict.UNSUPPORTED
            )
        with pytest.raises(IntegrityError), unit_of_work(service_session) as session:
            VerificationResultRepository(session).create(
                claim=seeded_claim["claim"], verdict=VerificationVerdict.NOT_ENOUGH_EVIDENCE
            )

    def test_verdicts_are_counted_for_the_support_rate(self, seeded_claim, service_session):
        with unit_of_work() as session:
            claims = ClaimRepository(session)
            verifications = VerificationResultRepository(session)
            verifications.create(
                claim=seeded_claim["claim"],
                verdict=VerificationVerdict.SUPPORTED,
                supporting_evidence=seeded_claim["evidence"],
            )
            second = claims.create(report=seeded_claim["report"], position=1, text="<claim 1>")
            verifications.create(claim=second, verdict=VerificationVerdict.UNSUPPORTED)

        service_session.expire_all()
        counts = VerificationResultRepository(service_session).count_by_verdict(
            seeded_claim["report"].id
        )
        assert counts[VerificationVerdict.SUPPORTED] == 1
        assert counts[VerificationVerdict.UNSUPPORTED] == 1

    def test_a_verdict_can_be_fetched_for_its_claim(self, seeded_claim, service_session):
        with unit_of_work() as session:
            VerificationResultRepository(session).create(
                claim=seeded_claim["claim"],
                verdict=VerificationVerdict.PARTIALLY_SUPPORTED,
                supporting_evidence=seeded_claim["evidence"],
            )
        found = VerificationResultRepository(service_session).get_for_claim(
            seeded_claim["claim"].id
        )
        assert found is not None
        assert found.verdict is VerificationVerdict.PARTIALLY_SUPPORTED


# --------------------------------------------------------------------------- #
# 6. Retrieve relationships
# --------------------------------------------------------------------------- #


class TestRetrieveRelationships:
    def test_the_evidence_chain_loads_through_the_repository(self, seeded_claim, service_session):
        """Research -> Report -> Claim -> Citation -> Source, plus the chunk."""
        with unit_of_work(service_session) as session:
            CitationRepository(session).create(
                claim=seeded_claim["claim"],
                marker="S1",
                status=CitationStatus.VALID,
                source_id=seeded_claim["source"].id,
                supporting_chunk_id=seeded_claim["chunk"].id,
            )

        claim = ClaimRepository(service_session).get_trace(seeded_claim["claim"].id)
        assert claim is not None
        assert claim.report.research_run.id == seeded_claim["run"].id

        citation = claim.citations[0]
        assert citation.source is not None
        assert citation.source.id == seeded_claim["source"].id

        evidence = claim.evidence[0]
        assert evidence.document_chunk.document.source.id == seeded_claim["source"].id
        assert evidence.document_chunk.end_char > evidence.document_chunk.start_char

    def test_the_verification_chain_loads_all_evidence_not_only_the_decisive_one(
        self, seeded_claim, service_session
    ):
        """A reader needs the evidence that was weighed and rejected."""
        with unit_of_work() as session:
            sources = SourceRepository(session)
            other, _ = sources.get_or_create(
                "8" * 64, source_type=SourceType.ACADEMIC, doi="10.0000/invalid.contra"
            )
            body = "<a passage that would contradict the claim>"
            document, _ = DocumentRepository(session).get_or_create(
                source=other, text=body, depth=EvidenceDepth.FULL_TEXT
            )
            chunk = DocumentChunkRepository(session).add_chunks(document, [(0, len(body), body)])[0]
            session.flush()
            EvidenceRepository(session).create(
                claim=seeded_claim["claim"],
                chunk=chunk,
                relation=EvidenceRelation.CONTRADICTS,
                quoted_text=body,
                quote_start_char=0,
                quote_end_char=len(body),
            )
            VerificationResultRepository(session).create(
                claim=seeded_claim["claim"],
                verdict=VerificationVerdict.PARTIALLY_SUPPORTED,
                supporting_evidence=seeded_claim["evidence"],
            )

        claim = ClaimRepository(service_session).get_verification_trace(seeded_claim["claim"].id)
        assert claim is not None
        assert len(claim.evidence) == 2
        assert len(claim.supporting_evidence) == 1
        assert len(claim.contradicting_evidence) == 1
        assert claim.verification_result.supporting_evidence_id == seeded_claim["evidence"].id

    def test_the_evaluation_chain_loads_configuration_with_metrics(self, service_session):
        """Research run -> Configuration -> Metrics."""
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC, mode=ResearchMode.HYBRID))

        with unit_of_work() as session:
            stored = ResearchRunRepository(session).get_or_raise(run.id)
            EvaluationResultRepository(session).record(
                run=stored,
                metric_key=MetricKey.CLAIM_SUPPORT_RATE,
                value=0.75,
                numerator=3,
                denominator=4,
                unit="ratio",
                metrics_version="v1",
            )

        metrics = EvaluationResultRepository(service_session).list_for_run(run.id)
        assert len(metrics) == 1
        assert metrics[0].research_run.configuration.mode is ResearchMode.HYBRID
        assert (metrics[0].numerator, metrics[0].denominator) == (3, 4)

    def test_a_run_loads_its_sources_through_the_association(self, seeded_claim, service_session):
        run = ResearchRunRepository(service_session).get_with_relations(seeded_claim["run"].id)
        assert run is not None
        assert len(run.research_sources) == 1
        assert run.research_sources[0].source.id == seeded_claim["source"].id
        assert run.sources[0].id == seeded_claim["source"].id

    def test_the_run_summary_counts_what_exists(self, seeded_claim, service_session):
        summary = ResearchService().run_summary(seeded_claim["run"].id)
        assert summary["report_id"] == seeded_claim["report"].id
        assert summary["claim_count"] == 1
        assert summary["source_count"] == 1
        assert summary["conflict_count"] == 0

    def test_deleting_a_run_removes_its_graph_but_keeps_the_source(
        self, seeded_claim, service_session
    ):
        from app.models import Claim, Evidence, Report, Source

        with unit_of_work() as session:
            run = ResearchRunRepository(session).get_or_raise(seeded_claim["run"].id)
            session.delete(run)

        service_session.expire_all()
        assert service_session.query(Report).count() == 0
        assert service_session.query(Claim).count() == 0
        assert service_session.query(Evidence).count() == 0
        assert service_session.query(Source).count() == 1

    def test_metrics_and_call_log_totals_are_queryable(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))

        with unit_of_work() as session:
            stored = ResearchRunRepository(session).get_or_raise(run.id)
            log = LlmCallLogRepository(session)
            log.record(
                stage=PipelineStage.SYNTHESIS,
                model_name="<model under test>",
                run=stored,
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                cost_usd=0.0012,
            )
            log.record(
                stage=PipelineStage.VERIFICATION,
                model_name="<model under test>",
                run=stored,
                total_tokens=80,
                cost_usd=0.0007,
            )

        totals = LlmCallLogRepository(service_session).totals_for_run(run.id)
        assert totals["calls"] == 2
        assert totals["total_tokens"] == 230
        assert round(totals["cost_usd"], 6) == 0.0019


# --------------------------------------------------------------------------- #
# Transaction handling
# --------------------------------------------------------------------------- #


class TestTransactionHandling:
    def test_a_failure_inside_a_unit_of_work_rolls_everything_back(self, service_session):
        """Partial state is the failure mode this layer exists to prevent."""
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))

        before = ClaimRepository(service_session).count()
        with pytest.raises(RuntimeError), unit_of_work() as session:
            stored = ResearchRunRepository(session).get_or_raise(run.id)
            report = ReportRepository(session).create(
                run=stored, title="<title>", markdown="<body>"
            )
            ClaimRepository(session).create(report=report, position=0, text="<claim 0>")
            raise RuntimeError("something failed after writing")

        assert ClaimRepository(service_session).count() == before
        assert ReportRepository(service_session).count() == 0

    def test_a_successful_unit_of_work_commits_everything(self, service_session):
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))

        with unit_of_work() as session:
            stored = ResearchRunRepository(session).get_or_raise(run.id)
            report = ReportRepository(session).create(
                run=stored, title="<title>", markdown="<body>"
            )
            ClaimRepository(session).create(report=report, position=0, text="<claim 0>")

        assert ReportRepository(service_session).count() == 1
        assert ClaimRepository(service_session).count() == 1

    def test_a_nested_unit_of_work_joins_rather_than_committing_early(self, service_session):
        """So a service method can be one step of a larger operation.

        The inner block must not commit work the outer block may still abandon.
        """
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))

        with pytest.raises(RuntimeError), unit_of_work() as outer:
            stored = ResearchRunRepository(outer).get_or_raise(run.id)
            with unit_of_work(outer) as inner:
                assert inner is outer
                ReportRepository(inner).create(
                    run=stored, title="<title>", markdown="<body>"
                )
            raise RuntimeError("outer failed after the inner block succeeded")

        assert ReportRepository(service_session).count() == 0

    def test_repositories_do_not_commit_on_their_own(self, service_session, db_engine):
        """The rule this layer is built on, asserted directly."""
        from app.core.config import Settings

        session = service_session
        service = ResearchService()
        run = service.create_run(ResearchRequest(topic=TOPIC))

        stored = ResearchRunRepository(session).get_or_raise(run.id)
        ReportRepository(session).create(run=stored, title="<title>", markdown="<body>")
        # Flushed, not committed. A separate connection must not see it.
        with build_engine(Settings(DATABASE_URL=str(db_engine.url))).connect() as connection:
            visible = connection.execute(text("SELECT COUNT(*) FROM report")).scalar_one()
        session.rollback()
        assert visible == 0
        assert ReportRepository(session).count() == 0
