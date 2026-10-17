"""report-quality-v1 persistence (stage 29, S2): schema invariants and the store.

Built with the placeholder builders on a throwaway SQLite database — no real
research data, no provider or model call.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.constants import (
    CitationOutcome,
    ClaimStatus,
    EstablishedBy,
    NotEvaluatedReason,
    PipelineVersion,
    PoolStatus,
    ReportQualityStatus,
    ResearchMode,
    VerificationVerdict,
)
from app.core.exceptions import ConflictError, ValidationError
from app.db.base import utcnow
from app.models import (
    CitationCheck,
    ClaimAudit,
    FinalClaimCheck,
    RunConfiguration,
)
from app.schemas.research import ResearchRequest
from app.services.quality.store import (
    CitationCheckRecord,
    ClaimAuditRecord,
    FinalClaimCheckRecord,
    QualityStore,
    quality_configuration_values,
)
from app.services.research_service import ResearchService
from tests.integration.builders import (
    link_source,
    make_chunk,
    make_claim,
    make_configuration,
    make_document,
    make_evidence,
    make_report,
    make_run,
    make_source,
    make_verification,
)

#: Fingerprints computed at e8d8fdb, before S2 touched `compute_fingerprint`.
LEGACY_MODEL_ONLY = {
    "mode": "model_only",
    "model": "gpt-4o-mini",
    "temperature": 0.0,
    "max_subquestions": 6,
    "max_sources": 25,
}
LEGACY_GROUNDED = {
    **LEGACY_MODEL_ONLY,
    "mode": "search_grounded",
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "chunk_size": 900,
    "chunk_overlap": 150,
    "top_k": 6,
}
PINNED = {
    "model_only": "3858a5711a397cbdfc440cbe1345fb86a3185c2643d9718cb7d5de93cce06a6f",
    "search_grounded": "9373802bb478ed271d28a2b54e26f4ad5d6314ce38b70c87cedf7bf231f3e08d",
}
QUOTE = "coffee intake was associated with lower risk"
CHUNK_TEXT = f"<chunk under test> {QUOTE} <end>"


class TestFingerprints:
    def test_legacy_fingerprints_are_byte_identical_to_before_s2(self):
        assert RunConfiguration.compute_fingerprint(LEGACY_MODEL_ONLY) == PINNED["model_only"]
        assert RunConfiguration.compute_fingerprint(LEGACY_GROUNDED) == PINNED["search_grounded"]

    def test_explicit_nulls_do_not_change_a_legacy_fingerprint(self):
        values = {**LEGACY_GROUNDED, "pipeline_version": None, "options": None}
        assert RunConfiguration.compute_fingerprint(values) == PINNED["search_grounded"]

    def test_the_pipeline_and_its_options_are_part_of_the_identity(self):
        a = {
            **LEGACY_GROUNDED,
            "pipeline_version": PipelineVersion.REPORT_QUALITY_V1,
            "options": {"x": 1, "y": 2},
        }
        reordered = {**a, "options": {"y": 2, "x": 1}}
        changed = {**a, "options": {"x": 1, "y": 3}}
        fingerprint = RunConfiguration.compute_fingerprint
        assert fingerprint(a) != PINNED["search_grounded"]
        assert fingerprint(a) == fingerprint(reordered)
        assert fingerprint(a) != fingerprint(changed)

    def test_legacy_request_values_carry_no_pipeline_fields(self, clean_env):
        service = ResearchService(settings=Settings(_env_file=None))
        for mode in ResearchMode:
            values = service.configuration_values_for(
                ResearchRequest(topic="<topic under test>", mode=mode)
            )
            assert "pipeline_version" not in values and "options" not in values

    def test_quality_values_snapshot_every_knob_but_the_gate(self, clean_env):
        settings = Settings(_env_file=None)
        values = quality_configuration_values(LEGACY_GROUNDED, settings)
        assert values["pipeline_version"] is PipelineVersion.REPORT_QUALITY_V1
        knobs = {n for n in type(settings).model_fields if n.startswith("REPORT_QUALITY_")}
        assert set(values["options"]) == (knobs - {"REPORT_QUALITY_ENABLED"}) | {
            "EVIDENCE_SELECTION"
        }


@pytest.fixture
def graph(db):
    """A run with a report, one claim, two chunks; claim verified with a quote on chunk A."""
    run = make_run(make_configuration())
    source = make_source()
    link_source(run, source)
    document = make_document(source)
    chunk_a = make_chunk(document, 0, CHUNK_TEXT)
    chunk_b = make_chunk(document, 1, "<other chunk under test> unrelated words")
    report = make_report(run)
    claim = make_claim(report)
    evidence = make_evidence(claim, chunk_a, quoted_text=QUOTE)
    verification = make_verification(
        claim,
        VerificationVerdict.SUPPORTED,
        evidence,
        verifier_model="<model>",
        prompt_version="verification-v2",
    )
    db.add_all([run, source, document, chunk_a, chunk_b, report, claim, evidence, verification])
    db.commit()
    return {
        "run": run,
        "report": report,
        "claim": claim,
        "a": chunk_a,
        "b": chunk_b,
        "store": QualityStore(session=db),
        "db": db,
    }


class TestSchemaChecks:
    """The database refuses what the store would refuse, where SQL can say it."""

    def fails(self, db, row):
        db.add(row)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_legacy_is_null_never_the_string(self, db):
        self.fails(
            db,
            RunConfiguration(
                **LEGACY_MODEL_ONLY,
                pipeline_version=PipelineVersion.LEGACY,
                fingerprint="f" * 64,
                created_at=utcnow(),
            ),
        )

    def test_options_need_a_pipeline(self, db):
        self.fails(
            db,
            RunConfiguration(
                **LEGACY_MODEL_ONLY, options={"a": 1}, fingerprint="e" * 64, created_at=utcnow()
            ),
        )

    def test_not_evaluated_needs_a_reason(self, graph):
        self.fails(
            graph["db"],
            ClaimAudit(claim_id=graph["claim"].id, pool_status=PoolStatus.NOT_EVALUATED),
        )

    def test_a_relation_needs_a_quote(self, graph):
        self.fails(
            graph["db"],
            CitationCheck(
                claim_id=graph["claim"].id,
                passage_alias="E1",
                cited_chunk_id=graph["a"].id,
                outcome=CitationOutcome.SUPPORTS,
                established_by=EstablishedBy.CITATION_CHECK,
                model="<model>",
                procedure_version="citation-check-v1",
                created_at=utcnow(),
            ),
        )

    def test_support_for_a_rewritten_sentence_needs_a_real_verification(self, graph):
        self.fails(
            graph["db"],
            FinalClaimCheck(
                report_id=graph["report"].id,
                section_index=0,
                sentence_index=0,
                text="<s>",
                change_kinds=["number"],
                cited_chunk_ids=[],
                supported_chunk_ids=[],
                pool_status=PoolStatus.SUPPORTED,
                claim_status=ClaimStatus.SUPPORTED_BY_CITED_SOURCE,
                model=None,
                prompt_version=None,
                created_at=utcnow(),
            ),
        )


class TestAudit:
    def test_lifecycle_merges_payload_and_freezes_when_terminal(self, graph):
        store, report = graph["store"], graph["report"]
        store.start_audit(report.id)
        with pytest.raises(ConflictError):
            store.start_audit(report.id)
        store.update_audit(report.id, stage="draft", payload={"a": 1})
        store.update_audit(report.id, payload={"b": 2}, quality_status=ReportQualityStatus.DEGRADED)
        record = store.quality_record(graph["run"].id)
        assert record["audit"] == {
            "quality_status": "degraded",
            "stage": "draft",
            "payload": {"a": 1, "b": 2},
        }
        with pytest.raises(ConflictError, match="final"):
            store.update_audit(report.id, stage="again")


class TestClaimAudit:
    def test_must_agree_with_the_stored_verdict(self, graph):
        store, claim = graph["store"], graph["claim"]
        with pytest.raises(ValidationError, match="stored verification verdict"):
            store.record_claim_audit(ClaimAuditRecord(claim.id, PoolStatus.CONTRADICTED))
        store.record_claim_audit(
            ClaimAuditRecord(
                claim.id, PoolStatus.SUPPORTED, claim_status=ClaimStatus.SUPPORTED_BY_CITED_SOURCE
            )
        )
        store.record_claim_audit(
            ClaimAuditRecord(
                claim.id, PoolStatus.SUPPORTED, claim_status=ClaimStatus.CITATION_NOT_CONFIRMED
            )
        )
        audits = store.quality_record(graph["run"].id)["claim_audits"]
        assert [a["claim_status"] for a in audits] == ["supported_citation_not_confirmed"]

    def test_an_unverified_claim_can_only_be_not_evaluated(self, graph, db):
        other = make_claim(graph["report"], position=1)
        db.add(other)
        db.commit()
        store = graph["store"]
        with pytest.raises(ValidationError, match="only be audited as not_evaluated"):
            store.record_claim_audit(ClaimAuditRecord(other.id, PoolStatus.SUPPORTED))
        store.record_claim_audit(
            ClaimAuditRecord(other.id, PoolStatus.NOT_EVALUATED, NotEvaluatedReason.CLAIM_CAP)
        )


class TestCitationCheck:
    def record(self, graph, **overrides):
        values = dict(
            claim_id=graph["claim"].id,
            passage_alias="E1",
            cited_chunk_id=graph["a"].id,
            outcome=CitationOutcome.SUPPORTS,
            procedure_version="citation-check-v1",
            established_by=EstablishedBy.CITATION_CHECK,
            quoted_text=QUOTE,
            model="<model>",
        )
        values.update(overrides)
        return CitationCheckRecord(**values)

    def test_a_quote_must_be_verbatim_in_the_cited_chunk(self, graph):
        with pytest.raises(ValidationError, match="verbatim"):
            graph["store"].record_citation_check(self.record(graph, quoted_text="paraphrased"))
        with pytest.raises(ValidationError, match="verbatim"):
            graph["store"].record_citation_check(self.record(graph, cited_chunk_id=graph["b"].id))
        graph["store"].record_citation_check(self.record(graph))
        with pytest.raises(ConflictError):
            graph["store"].record_citation_check(self.record(graph))

    def test_the_pool_shortcut_must_reuse_stored_evidence_on_that_exact_chunk(self, graph, db):
        store = graph["store"]
        other = make_chunk(graph["a"].document, 2, f"<third chunk> {QUOTE}")
        db.add(other)
        db.commit()
        with pytest.raises(ValidationError, match="stored evidence row"):
            store.record_citation_check(
                self.record(
                    graph,
                    passage_alias="E3",
                    cited_chunk_id=other.id,
                    established_by=EstablishedBy.POOL_VERIFICATION,
                    model=None,
                )
            )
        store.record_citation_check(
            self.record(graph, established_by=EstablishedBy.POOL_VERIFICATION, model=None)
        )

    def test_invalid_references_and_unevaluated_passages_are_recorded(self, graph):
        store = graph["store"]
        store.record_citation_check(
            self.record(
                graph,
                passage_alias="E99",
                cited_chunk_id=None,
                outcome=CitationOutcome.INVALID_REFERENCE,
                established_by=None,
                quoted_text=None,
                model=None,
            )
        )
        store.record_citation_check(
            self.record(
                graph,
                passage_alias="E2",
                cited_chunk_id=graph["b"].id,
                outcome=CitationOutcome.NOT_EVALUATED,
                established_by=None,
                quoted_text=None,
                not_evaluated_reason=NotEvaluatedReason.UNALIGNED,
            )
        )
        outcomes = {
            c["passage_alias"]: c["outcome"]
            for c in store.quality_record(graph["run"].id)["citation_checks"]
        }
        assert outcomes == {"E2": "not_evaluated", "E99": "invalid_reference"}


class TestFinalClaimCheck:
    """Safety invariant (approved): a sentence flagged for re-verification is never
    supported until a re-verification has actually succeeded on what it cites."""

    def awaiting(self, graph):
        return FinalClaimCheckRecord(
            section_index=0,
            sentence_index=0,
            text="<rewritten sentence>",
            change_kinds=["number"],
            cited_chunk_ids=[graph["a"].id],
            pool_status=PoolStatus.NOT_EVALUATED,
            claim_status=ClaimStatus.NOT_EVALUATED,
            not_evaluated_reason=NotEvaluatedReason.AWAITING_REVERIFICATION,
            derived_from_claim_id=graph["claim"].id,
        )

    def result(self, graph, supported):
        return FinalClaimCheckRecord(
            section_index=0,
            sentence_index=0,
            text="<rewritten sentence>",
            change_kinds=["number"],
            cited_chunk_ids=[graph["a"].id],
            pool_status=PoolStatus.SUPPORTED,
            claim_status=ClaimStatus.SUPPORTED_BY_CITED_SOURCE,
            supported_chunk_ids=supported,
            model="<model>",
            prompt_version="verification-v2",
            derived_from_claim_id=graph["claim"].id,
        )

    def test_support_through_a_chunk_the_sentence_does_not_cite_is_refused(self, graph):
        store, report = graph["store"], graph["report"]
        store.record_final_claim_check(report.id, self.awaiting(graph))
        with pytest.raises(ValidationError, match="supporting chunk is one it cites"):
            store.record_final_claim_check(report.id, self.result(graph, [graph["b"].id]))
        status = store.quality_record(graph["run"].id)["final_claim_checks"][0]
        assert (status["claim_status"], status["not_evaluated_reason"]) == (
            "not_evaluated",
            "awaiting_reverification",
        )

    def test_the_awaiting_row_is_replaced_once_by_a_real_result(self, graph):
        store, report = graph["store"], graph["report"]
        store.record_final_claim_check(report.id, self.awaiting(graph))
        store.record_final_claim_check(report.id, self.result(graph, [graph["a"].id]))
        [check] = store.quality_record(graph["run"].id)["final_claim_checks"]
        assert check["claim_status"] == "supported_by_cited_source"
        with pytest.raises(ConflictError, match="already has a re-verified result"):
            store.record_final_claim_check(report.id, self.result(graph, [graph["a"].id]))

    def test_a_change_must_be_named_and_chunks_must_exist(self, graph):
        store, report = graph["store"], graph["report"]
        record = self.awaiting(graph)
        with pytest.raises(ValidationError, match="why the sentence changed"):
            store.record_final_claim_check(
                report.id, FinalClaimCheckRecord(**{**record.__dict__, "change_kinds": []})
            )
        from app.core.exceptions import NotFoundError

        with pytest.raises(NotFoundError, match="Unknown chunk"):
            store.record_final_claim_check(
                report.id,
                FinalClaimCheckRecord(**{**record.__dict__, "cited_chunk_ids": [uuid.uuid4()]}),
            )


def test_a_legacy_run_reads_back_empty(graph):
    record = graph["store"].quality_record(graph["run"].id)
    assert record["pipeline_version"] is None and record["options"] is None
    assert record["audit"] is None
    assert record["claim_audits"] == record["citation_checks"] == record["final_claim_checks"] == []
