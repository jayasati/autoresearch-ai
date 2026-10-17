"""The local report-quality-v1 runner, `scripts/run_quality_pipeline.py` (stage 34).

Every test runs the real script — arguments, isolated workspace, real migrations on
its throwaway SQLite file, artifact writing — with fake providers injected through
`pipeline_factory`. The working directory is moved to an empty folder so no `.env`
is read: provider configuration is exactly what each test sets. No network, no
tokens.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import Settings, get_settings
from app.services.quality.pipeline import ReportQualityPipeline
from app.services.research_service import ResearchService
from tests.unit.test_quality_finalization import FinalLLM, exact_support
from tests.unit.test_quality_pipeline import TOPIC
from tests.unit.test_quality_planner_evidence import EMBED
from tests.unit.test_quality_verification import (
    FakeStore,
    PerQuerySearch,
    alpha_first,
    bravo_first,
    judge,
    providers,
)

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "run_quality_pipeline", ROOT / "scripts" / "run_quality_pipeline.py"
)
runner = importlib.util.module_from_spec(_spec)
sys.modules["run_quality_pipeline"] = runner
_spec.loader.exec_module(runner)

OPENAI_SECRET = "sk-runner-secret-0123456789"
TAVILY_SECRET = "tvly-runner-secret-0123456789"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """No .env, no provider credentials, no inherited database settings."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    for key in (
        "OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "TAVILY_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        *runner._OVERRIDDEN,
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    return tmp_path


@pytest.fixture
def configured(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_SECRET)
    monkeypatch.setenv("TAVILY_API_KEY", TAVILY_SECRET)
    return env


def fake_pipeline(llm, *, order=alpha_first, web=None, academic=None):
    def factory(settings: Settings) -> ReportQualityPipeline:
        default_web, default_academic = providers()
        store = FakeStore(order)
        return ReportQualityPipeline(
            settings=settings,
            research_service=ResearchService(settings=settings),
            llm=llm,
            web_search=web or default_web,
            academic_search=academic or default_academic,
            embedder=EMBED,
            store_factory=lambda name: store,
        )

    return factory


def never_called(settings):
    raise AssertionError("the pipeline must not be built")


def run(tmp_path, *args, factory=never_called):
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir(exist_ok=True)
    code = runner.main(
        [TOPIC, "--out", str(tmp_path / "artifacts"), *args],
        pipeline_factory=factory,
        workspace_root=workspaces,
    )
    return code, workspaces


def artifact(tmp_path) -> tuple[Path, dict]:
    [directory] = (tmp_path / "artifacts").iterdir()
    return directory, json.loads((directory / "run.json").read_text(encoding="utf-8"))


class TestArguments:
    def test_defaults_are_quick_preflight(self):
        args = runner.parse_args(["  Does   coffee affect health?  "])
        assert args.question == "Does coffee affect health?"
        assert args.depth == "quick" and args.execute is False
        assert args.out == runner.ARTIFACTS and args.keep_workspace is False

    def test_depth_and_execute(self):
        args = runner.parse_args([TOPIC, "--depth", "deep", "--execute"])
        assert args.depth == "deep" and args.execute is True

    @pytest.mark.parametrize("argv", [[TOPIC, "--depth", "huge"], ["hi"], []])
    def test_invalid_arguments_are_refused(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            runner.parse_args(argv)
        assert exc.value.code == runner.EXIT_REFUSED


class TestPreflight:
    def test_a_dry_run_calls_nothing_writes_nothing_and_cleans_up(self, configured, capsys):
        code, workspaces = run(configured)
        out = capsys.readouterr().out
        assert code == runner.EXIT_COMPLETE
        assert "no provider was called" in out and "Ready." in out
        assert "60 model attempts, 120000 tokens, 30 verified claims, 600 s deadline" in out
        assert "$0.018-$0.072 at the token cap" in out  # gpt-4o-mini list prices
        assert not (configured / "artifacts").exists()  # no research result
        assert list(workspaces.iterdir()) == []  # workspace removed
        # the process environment is restored
        assert get_settings().REPORT_QUALITY_ENABLED is False
        assert "sqlite" not in get_settings().DATABASE_URL

    def test_the_migration_is_applied_to_the_workspace_database(self, configured):
        code, workspaces = run(configured, "--keep-workspace")
        assert code == runner.EXIT_COMPLETE
        [workspace] = workspaces.iterdir()
        config = Config()
        config.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
        head = ScriptDirectory.from_config(config).get_current_head()
        with sqlite3.connect(workspace / "run.db") as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        assert {"report_quality_audit", "claim_audit", "citation_check"} <= tables
        assert (workspace / runner.MARKER).is_file()

    def test_missing_provider_configuration_is_reported(self, env, capsys):
        code, _ = run(env)
        out = capsys.readouterr().out
        assert code == runner.EXIT_REFUSED
        assert "Not ready : OPENAI_API_KEY" in out and "Not ready : TAVILY_API_KEY" in out

    def test_execute_without_configuration_is_refused_before_any_call(self, env, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-replace-me")  # the template placeholder
        monkeypatch.setenv("TAVILY_API_KEY", TAVILY_SECRET)
        code, workspaces = run(env, "--execute")
        assert code == runner.EXIT_REFUSED
        assert not (env / "artifacts").exists()
        assert list(workspaces.iterdir()) == []

    def test_a_budget_below_the_minimum_is_refused(self, configured, monkeypatch, capsys):
        monkeypatch.setenv("REPORT_QUALITY_MAX_LLM_ATTEMPTS_QUICK", "3")
        code, _ = run(configured, "--execute")
        out = capsys.readouterr().out
        assert code == runner.EXIT_REFUSED
        assert "3 model attempts" in out and "budget below the pipeline minimum" in out
        assert not (configured / "artifacts").exists()


class TestIsolation:
    def test_a_postgres_or_outside_database_is_refused(self, tmp_path):
        with pytest.raises(runner.UnsafeWorkspace):
            runner.check_isolated(
                Settings(DATABASE_URL="postgresql://u:p@db.example/x", REPORT_QUALITY_ENABLED=True),
                tmp_path,
            )
        elsewhere = tmp_path.parent / "elsewhere.db"
        with pytest.raises(runner.UnsafeWorkspace):
            runner.check_isolated(
                Settings(
                    DATABASE_URL=f"sqlite:///{elsewhere.as_posix()}",
                    CHROMA_PERSIST_DIR=str(tmp_path / "v"),
                    REPORT_QUALITY_ENABLED=True,
                ),
                tmp_path,
            )

    def test_only_a_marked_workspace_is_deleted(self, tmp_path):
        unmarked = tmp_path / "user-data"
        unmarked.mkdir()
        (unmarked / "keep.txt").write_text("precious")
        assert runner.remove_workspace(unmarked) is False
        assert (unmarked / "keep.txt").read_text() == "precious"


class TestExecute:
    def test_a_complete_run_prints_and_saves_everything_without_secrets(self, configured, capsys):
        code, workspaces = run(
            configured, "--execute", factory=fake_pipeline(FinalLLM(pool=exact_support))
        )
        out = capsys.readouterr().out
        assert code == runner.EXIT_COMPLETE
        assert "Outcome   : complete" in out and "## References" in out
        assert "Citation-specific support" in out and "Unresolved / contested claims: 0" in out
        assert "measured" in out and "Latency" in out
        assert list(workspaces.iterdir()) == []

        directory, record = artifact(configured)
        assert record["question"] == TOPIC and record["exit_code"] == 0
        assert record["outcome"] == {"status": "complete", "reasons": []}
        assert record["run_status"] == "completed"
        assert record["configuration"]["prompt_versions"]["citation_check"] == "citation-check-v1"
        assert record["configuration"]["report_quality"]["REPORT_QUALITY_ENABLED"] is True
        assert record["final_markdown"] == (directory / "final_report.md").read_text("utf-8")
        assert all(c["claim_status"] == "supported_by_cited_source" for c in record["claims"])
        assert all("supports" in c["citation_outcomes"].values() for c in record["claims"])
        assert record["persisted"]["audit"]["quality_status"] == "complete"
        # every fake response reports usage: all measured, split, nothing estimated
        usage = record["usage"]
        assert usage["tokens_estimated"] == 0 and usage["tokens_measured"] > 0
        assert (
            usage["measured_prompt_tokens"] + usage["measured_completion_tokens"]
            == (usage["tokens_measured"])
        )
        assert usage["cost"]["estimated_upper_bound_usd"] == 0
        assert set(record["stage_seconds"]) == {
            "prepare_evidence",
            "draft_and_align",
            "verify_claims",
            "finalize",
        }
        for path in directory.iterdir():
            content = path.read_text(encoding="utf-8")
            assert OPENAI_SECRET not in content and TAVILY_SECRET not in content

    def test_an_unconfirmed_citation_is_reported_as_unresolved(self, configured, capsys):
        llm = FinalLLM(
            pool=judge(pick="Bravo"), check=judge(verdict="not_enough_evidence", relation="neutral")
        )
        code, _ = run(configured, "--execute", factory=fake_pipeline(llm, order=bravo_first))
        _, record = artifact(configured)
        assert record["unresolved"], "the unconfirmed claim must be listed"
        assert {c["claim_status"] for c in record["unresolved"]} <= {
            "supported_citation_not_confirmed",
            "insufficient_evidence",
            "contested",
            "not_evaluated",
        }
        assert all(c["placement"] for c in record["unresolved"])
        assert code == runner._EXIT_FOR_STATUS.get(record["outcome"]["status"], 1)
        assert "Unresolved / contested claims: 0" not in capsys.readouterr().out

    def test_a_failed_stage_exits_nonzero_and_keeps_diagnostics(self, configured):
        empty = PerQuerySearch({}, academic=False), PerQuerySearch({}, academic=True)
        factory = fake_pipeline(FinalLLM(pool=exact_support), web=empty[0], academic=empty[1])
        code, workspaces = run(configured, "--execute", factory=factory)
        assert code == runner.EXIT_FAILED
        _, record = artifact(configured)
        [error] = record["errors"]
        assert error["stage"] == "prepare_evidence" and error["code"] == "no_evidence"
        assert record.get("outcome") is None and record["exit_code"] == 1
        assert list(workspaces.iterdir()) == []

    def test_a_late_error_keeps_the_run_budget_and_claims(self, configured):
        def boom(prompt):
            raise RuntimeError("renderer exploded")

        llm = FinalLLM(
            pool=judge(pick="Bravo"),
            check=judge(verdict="not_enough_evidence", relation="neutral"),
            revise=boom,
        )
        code, _ = run(configured, "--execute", factory=fake_pipeline(llm, order=bravo_first))
        assert code == runner.EXIT_FAILED
        _, record = artifact(configured)
        assert record["errors"][0] == {
            "stage": "finalize",
            "code": "RuntimeError",
            "message": "renderer exploded",
        }
        assert record["run_status"] == "failed"
        assert record["claims"] and record["budget"]["attempts"] > 0

    def test_finalization_failed_exits_nonzero(self, configured):
        from app.services.llm import LLMResponseInvalid

        llm = FinalLLM(
            pool=judge(pick="Bravo"),
            check=judge(verdict="not_enough_evidence", relation="neutral"),
            revise=lambda prompt: LLMResponseInvalid(),
        )
        code, _ = run(configured, "--execute", factory=fake_pipeline(llm, order=bravo_first))
        _, record = artifact(configured)
        assert code == runner.EXIT_FAILED
        assert record["outcome"] == {
            "status": "finalization_failed",
            "reasons": ["revision not produced: invalid output twice"],
        }
        assert record["final_markdown"] is None
        assert record["budget"]["stages"]["revision"]["failures"] == 2


class TestAccounting:
    @pytest.mark.parametrize(
        ("result", "code"),
        [
            ({"errors": [], "outcome": {"status": "complete"}}, 0),
            ({"errors": [], "outcome": {"status": "degraded"}}, 3),
            ({"errors": [], "outcome": {"status": "finalization_failed"}}, 1),
            ({"errors": [{"stage": "verify_claims"}]}, 1),
        ],
    )
    def test_exit_codes(self, result, code):
        assert runner.exit_code(result) == code

    def test_measured_and_estimated_tokens_are_priced_apart(self):
        budget = {
            "tokens_measured": 1_200,
            "tokens_estimated": 2_000,
            "stages": {
                "drafting": {"measured_prompt_tokens": 1_000, "measured_completion_tokens": 200},
                "revision": {"measured_prompt_tokens": 0, "measured_completion_tokens": 0},
            },
        }
        usage = runner.usage_and_cost(budget, "gpt-4o-mini-2024-07-18")
        assert usage["cost"] == {
            "measured_usd": round(1_000 * 0.15e-6 + 200 * 0.60e-6, 6),
            "estimated_upper_bound_usd": round(2_000 * 0.60e-6, 6),
        }
        assert runner.usage_and_cost(budget, "some-unlisted-model")["cost"] is None
        assert runner.usage_and_cost(None, "gpt-4o-mini") is None
