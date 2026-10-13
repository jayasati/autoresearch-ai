"""
The controlled experiment: exact formulas, code-docs agreement, output writers, and
a full smoke run over the real component chain with scripted LLMs.

The formula tests pin hand-computed values — a formula test that recomputes the
formula would test nothing. The smoke test runs the real runner over the real
development set (limit 1) with the same fake infrastructure every other stage's
tests use, and asserts what the spec demands of the outputs: raw results stored,
three files written, per-arm aggregates, and no overall score anywhere.
"""

import csv
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import BenchmarkRun, EvaluationResult
from app.services.benchmark import metrics as formulas
from app.services.benchmark.relevance import RelevanceJudgement, RelevantPair
from app.services.benchmark.runner import ExperimentRunner
from app.services.claims import ClaimExtractor
from app.services.llm import StructuredLLMResponse, TokenUsage
from app.services.orchestration import ReportFinalizer, ResearchOrchestrator
from app.services.research_service import ResearchService
from app.services.vectorstore import VectorStore
from app.services.verification import ClaimVerifier
from tests.unit.test_claim_extraction import FakeStructuredLLM, claim, extraction
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


class TestFormulas:
    def test_claim_support_rate(self):
        verdicts = {"supported": 3, "partially_supported": 1, "unsupported": 2,
                    "contradicted": 1, "not_enough_evidence": 1}
        assert formulas.claim_support_rate(verdicts) == 0.5  # (3+1)/8
        assert formulas.claim_support_rate({}) is None  # undefined, not zero

    def test_unsupported_claim_rate(self):
        verdicts = {"supported": 3, "unsupported": 2, "contradicted": 1,
                    "not_enough_evidence": 2}
        assert formulas.unsupported_claim_rate(verdicts) == 0.375  # (2+1)/8

    def test_citation_precision_excludes_unvalidated(self):
        statuses = {"valid": 3, "misattributed": 1, "fabricated": 1, "unvalidated": 5}
        assert formulas.citation_precision(statuses) == 0.6  # 3/5, not 3/10
        assert formulas.citation_precision({"unvalidated": 4}) is None

    def test_citation_coverage(self):
        assert formulas.citation_coverage(6, 8) == 0.75
        assert formulas.citation_coverage(0, 0) is None

    def test_precision_at_k(self):
        assert formulas.precision_at_k([True, False, True], 3) == 0.6667
        # The denominator is what was actually inspected, not an unfillable K.
        assert formulas.precision_at_k([True], 4) == 1.0
        assert formulas.precision_at_k([], 4) is None

    def test_recall_at_k(self):
        assert formulas.recall_at_k(2, 3) == 0.6667
        assert formulas.recall_at_k(0, 0) is None

    def test_ndcg_at_k_hand_computed(self):
        # DCG = 1/log2(2) + 0 + 1/log2(4) = 1.5; IDCG = 1 + 1/log2(3) = 1.63093
        assert formulas.ndcg_at_k([True, False, True], 3) == pytest.approx(0.9197, abs=1e-4)
        assert formulas.ndcg_at_k([True, True], 2) == 1.0  # already ideal
        assert formulas.ndcg_at_k([False, False], 2) is None  # nothing to order well

    def test_conflict_detection_rate(self):
        assert formulas.conflict_detection_rate(2, 3) == 0.6667
        assert formulas.conflict_detection_rate(0, 0) is None

    def test_estimated_cost_hand_computed(self):
        # 1000 * 0.15/1M + 1000 * 0.60/1M = 0.00015 + 0.0006
        assert formulas.estimated_cost_usd("gpt-4o-mini-2024-07-18", 1000, 1000) == 0.00075
        assert formulas.estimated_cost_usd("some-unknown-model", 1000, 1000) is None

    def test_longest_prefix_wins_for_prices(self):
        """gpt-4o-mini must not be billed at gpt-4o prices via the shorter prefix."""
        mini = formulas.estimated_cost_usd("gpt-4o-mini-2024-07-18", 1_000_000, 0)
        full = formulas.estimated_cost_usd("gpt-4o-2024-08-06", 1_000_000, 0)
        assert mini == 0.15 and full == 2.5


class TestFormulasMatchTheDocumentation:
    """The spec requires the exact formulas in code AND documentation; this is the
    test that stops the two drifting."""

    FRAGMENTS = {
        formulas.claim_support_rate: (
            "claim_support_rate = (supported + partially_supported) / total_claims"
        ),
        formulas.unsupported_claim_rate: (
            "unsupported_claim_rate = (unsupported + contradicted) / total_claims"
        ),
        formulas.citation_precision: (
            "citation_precision = valid / (valid + misattributed + fabricated + broken)"
        ),
        formulas.citation_coverage: (
            "citation_coverage = claims_with_at_least_one_citation / claims_requiring_citation"
        ),
        formulas.precision_at_k: (
            "precision@K = |relevant chunks among the first K retrieved| / min(K, retrieved)"
        ),
        formulas.recall_at_k: (
            "recall@K = |expected evidence statements covered by the first K chunks| "
            "/ |expected statements|"
        ),
        formulas.ndcg_at_k: "nDCG@K = DCG@K / IDCG@K",
        formulas.conflict_detection_rate: "/ |items with expected conflicts|",
        formulas.estimated_cost_usd: (
            "prompt_tokens * input_price + completion_tokens * output_price"
        ),
    }

    @staticmethod
    def normalise(text: str) -> str:
        # Strip bold markers but keep single '*' (it is the multiplication sign).
        cleaned = text.replace("×", "*").replace("**", "").replace("≥", ">=")
        return re.sub(r"\s+", " ", cleaned)

    def test_every_formula_appears_in_code_and_docs(self):
        documentation = self.normalise(
            Path("../data/benchmarks/METRICS.md").read_text(encoding="utf-8")
        )
        for function, fragment in self.FRAGMENTS.items():
            fragment_normalised = self.normalise(fragment)
            assert fragment_normalised in self.normalise(function.__doc__), function.__name__
            assert fragment_normalised in documentation, function.__name__


class FakeJudgeLLM:
    """Marks the first passage relevant to the first statement, deterministically."""

    def __init__(self):
        self.calls_made = 0
        self.total_usage = TokenUsage.zero()

    async def generate_structured_output(self, prompt, schema, *, system=None, model=None,
                                         temperature=None, max_tokens=None):
        self.calls_made += 1
        judgement = RelevanceJudgement(pairs=[RelevantPair(passage_index=0, statement_index=0)])
        return StructuredLLMResponse[RelevanceJudgement](
            data=judgement, text=judgement.model_dump_json(), model="fake-judge",
            finish_reason="stop", usage=None, latency_seconds=0.01,
        )

    async def aclose(self):  # pragma: no cover
        pass


@pytest.fixture
def runner(service_session, tmp_path):
    test_settings = settings()
    store_factory = lambda name: VectorStore(  # noqa: E731
        embedder=BagOfWordsEmbedder(),
        persist_dir=str(tmp_path / "chroma"),
        collection_name=name,
    )
    research = ResearchService(settings=test_settings)
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
        judge_llm=FakeJudgeLLM(),
        output_root=str(tmp_path / "results"),
    )


class TestExperimentRunner:
    ARMS = ("model_only", "search_grounded", "full_autoresearch_verified")

    async def test_the_experiment_runs_measures_and_writes(self, runner, service_session):
        payload = await runner.run_experiment("development", arms=self.ARMS, limit=1)

        assert len(payload["rows"]) == len(self.ARMS)
        assert all(row["error"] is None for row in payload["rows"])
        by_arm = {row["arm"]: row for row in payload["rows"]}

        # The ungrounded baseline: claims exist, nothing supports them.
        assert by_arm["model_only"]["claim_support_rate"] == 0.0
        assert by_arm["model_only"]["retrieval_precision_at_k"] is None
        # The grounded arms: the scripted chain supports the claim and its citation.
        assert by_arm["search_grounded"]["claim_support_rate"] == 1.0
        assert by_arm["search_grounded"]["citation_precision"] == 1.0
        assert by_arm["search_grounded"]["retrieval_precision_at_k"] is not None
        # D pays for verification as product; C records it as evaluation overhead.
        assert (
            by_arm["full_autoresearch_verified"]["product_total_tokens"]
            > by_arm["search_grounded"]["product_total_tokens"]
        )
        assert by_arm["search_grounded"]["evaluation_tokens"] is not None

        output_dir = Path(payload["output_dir"])
        assert (output_dir / "results.json").exists()
        assert (output_dir / "results.csv").exists()
        assert (output_dir / "evaluation_summary.md").exists()

        with (output_dir / "results.csv").open(encoding="utf-8") as handle:
            csv_rows = list(csv.DictReader(handle))
        assert len(csv_rows) == len(self.ARMS)

        stored = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
        assert stored["experiment"]["set_fingerprint"] == payload["experiment"]["set_fingerprint"]
        assert stored["experiment"]["constants"]["metrics_version"] == formulas.METRICS_VERSION

    async def test_no_overall_score_exists_anywhere(self, runner):
        payload = await runner.run_experiment("development", arms=("model_only",), limit=1)
        # The tmp output path contains this test's own name; check the results only.
        serialised = json.dumps(
            {key: value for key, value in payload.items() if key != "output_dir"}
        ).lower()
        summary = Path(payload["output_dir"], "evaluation_summary.md").read_text(encoding="utf-8")
        for forbidden in ("overall_score", "composite_score", "final_score"):
            assert forbidden not in serialised
            assert forbidden not in summary.lower()
        assert "deliberately no overall score" in summary

    async def test_a_crashing_cell_is_recorded_and_does_not_sink_the_grid(self, runner):
        """Observed live: a Neon connection drop raised OperationalError and killed
        the 21 remaining cells. Any exception now becomes a recorded error row."""
        real_run = runner.orchestrator.run

        async def failing_run(request, benchmark_run_id=None):
            if request.mode.value == "hybrid":
                raise RuntimeError("synthetic infrastructure failure")
            return await real_run(request, benchmark_run_id)

        runner.orchestrator.run = failing_run
        payload = await runner.run_experiment(
            "development", arms=("hybrid", "search_grounded"), limit=1
        )

        by_arm = {row["arm"]: row for row in payload["rows"]}
        assert by_arm["hybrid"]["error"]["code"] == "internal_error"
        assert "RuntimeError" in by_arm["hybrid"]["error"]["message"]
        assert by_arm["search_grounded"]["error"] is None  # the grid continued
        assert payload["aggregates"]["hybrid"]["items_failed"] == 1

    def test_comparison_math_and_meaningfulness(self):
        aggregates = {
            "model_only": {
                "claim_support_rate": {"mean": 0.0, "n": 8},
                "citation_precision": {"mean": None, "n": 0},
                "product_cost_usd": {"mean": 0.0003, "n": 8},
                "unsupported_claim_rate": {"mean": 0.0, "n": 8},
                "citation_coverage": {"mean": 0.0, "n": 8},
                "retrieval_precision_at_k": {"mean": None, "n": 0},
                "retrieval_recall_at_k": {"mean": None, "n": 0},
                "retrieval_ndcg_at_k": {"mean": None, "n": 0},
                "product_latency_seconds": {"mean": 8.0, "n": 8},
                "product_total_tokens": {"mean": 700, "n": 8},
                "failure_rate": 0.0,
            },
            "full_autoresearch_verified": {
                "claim_support_rate": {"mean": 0.6, "n": 8},
                "citation_precision": {"mean": 0.5, "n": 8},
                "product_cost_usd": {"mean": 0.006, "n": 8},
                "unsupported_claim_rate": {"mean": 0.08, "n": 8},
                "citation_coverage": {"mean": 0.6, "n": 8},
                "retrieval_precision_at_k": {"mean": 0.5, "n": 7},
                "retrieval_recall_at_k": {"mean": 0.85, "n": 7},
                "retrieval_ndcg_at_k": {"mean": 0.8, "n": 7},
                "product_latency_seconds": {"mean": 57.0, "n": 8},
                "product_total_tokens": {"mean": 29000, "n": 8},
                "failure_rate": 0.0,
            },
        }
        comparison = {
            entry["metric"]: entry
            for entry in ExperimentRunner.build_comparison(aggregates)
        }

        support = comparison["claim_support_rate"]
        assert support["absolute_difference"] == 0.6
        assert support["relative_difference"] is None  # zero baseline: not meaningful
        assert support["direction"] == "improved"

        cost = comparison["product_cost_usd"]
        assert cost["absolute_difference"] == 0.0057
        assert cost["relative_difference"] == 19.0  # (0.006-0.0003)/0.0003
        assert cost["direction"] == "worse"  # lower is better for cost

        precision = comparison["citation_precision"]
        assert precision["baseline"] is None  # undefined for the baseline
        assert precision["absolute_difference"] is None
        assert precision["direction"] is None

        hallucination = comparison["unsupported_claim_rate"]
        assert hallucination["direction"] == "worse"  # 0.08 over a 0.0 baseline

        assert comparison["failure_rate"]["direction"] == "unchanged"

    def test_structural_baseline_values_carry_their_note(self):
        """Audit CD-2: a definition must not read as a measurement."""
        aggregates = {
            "model_only": {
                "claim_support_rate": {"mean": 0.0, "n": 8},
                "unsupported_claim_rate": {"mean": 0.0, "n": 8},
                "citation_coverage": {"mean": 0.0, "n": 8},
                "citation_precision": {"mean": None, "n": 0},
                "retrieval_precision_at_k": {"mean": None, "n": 0},
                "retrieval_recall_at_k": {"mean": None, "n": 0},
                "retrieval_ndcg_at_k": {"mean": None, "n": 0},
                "product_latency_seconds": {"mean": 9.0, "n": 8},
                "product_total_tokens": {"mean": 700, "n": 8},
                "product_cost_usd": {"mean": 0.0003, "n": 8},
                "failure_rate": 0.0,
            },
            "full_autoresearch_verified": {
                key: {"mean": 0.5, "n": 8}
                for key in ExperimentRunner.HIGHER_IS_BETTER
                if key != "failure_rate"
            }
            | {"failure_rate": 0.0},
        }
        comparison = {
            entry["metric"]: entry
            for entry in ExperimentRunner.build_comparison(aggregates)
        }
        assert "structural" in comparison["claim_support_rate"]["baseline_note"]
        assert "structural" in comparison["unsupported_claim_rate"]["baseline_note"]
        assert comparison["product_cost_usd"]["baseline_note"] is None

    def test_matched_comparison_covers_only_the_intersection(self):
        """Audit CD-1: when arms failed different items, the companion comparison
        is computed over the items both completed, naming the missing ones."""
        def row(question_id, arm, error=None, support=0.5):
            return {
                "question_id": question_id,
                "arm": arm,
                "error": error,
                "claim_support_rate": support,
                "conflict_expected": False,
                "conflicts_detected": 0,
            }

        rows = [
            row("q1", "model_only"),
            row("q2", "model_only", error={"code": "internal_error", "message": "x"}),
            row("q3", "model_only"),
            row("q1", "full_autoresearch_verified", support=0.9),
            row("q2", "full_autoresearch_verified", support=0.9),
            row("q3", "full_autoresearch_verified",
                error={"code": "internal_error", "message": "x"}),
        ]
        matched = ExperimentRunner._matched_comparison(rows, ExperimentRunner._aggregate)

        assert matched is not None
        assert matched["matched_items"] == ["q1"]
        assert matched["baseline_missing"] == ["q2"]
        assert matched["autoresearch_missing"] == ["q3"]
        support = next(
            e for e in matched["comparison"] if e["metric"] == "claim_support_rate"
        )
        assert support["baseline"] == 0.5 and support["autoresearch"] == 0.9

    def test_matched_comparison_is_omitted_when_subsets_are_identical(self):
        rows = [
            {"question_id": "q1", "arm": arm, "error": None, "claim_support_rate": 0.5,
             "conflict_expected": False, "conflicts_detected": 0}
            for arm in ("model_only", "full_autoresearch_verified")
        ]
        assert ExperimentRunner._matched_comparison(rows, ExperimentRunner._aggregate) is None

    def test_aggregation_method_is_documented(self):
        """Audit CD-3: macro-averaging must be stated where the formulas live."""
        documentation = Path("../data/benchmarks/METRICS.md").read_text(encoding="utf-8")
        assert "macro-average" in documentation
        assert "Structural values" in documentation

    async def test_the_test_set_writes_the_final_file_names(self, runner, service_session):
        """The final evaluation's outputs carry the names the write-up names."""
        payload = await runner.run_experiment("test", arms=("model_only",), limit=1)
        output_dir = Path(payload["output_dir"])
        assert (output_dir / "raw_results.json").exists()
        assert (output_dir / "raw_results.csv").exists()
        assert (output_dir / "evaluation_report.md").exists()
        assert not (output_dir / "results.json").exists()

    async def test_raw_results_land_in_the_database(self, runner, service_session):
        await runner.run_experiment("development", arms=("search_grounded",), limit=1)

        [benchmark] = service_session.execute(select(BenchmarkRun)).scalars().all()
        assert benchmark.topic_set == "development"
        assert benchmark.completed_at is not None
        metrics_rows = service_session.execute(select(EvaluationResult)).scalars().all()
        assert metrics_rows, "per-run metric rows must be stored"
        assert {row.metrics_version for row in metrics_rows} == {formulas.METRICS_VERSION}
