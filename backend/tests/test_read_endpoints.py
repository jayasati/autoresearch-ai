"""
The stage-23 read endpoints: stored runs, reports, sources, evidence, benchmarks.

Every endpoint is read-only over persisted rows or committed artifact files, so
each test seeds the throwaway database with the integration builders (placeholder
values only, nothing that could pass for research output) and asserts the endpoint
reads back exactly what was stored.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.constants import (
    CitationStatus,
    ConflictType,
    ResearchMode,
    VerificationVerdict,
)
from app.main import create_app
from app.models import Conflict
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


@pytest.fixture
def seeded_run(service_session):
    """One completed, verified run: report, claims, verdicts, citations, sources,
    and a conflict — the full graph the read endpoints walk."""
    configuration = make_configuration()
    run = make_run(configuration)
    make_subquestion(run)
    source = make_source()
    link = link_source(run, source, label="S1")
    document = make_document(source)
    chunk = make_chunk(document)
    report = make_report(run)

    supported = make_claim(report, 0)
    verified_evidence = make_evidence(supported, chunk)
    make_verification(supported, VerificationVerdict.SUPPORTED, verified_evidence)
    make_citation(supported, source, marker="S1", status=CitationStatus.VALID, chunk=chunk)

    contradicted = make_claim(report, 1)
    conflicting_evidence = make_evidence(contradicted, chunk)
    make_verification(contradicted, VerificationVerdict.CONTRADICTED, conflicting_evidence)
    conflict = Conflict(
        research_run=run,
        claim_id=contradicted.id,
        evidence_a=verified_evidence,
        evidence_b=conflicting_evidence,
        conflict_type=ConflictType.NUMERIC_DISAGREEMENT,
        description="<conflict description under test>",
        detected_at=NOW,
    )

    service_session.add_all([run, link, document, conflict])
    service_session.commit()
    return run


class TestListRuns:
    def test_empty_database_lists_nothing(self, client, service_session):
        r = client.get("/api/v1/research")
        assert r.status_code == 200
        assert r.json() == []

    def test_lists_the_seeded_run_with_its_mode(self, client, seeded_run):
        body = client.get("/api/v1/research").json()
        assert len(body) == 1
        assert body[0]["id"] == str(seeded_run.id)
        assert body[0]["topic"] == "<topic under test>"
        assert body[0]["mode"] == ResearchMode.SEARCH_GROUNDED.value
        assert body[0]["status"] == "completed"


class TestGetRun:
    def test_returns_the_run_with_counts(self, client, seeded_run):
        r = client.get(f"/api/v1/research/{seeded_run.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["run"]["topic"] == "<topic under test>"
        assert body["claim_count"] == 2
        assert body["source_count"] == 1
        assert body["conflict_count"] == 1

    def test_unknown_run_is_a_404_envelope(self, client, service_session):
        r = client.get("/api/v1/research/00000000-0000-4000-8000-000000000000")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


class TestGetReport:
    def test_returns_draft_and_finalized_flag(self, client, seeded_run):
        body = client.get(f"/api/v1/research/{seeded_run.id}/report").json()
        assert body["markdown"] == "<report markdown under test>"
        assert body["final_markdown"] is None
        assert body["finalized"] is False

    def test_run_without_report_is_a_404(self, client, service_session):
        configuration = make_configuration()
        bare = make_run(configuration)
        service_session.add(bare)
        service_session.commit()
        r = client.get(f"/api/v1/research/{bare.id}/report")
        assert r.status_code == 404


class TestGetSources:
    def test_returns_the_link_facts_not_just_the_source(self, client, seeded_run):
        body = client.get(f"/api/v1/research/{seeded_run.id}/sources").json()
        assert len(body) == 1
        row = body[0]
        assert row["label"] == "S1"
        assert row["title"] == "<source title under test>"
        assert row["evidence_depth"] == "full_text"
        assert row["retriever"] == "<retriever under test>"

    def test_unlabelled_sources_are_included(self, client, service_session, seeded_run):
        extra = make_source()
        service_session.add(link_source(seeded_run, extra, label=None, rank=1))
        service_session.commit()
        body = client.get(f"/api/v1/research/{seeded_run.id}/sources").json()
        assert len(body) == 2
        assert {row["label"] for row in body} == {"S1", None}


class TestGetEvidence:
    def test_returns_claims_verdicts_citations_and_conflicts(self, client, seeded_run):
        body = client.get(f"/api/v1/research/{seeded_run.id}/evidence").json()
        assert body["topic"] == "<topic under test>"
        assert body["finalized"] is False
        verdicts = {claim["text"]: claim["verdict"] for claim in body["claims"]}
        assert verdicts == {
            "<claim 0 under test>": "supported",
            "<claim 1 under test>": "contradicted",
        }
        supported = next(c for c in body["claims"] if c["verdict"] == "supported")
        assert supported["citations"] == {"S1": "valid"}
        assert len(body["conflicts"]) == 1
        assert body["conflicts"][0]["description"] == "<conflict description under test>"
        assert [s["label"] for s in body["sources"]] == ["S1"]

    def test_run_without_report_is_a_404(self, client, service_session):
        configuration = make_configuration()
        bare = make_run(configuration)
        service_session.add(bare)
        service_session.commit()
        assert client.get(f"/api/v1/research/{bare.id}/evidence").status_code == 404


class TestBenchmarkArtifacts:
    @pytest.fixture
    def artifact_client(self, tmp_path):
        """A client whose settings point BENCHMARK_DATA_DIR at a seeded tmp dir."""
        results = tmp_path / "results" / "test"
        experiment_dir = results / "20260101T000000Z"
        experiment_dir.mkdir(parents=True)
        (experiment_dir / "raw_results.json").write_text(
            json.dumps(
                {
                    "experiment": {
                        "benchmark_run_id": "<benchmark id under test>",
                        "set_name": "test",
                        "arms": ["model_only", "full_autoresearch_verified"],
                        "secret_internal_field": "must not be served",
                    },
                    "rows": [],
                    "aggregates": {"model_only": {"items_completed": 1}},
                    "comparison": [
                        {"metric": "claim_support_rate", "baseline": 0.1, "autoresearch": 0.9}
                    ],
                }
            ),
            encoding="utf-8",
        )
        reeval_dir = results / "20260102T000000Z-v2eval"
        reeval_dir.mkdir()
        (reeval_dir / "raw_results.json").write_text(
            json.dumps(
                {
                    "reevaluation": {"benchmark_run_id": "<benchmark id under test>"},
                    "rows": [],
                    "aggregates_v2": {"model_only": {"external_support_rate_macro": 0.45}},
                    "primary_matched": {
                        "n": 6,
                        "macro_difference_system_minus_baseline": 0.21,
                    },
                }
            ),
            encoding="utf-8",
        )

        settings = Settings(
            APP_ENV="test", DEBUG=True, LOG_LEVEL="WARNING",
            BENCHMARK_DATA_DIR=str(tmp_path),
        )
        app = create_app(settings=settings)
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as c:
            yield c

    def test_serves_the_artifacts_verbatim(self, artifact_client):
        r = artifact_client.get("/api/v1/evaluation/benchmark")
        assert r.status_code == 200
        body = r.json()
        assert body["set_name"] == "test"
        assert body["source_dir"] == "20260101T000000Z"
        assert body["comparison"][0]["autoresearch"] == 0.9
        assert (
            body["external"]["primary_matched"]["macro_difference_system_minus_baseline"] == 0.21
        )
        # Only the declared header fields are served.
        assert "secret_internal_field" not in body["experiment"]

    def test_no_artifacts_is_a_404_not_an_empty_table(self, tmp_path):
        # An explicitly empty artifact dir: the default settings would point at
        # the repository's real artifacts, which exist.
        settings = Settings(
            APP_ENV="test", DEBUG=True, LOG_LEVEL="WARNING",
            BENCHMARK_DATA_DIR=str(tmp_path),
        )
        app = create_app(settings=settings)
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as empty_client:
            r = empty_client.get("/api/v1/evaluation/benchmark")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"
