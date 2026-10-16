"""
The re-evaluation mode (final analysis plan §6, Option B) — its five obligations.

(a) zero new research runs, reports, or run-owned rows; (b) source files
byte-unchanged; (c) imported v1 values exactly equal the source rows; (d) a partial
failure is visible and recoverable, with judged cells reused on resume; (e) the
matched-primary rule — one shared denominator for both arms — agrees with
hand-computed values on a synthetic fixture.
"""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import EvaluationResult, Report, ResearchRun
from app.services.benchmark.relevance import RelevanceJudgement
from app.services.benchmark.runner import ExperimentRunner
from app.services.claims import ClaimExtractor
from app.services.orchestration import ReportFinalizer, ResearchOrchestrator
from app.services.research_service import ResearchService
from app.services.vectorstore import VectorStore
from app.services.verification import ClaimVerifier
from tests.unit.test_claim_extraction import FakeStructuredLLM, claim, extraction
from tests.unit.test_experiment import FakeJudgeLLM
from tests.unit.test_final_report import FINAL_BODY
from tests.unit.test_orchestrator import (
    CITED_REPORT,
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
from tests.unit.test_verification import SUPPORTED_CLAIM, WEB_QUOTE, FakeVerifierLLM, judge


class FlakyJudge(FakeJudgeLLM):
    """Raises a non-LLM infrastructure error for the first N external judgements."""

    def __init__(self, failures: int = 1):
        super().__init__()
        self.fail_remaining = failures

    async def generate_structured_output(self, prompt, schema, **kwargs):
        if schema is not RelevanceJudgement and self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise RuntimeError("synthetic judge infrastructure failure")
        return await super().generate_structured_output(prompt, schema, **kwargs)


class ExplodingSearch:
    """A pool provider that must never be consulted (cache-reuse proof)."""

    async def search(self, *args, **kwargs):  # pragma: no cover - failing is the point
        raise AssertionError("pool providers were consulted despite an existing cache")


@pytest.fixture
def harness(service_session, tmp_path):
    """A completed source experiment (A and D, one question) plus builders."""
    bench_dir = tmp_path / "benchdata"
    bench_dir.mkdir()
    for source in Path("../data/benchmarks").iterdir():
        if source.is_file():
            shutil.copy(source, bench_dir / source.name)
    test_settings = settings().model_copy(update={"BENCHMARK_DATA_DIR": str(bench_dir)})
    store_factory = lambda name: VectorStore(  # noqa: E731
        embedder=BagOfWordsEmbedder(),
        persist_dir=str(tmp_path / "chroma"),
        collection_name=name,
    )
    research = ResearchService(settings=test_settings)

    def build_runner(judge_llm) -> ExperimentRunner:
        return ExperimentRunner(
            settings=test_settings,
            research_service=research,
            orchestrator=ResearchOrchestrator(
                settings=test_settings,
                research_service=research,
                web_search=FakeWebSearch(web_result(web_candidate())),
                academic_search=FakeAcademicSearch(academic_result(academic_candidate())),
                store_factory=store_factory,
                llm=FakeLLM(CITED_REPORT),
            ),
            extractor=ClaimExtractor(
                settings=test_settings,
                research_service=research,
                llm=FakeStructuredLLM(extraction(claim(SUPPORTED_CLAIM, labels=["S1"]))),
            ),
            verifier=ClaimVerifier(
                settings=test_settings,
                research_service=research,
                store_factory=store_factory,
                llm=FakeVerifierLLM().on(
                    "caffeine intake", lambda p: judge(p, support_containing=WEB_QUOTE)
                ),
            ),
            finalizer=ReportFinalizer(
                settings=test_settings, research_service=research, llm=FakeLLM(FINAL_BODY)
            ),
            judge_llm=judge_llm,
            output_root=str(tmp_path / "results"),
            pool_store_factory=store_factory,
            pool_web_search=FakeWebSearch(web_result(web_candidate())),
            pool_academic_search=FakeAcademicSearch(academic_result(academic_candidate())),
        )

    return test_settings, build_runner, store_factory


async def make_source(build_runner) -> tuple[dict, str]:
    source_runner = build_runner(FakeJudgeLLM())
    payload = await source_runner.run_experiment(
        "development", arms=("model_only", "full_autoresearch_verified"), limit=1
    )
    # The runner records a cell failure as an error row rather than raising, so a
    # harness hiccup here would otherwise surface far away as missing_product_cell.
    errors = {
        (row["question_id"], row["arm"]): row["error"]
        for row in payload["rows"]
        if row.get("error")
    }
    assert not errors, f"source experiment cells failed in the harness: {errors}"
    return payload, payload["output_dir"]


class TestReevaluationObligations:
    async def test_a_zero_new_runs_reports_or_metric_rows(
        self, harness, service_session
    ):
        test_settings, build_runner, store_factory = harness
        source_payload, source_dir = await make_source(build_runner)

        def counts():
            return tuple(
                service_session.execute(select(func.count()).select_from(model)).scalar_one()
                for model in (ResearchRun, Report, EvaluationResult)
            )

        before = counts()
        reeval_runner = build_runner(FakeJudgeLLM())
        result = await reeval_runner.run_reevaluation(
            source_payload["experiment"]["benchmark_run_id"],
            source_dir,
            set_name="development",
        )
        service_session.expire_all()
        assert counts() == before  # §6a: nothing run-owned was created
        assert result["reevaluation"]["source_files_unchanged"] is True
        # The normal timestamped output path (no resume) still works and is new.
        assert result["output_dir"].endswith("-v2eval")
        assert Path(result["output_dir"]) != Path(source_dir)
        # The report names the set it actually re-evaluated — a development
        # artifact must never wear the frozen test set's label.
        report = (Path(result["output_dir"]) / "evaluation_report.md").read_text(
            encoding="utf-8"
        )
        assert report.startswith(
            "# Metrics-v2 re-evaluation of the stored development-set runs"
        )
        assert "frozen test" not in report.splitlines()[0]

    async def test_b_and_c_source_files_untouched_and_v1_values_exact(
        self, harness
    ):
        test_settings, build_runner, _ = harness
        source_payload, source_dir = await make_source(build_runner)
        hashes_before = {
            p.name: __import__("hashlib").sha256(p.read_bytes()).hexdigest()
            for p in Path(source_dir).iterdir() if p.is_file()
        }

        result = await build_runner(FakeJudgeLLM()).run_reevaluation(
            source_payload["experiment"]["benchmark_run_id"],
            source_dir,
            set_name="development",
        )

        hashes_after = {
            p.name: __import__("hashlib").sha256(p.read_bytes()).hexdigest()
            for p in Path(source_dir).iterdir() if p.is_file()
        }
        assert hashes_after == hashes_before  # §6b

        source_rows = {
            (row["question_id"], row["arm"]): row for row in source_payload["rows"]
        }
        for row in result["rows"]:  # §6c: field-level exact equality
            src = source_rows[(row["question_id"], row["arm"])]
            for field in ExperimentRunner.V1_IMPORT_FIELDS:
                assert row["v1"][field] == src.get(field), field

    async def test_pool_caches_are_reused_not_rebuilt(self, harness):
        test_settings, build_runner, store_factory = harness
        source_payload, source_dir = await make_source(build_runner)

        reeval_runner = build_runner(FakeJudgeLLM())
        reeval_runner._pool_web_search = ExplodingSearch()
        reeval_runner._pool_academic_search = ExplodingSearch()

        result = await reeval_runner.run_reevaluation(
            source_payload["experiment"]["benchmark_run_id"],
            source_dir,
            set_name="development",
        )
        hashes = {row["external_pool_hash"] for row in result["rows"]}
        assert len(hashes) == 1 and None not in hashes  # cache served every arm

    async def test_d_partial_failure_is_visible_and_resumable_with_reuse(self, harness):
        test_settings, build_runner, _ = harness
        source_payload, source_dir = await make_source(build_runner)
        benchmark_id = source_payload["experiment"]["benchmark_run_id"]

        flaky = FlakyJudge(failures=1)
        first_runner = build_runner(flaky)
        first = await first_runner.run_reevaluation(
            benchmark_id, source_dir, set_name="development"
        )
        statuses = {row["arm"]: row["status"] for row in first["rows"]}
        assert "judging_error" in statuses.values()  # visible, not hidden
        assert "judged" in statuses.values()
        judged_calls_after_first = flaky.calls_made

        healthy = FakeJudgeLLM()
        second = await build_runner(healthy).run_reevaluation(
            benchmark_id, source_dir,
            set_name="development", resume_dir=first["output_dir"],
        )
        assert all(row["status"] == "judged" for row in second["rows"])
        # The already-judged cell was reused: the healthy judge only worked the
        # previously failed cell (one cell's worth of claims, here one claim).
        assert healthy.calls_made <= judged_calls_after_first

    def test_e_matched_primary_hand_computed_with_one_shared_denominator(self):
        def judged_row(question_id, arm, supported, judged):
            return {
                "question_id": question_id, "arm": arm, "status": "judged",
                "external_judged": judged,
                "external_support_rate": round(supported / judged, 4),
                "external_contradiction_rate": 0.0,
                "external": {"verdicts": {"supported": supported,
                                          "not_enough_evidence": judged - supported}},
            }

        rows = [
            judged_row("q1", "model_only", 4, 10),
            judged_row("q1", "full_autoresearch_verified", 6, 10),
            judged_row("q2", "model_only", 9, 10),
            {"question_id": "q2", "arm": "full_autoresearch_verified",
             "status": "missing_product_cell"},
            judged_row("q3", "full_autoresearch_verified", 2, 4),
            {"question_id": "q3", "arm": "model_only", "status": "judged",
             "external_judged": 0, "external_support_rate": None,
             "external_contradiction_rate": None, "external": {"verdicts": {}}},
            judged_row("q4", "model_only", 2, 2),
            judged_row("q4", "full_autoresearch_verified", 1, 2),
        ]
        primary = ExperimentRunner.matched_primary(rows)

        # One shared denominator: only q1 and q4 qualify for BOTH arms.
        assert primary["matched_questions"] == ["q1", "q4"]
        assert primary["n"] == 2
        # macro: baseline mean(0.4, 1.0)=0.7; system mean(0.6, 0.5)=0.55
        assert primary["baseline"]["macro"] == 0.7
        assert primary["system"]["macro"] == 0.55
        assert primary["macro_difference_system_minus_baseline"] == -0.15
        # micro over the SAME matched set: baseline 6/12=0.5; system 7/12=0.5833
        assert primary["baseline"]["micro"] == 0.5
        assert primary["system"]["micro"] == 0.5833
        # exclusions carry per-arm reasons; unmatched counterparts are named too.
        reasons = {(e["question_id"], e["arm"]): e["reason"] for e in primary["exclusions"]}
        assert reasons[("q2", "full_autoresearch_verified")] == "missing product cell"
        assert reasons[("q3", "model_only")] == "zero judged claims"
        assert "unmatched" in reasons[("q2", "model_only")]
        assert "unmatched" in reasons[("q3", "full_autoresearch_verified")]

    async def test_d1_resume_into_the_source_directory_is_rejected_untouched(
        self, harness
    ):
        """D1 regression: resuming into the frozen source directory must fail
        closed before any write, leaving every source file byte-identical."""
        import hashlib

        test_settings, build_runner, _ = harness
        source_payload, source_dir = await make_source(build_runner)
        hashes_before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(source_dir).iterdir() if p.is_file()
        }

        with pytest.raises(ValueError, match="read-only inputs"):
            await build_runner(FakeJudgeLLM()).run_reevaluation(
                source_payload["experiment"]["benchmark_run_id"],
                source_dir,
                set_name="development",
                resume_dir=source_dir,
            )

        hashes_after = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(source_dir).iterdir() if p.is_file()
        }
        assert hashes_after == hashes_before

    async def test_d1_resume_into_an_arbitrary_existing_directory_is_rejected(
        self, harness, tmp_path
    ):
        """D1 regression: an existing directory without re-evaluation progress is
        not a resume target; its contents must survive untouched."""
        test_settings, build_runner, _ = harness
        source_payload, source_dir = await make_source(build_runner)
        bystander = tmp_path / "unrelated_results"
        bystander.mkdir()
        precious = bystander / "raw_results.json"
        precious.write_text('{"somebody": "elses results"}', encoding="utf-8")

        with pytest.raises(ValueError, match="no progress.jsonl"):
            await build_runner(FakeJudgeLLM()).run_reevaluation(
                source_payload["experiment"]["benchmark_run_id"],
                source_dir,
                set_name="development",
                resume_dir=str(bystander),
            )
        assert precious.read_text(encoding="utf-8") == '{"somebody": "elses results"}'

    async def test_d2_progress_from_another_benchmark_run_is_rejected(self, harness):
        """D2 regression: a resume directory whose progress header names a
        different benchmark run is rejected before any judgment is reused or any
        output is modified."""
        test_settings, build_runner, _ = harness
        payload_a, source_a = await make_source(build_runner)
        reeval_a = await build_runner(FakeJudgeLLM()).run_reevaluation(
            payload_a["experiment"]["benchmark_run_id"], source_a,
            set_name="development",
        )
        outputs_before = {
            p.name: p.read_bytes()
            for p in Path(reeval_a["output_dir"]).iterdir() if p.is_file()
        }

        payload_b, source_b = await make_source(build_runner)  # a second benchmark run
        assert (
            payload_b["experiment"]["benchmark_run_id"]
            != payload_a["experiment"]["benchmark_run_id"]
        )
        judge = FakeJudgeLLM()
        with pytest.raises(ValueError, match="different re-evaluation"):
            await build_runner(judge).run_reevaluation(
                payload_b["experiment"]["benchmark_run_id"], source_b,
                set_name="development",
                resume_dir=reeval_a["output_dir"],
            )
        assert judge.calls_made == 0  # rejected before any judgment
        outputs_after = {
            p.name: p.read_bytes()
            for p in Path(reeval_a["output_dir"]).iterdir() if p.is_file()
        }
        assert outputs_after == outputs_before  # nothing in the resume dir changed

    def test_same_second_output_dirs_never_collide(self, tmp_path):
        """Output directories are named to the second; two runs started within
        one second must get distinct directories, or the later run overwrites
        the earlier artifacts and a fresh re-evaluation reads a stranger's
        progress file (the intermittent d/d2 failures observed in this file)."""
        from app.services.benchmark.runner import _fresh_output_dir

        base = tmp_path / "development" / "20261016T120000Z-v2eval"
        first = _fresh_output_dir(base)
        second = _fresh_output_dir(base)
        third = _fresh_output_dir(base)
        assert first == base and first.is_dir()
        assert second == base.with_name(base.name + "-2") and second.is_dir()
        assert third == base.with_name(base.name + "-3") and third.is_dir()

    async def test_mismatched_benchmark_id_is_refused(self, harness):
        test_settings, build_runner, _ = harness
        _, source_dir = await make_source(build_runner)
        with pytest.raises(ValueError, match="not"):
            await build_runner(FakeJudgeLLM()).run_reevaluation(
                "00000000-0000-0000-0000-000000000000", source_dir,
                set_name="development",
            )
