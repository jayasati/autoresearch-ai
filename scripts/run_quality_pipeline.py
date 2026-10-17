"""
Run report-quality-v1 once, locally, on one research question (stage 34).

    backend/.venv/Scripts/python.exe scripts/run_quality_pipeline.py "QUESTION"
    backend/.venv/Scripts/python.exe scripts/run_quality_pipeline.py "QUESTION" --execute

Options:
    --depth quick|standard|deep   default quick
    --execute                     make the REAL, PAID provider calls (see below)
    --out DIR                     artifact directory (default data/exports/quality-runs)
    --keep-workspace              keep the temporary SQLite file and vector store

**Without `--execute` this is a preflight.** It creates the isolated workspace, applies
the migrations to its throwaway SQLite file, checks the provider configuration and
prints the budget a run would be held to. No provider is called, no research result
is written, and the workspace is deleted afterwards.

**With `--execute` it makes paid provider calls** (OpenAI or Azure OpenAI, Tavily,
Semantic Scholar) through the `ReportQualityPipeline` stages exactly as they are —
prepare_evidence → draft_and_align → verify_claims → finalize — under the configured
per-run budget (model attempts, tokens, claims, deadline). Nothing of the pipeline is
re-implemented here. Retrieval searches are not model calls and are outside that
budget; the preflight prints their bound.

Isolation: for the duration of the run, this process's DATABASE_URL points at a new
SQLite file and CHROMA_PERSIST_DIR at a new directory, both inside a fresh temporary
workspace, and REPORT_QUALITY_ENABLED is on for this process only. A configuration
that does not resolve to that workspace is refused before anything is migrated. Only
a directory carrying this runner's marker file is ever deleted.

The artifact (`<out>/<timestamp>-<run>/run.json`, plus `final_report.md` when a
report was produced) holds the question, configuration, budget limits and use,
outcome, per-claim citation results, the persisted audit and any errors — never a
credential.

Exit codes: 0 complete (or preflight ready) · 1 failed (an error, or
finalization_failed) · 2 refused (invalid arguments, missing provider configuration,
insufficient budget, non-isolated database) · 3 degraded (a report was produced but
the run is incomplete).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.constants import ResearchMode  # noqa: E402
from app.core.exceptions import AppError  # noqa: E402
from app.db.session import build_engine, reset_engine  # noqa: E402
from app.schemas.research import ResearchStartRequest  # noqa: E402
from app.services.benchmark.metrics import MODEL_PRICES_PER_TOKEN  # noqa: E402
from app.services.claims.prompts import CLAIM_EXTRACTION_PROMPT_VERSION  # noqa: E402
from app.services.quality.budget import BudgetInsufficient, RunBudget  # noqa: E402
from app.services.quality.pipeline import (  # noqa: E402
    ReportQualityPipeline,
    budget_for,
    preflight,
)
from app.services.quality.prompts import (  # noqa: E402
    DRAFT_PROMPT_VERSION,
    FINAL_REPORT_PROMPT_VERSION,
    PLANNER_PROMPT_VERSION,
)
from app.services.quality.verification import CITATION_CHECK_VERSION  # noqa: E402
from app.services.verification.prompts import VERIFICATION_PROMPT_VERSION  # noqa: E402

RUNNER_VERSION = "quality-runner-v1"
ARTIFACTS = ROOT / "data" / "exports" / "quality-runs"
MARKER = ".quality-run-workspace"
EXIT_COMPLETE, EXIT_FAILED, EXIT_REFUSED, EXIT_DEGRADED = 0, 1, 2, 3
_EXIT_FOR_STATUS = {"complete": EXIT_COMPLETE, "degraded": EXIT_DEGRADED}
_OVERRIDDEN = ("DATABASE_URL", "CHROMA_PERSIST_DIR", "REPORT_QUALITY_ENABLED")
_SECRET_SETTINGS = (
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "TAVILY_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
)


class UnsafeWorkspace(RuntimeError):
    """The effective configuration does not point at the runner's own workspace."""


# --- arguments ----------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run report-quality-v1 once on one question (preflight by default)."
    )
    parser.add_argument("question", help="the research question")
    parser.add_argument("--depth", choices=("quick", "standard", "deep"), default="quick")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="make the real, PAID provider calls; without it only a preflight runs",
    )
    parser.add_argument("--out", type=Path, default=ARTIFACTS, help="artifact directory")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args(argv)
    args.question = " ".join(args.question.split())
    try:
        ResearchStartRequest(topic=args.question, depth=args.depth)
    except ValidationError as exc:
        parser.error(f"invalid question: {exc.errors()[0]['msg']}")
    return args


# --- the isolated workspace ---------------------------------------------------------


@dataclass
class Workspace:
    path: Path
    settings: Settings


def _reset_caches() -> None:
    reset_engine()
    get_settings.cache_clear()


def check_isolated(settings: Settings, path: Path) -> None:
    """Refuse unless the database and the vector store are inside `path`."""
    url = settings.DATABASE_URL
    if settings.database_is_postgres or not url.startswith("sqlite:///"):
        raise UnsafeWorkspace(
            f"database is not the workspace SQLite file: {settings.database_url_safe}"
        )
    database = Path(url.removeprefix("sqlite:///")).resolve()
    vectors = Path(settings.CHROMA_PERSIST_DIR).resolve()
    for name, location in (("database", database), ("vector store", vectors)):
        if path not in location.parents:
            raise UnsafeWorkspace(f"{name} is outside the workspace: {location}")
    if not settings.REPORT_QUALITY_ENABLED:
        raise UnsafeWorkspace("REPORT_QUALITY_ENABLED did not take effect")


def remove_workspace(path: Path) -> bool:
    """Delete a workspace this runner created — identified by its marker — and
    nothing else. Returns False if anything was left behind."""
    if not (path / MARKER).is_file():
        print(f"  Not removing {path}: no runner marker.")
        return False
    if (path / "vectorstore").exists() and "chromadb" in sys.modules:
        # Release ChromaDB's cached client for this path, which holds its files open.
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    try:
        shutil.rmtree(path)
    except OSError as exc:
        print(f"  Workspace could not be fully removed ({exc}); left at {path}")
        return False
    return True


@contextmanager
def isolated_workspace(root: Path | None = None, *, keep: bool = False) -> Iterator[Workspace]:
    path = Path(tempfile.mkdtemp(prefix="quality-run-", dir=root)).resolve()
    (path / MARKER).write_text(RUNNER_VERSION, encoding="utf-8")
    saved = {key: os.environ.get(key) for key in _OVERRIDDEN}
    os.environ["DATABASE_URL"] = f"sqlite:///{(path / 'run.db').as_posix()}"
    os.environ["CHROMA_PERSIST_DIR"] = str(path / "vectorstore")
    os.environ["REPORT_QUALITY_ENABLED"] = "true"
    _reset_caches()
    try:
        settings = get_settings()
        check_isolated(settings, path)
        yield Workspace(path, settings)
    finally:
        reset_engine()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _reset_caches()
        if keep:
            print(f"  Workspace kept at {path}")
        else:
            remove_workspace(path)


def migrate(settings: Settings) -> str:
    """Bring the workspace database to the latest migration; returns the revision."""
    engine = build_engine(settings)
    try:
        with engine.begin() as connection:
            config = Config()  # no ini file: Alembic must not reconfigure logging
            config.set_main_option("script_location", str(BACKEND / "migrations"))
            config.set_main_option("path_separator", "os")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


# --- readiness and budget -----------------------------------------------------------


def model_name(settings: Settings) -> str | None:
    if settings.llm_provider == "azure_openai":
        return settings.AZURE_OPENAI_DEPLOYMENT_NAME
    return settings.OPENAI_MODEL if settings.llm_provider == "openai" else None


def missing_configuration(settings: Settings) -> list[str]:
    missing = []
    if settings.llm_provider is None:
        missing.append("OPENAI_API_KEY (or all three AZURE_OPENAI_* settings)")
    if not Settings._is_real_credential(settings.TAVILY_API_KEY):
        missing.append("TAVILY_API_KEY")
    return missing


def prices(model: str | None) -> tuple[float, float] | None:
    """(input, output) USD per token, matched by longest prefix; None if unlisted."""
    match = max(
        (prefix for prefix in MODEL_PRICES_PER_TOKEN if model and model.startswith(prefix)),
        key=len,
        default=None,
    )
    return MODEL_PRICES_PER_TOKEN[match] if match else None


def budget_plan(settings: Settings, depth: str) -> dict:
    budget = RunBudget.for_depth(settings, depth)
    subquestions, passages = budget_for(settings, depth)
    model = model_name(settings)
    price = prices(model)
    return {
        "model": model,
        "limits": budget.as_record()["limits"],
        "max_subquestions": subquestions,
        "evidence_passages": passages,
        # At the token cap: every token billed at the input price (low) or the
        # output price (high). Published list prices; cached-input discounts ignored.
        "cost_at_token_cap_usd": None
        if price is None
        else {
            "low": round(budget.max_tokens * price[0], 4),
            "high": round(budget.max_tokens * price[1], 4),
        },
        "retrieval_bound": {
            "web_searches": subquestions,
            "web_attempts_each": settings.TAVILY_MAX_ATTEMPTS,
            "academic_searches": subquestions,
            "academic_attempts_each": settings.SEMANTIC_SCHOLAR_MAX_ATTEMPTS,
            "semantic_scholar_keyed": bool(settings.SEMANTIC_SCHOLAR_API_KEY),
        },
    }


def configuration(settings: Settings, depth: str) -> dict:
    """What determines the run, by name — a whitelist, so no credential can enter."""
    return {
        "depth": depth,
        "mode": ResearchMode.SEARCH_GROUNDED.value,
        "llm_provider": settings.llm_provider,
        "model": model_name(settings),
        "embedding_model": settings.EMBEDDING_MODEL,
        "prompt_versions": {
            "planner": PLANNER_PROMPT_VERSION,
            "draft": DRAFT_PROMPT_VERSION,
            "claim_extraction": CLAIM_EXTRACTION_PROMPT_VERSION,
            "verification": VERIFICATION_PROMPT_VERSION,
            "citation_check": CITATION_CHECK_VERSION,
            "final_report": FINAL_REPORT_PROMPT_VERSION,
        },
        "retrieval": {
            "tavily_search_depth": settings.TAVILY_SEARCH_DEPTH,
            "tavily_max_results": settings.TAVILY_MAX_RESULTS,
            "semantic_scholar_max_results": settings.SEMANTIC_SCHOLAR_MAX_RESULTS,
        },
        "report_quality": {
            name: getattr(settings, name)
            for name in type(settings).model_fields
            if name.startswith("REPORT_QUALITY_")
        },
        "code": git_state(),
    }


def git_state() -> dict:
    def git(*args: str) -> str | None:
        try:
            return subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    status = git("status", "--porcelain")
    return {"commit": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


# --- the run ------------------------------------------------------------------------


def default_pipeline(settings: Settings) -> ReportQualityPipeline:
    return ReportQualityPipeline(settings=settings)


def error_record(stage: str, exc: BaseException) -> dict:
    if isinstance(exc, AppError):
        return {"stage": stage, "code": exc.code, "message": exc.message, "details": exc.details}
    return {"stage": stage, "code": type(exc).__name__, "message": str(exc)[:1000]}


async def run_pipeline(pipeline: ReportQualityPipeline, question: str, depth: str) -> dict:
    """The four pipeline stages in order; the first error stops the run and is kept."""
    request = ResearchStartRequest(topic=question, mode=ResearchMode.SEARCH_GROUNDED, depth=depth)
    seconds: dict[str, float] = {}
    result: dict = {"stage_seconds": seconds, "errors": []}
    prepared = drafted = verified = final = None
    stage = "prepare_evidence"
    try:
        started = time.perf_counter()
        prepared = await pipeline.prepare_evidence(request)
        seconds[stage] = round(time.perf_counter() - started, 3)
        stage, started = "draft_and_align", time.perf_counter()
        drafted = await pipeline.draft_and_align(prepared)
        seconds[stage] = round(time.perf_counter() - started, 3)
        stage, started = "verify_claims", time.perf_counter()
        verified = await pipeline.verify_claims(prepared, drafted)
        seconds[stage] = round(time.perf_counter() - started, 3)
        stage, started = "finalize", time.perf_counter()
        final = await pipeline.finalize(prepared, drafted, verified)
        seconds[stage] = round(time.perf_counter() - started, 3)
    except Exception as exc:  # noqa: BLE001 - recorded and reported, never swallowed
        seconds[stage] = round(time.perf_counter() - started, 3)
        result["errors"].append(error_record(stage, exc))

    result["research_id"] = str(prepared.research_id) if prepared else None
    result["budget"] = prepared.budget.as_record() if prepared and prepared.budget else None
    if verified is not None:
        result["claims"] = [
            {**record.as_record(), "text": record.text} for record in verified.claims
        ]
    if final is not None:
        result["outcome"] = {"status": final.status, "reasons": final.reasons}
        result["final_markdown"] = final.final_markdown
        result["revised"] = final.revised
        result["warnings"] = final.warnings
    elif verified is not None:
        result["warnings"] = verified.warnings
    if prepared is not None:
        try:
            run = pipeline.research.get_run(prepared.research_id)
            result["run_status"] = run.status.value
            result["persisted"] = pipeline.store.quality_record(prepared.research_id)
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            result["errors"].append(error_record("read_back", exc))
    return result


def usage_and_cost(budget: dict | None, model: str | None) -> dict | None:
    """Measured tokens priced as billed; estimated tokens (failed attempts, responses
    without usage) priced as an upper bound at the output price — kept apart."""
    if budget is None:
        return None
    stages = budget["stages"]
    measured_prompt = sum(s.get("measured_prompt_tokens", 0) for s in stages.values())
    measured_completion = sum(s.get("measured_completion_tokens", 0) for s in stages.values())
    price = prices(model)
    cost = None
    if price is not None:
        cost = {
            "measured_usd": round(measured_prompt * price[0] + measured_completion * price[1], 6),
            "estimated_upper_bound_usd": round(budget["tokens_estimated"] * price[1], 6),
        }
    return {
        "tokens_measured": budget["tokens_measured"],
        "tokens_estimated": budget["tokens_estimated"],
        "measured_prompt_tokens": measured_prompt,
        "measured_completion_tokens": measured_completion,
        "cost": cost,
        "price_listed": price is not None,
    }


def exit_code(result: dict) -> int:
    outcome = result.get("outcome")
    if result["errors"] and outcome is None:
        return EXIT_FAILED
    return _EXIT_FOR_STATUS.get(outcome["status"], EXIT_FAILED) if outcome else EXIT_FAILED


def unresolved(result: dict) -> list[dict]:
    persisted = result.get("persisted") or {}
    placements = (
        ((persisted.get("audit") or {}).get("payload") or {})
        .get("finalization", {})
        .get("placements", {})
    )
    return [
        {
            "alias": claim["alias"],
            "claim_status": claim["claim_status"],
            "not_evaluated_reason": claim["not_evaluated_reason"]
            or claim["citation_not_evaluated_reason"],
            "placement": placements.get(claim["alias"]),
            "text": claim["text"],
        }
        for claim in result.get("claims", [])
        if claim["claim_status"] != "supported_by_cited_source"
    ]


# --- artifact and output ------------------------------------------------------------


def secret_values(settings: Settings) -> list[str]:
    values = [getattr(settings, name) for name in _SECRET_SETTINGS]
    password = make_url(settings.DATABASE_URL).password
    return [v for v in (*values, password) if isinstance(v, str) and len(v) >= 6]


def write_artifact(out: Path, record: dict, settings: Settings) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run = (record.get("research_id") or "no-run")[:8]
    directory = out / f"{stamp}-{run}"
    directory.mkdir(parents=True, exist_ok=False)
    payload = json.dumps(record, indent=2, default=str, ensure_ascii=False)
    markdown = record.get("final_markdown") or ""
    for secret in secret_values(settings):
        payload = payload.replace(secret, "[redacted]")
        markdown = markdown.replace(secret, "[redacted]")
    (directory / "run.json").write_text(payload + "\n", encoding="utf-8")
    if markdown:
        (directory / "final_report.md").write_text(markdown, encoding="utf-8")
    return directory


def print_plan(plan: dict, revision: str, workspace: Path) -> None:
    limits = plan["limits"]
    print(f"Workspace : {workspace} (SQLite at migration {revision})")
    print(f"Model     : {plan['model'] or 'not configured'}")
    print(
        f"Budget    : {limits['attempts']} model attempts, {limits['tokens']} tokens, "
        f"{limits['claims']} verified claims, {limits['deadline_seconds']:.0f} s deadline"
    )
    cost = plan["cost_at_token_cap_usd"]
    print(
        "Cost cap  : "
        + (
            f"${cost['low']}-${cost['high']} at the token cap (list prices)"
            if cost
            else "no listed price"
        )
    )
    bound = plan["retrieval_bound"]
    print(
        f"Retrieval : <={bound['web_searches']} Tavily searches (<={bound['web_attempts_each']} "
        f"attempts each), <={bound['academic_searches']} Semantic Scholar searches "
        f"(<={bound['academic_attempts_each']} attempts each, "
        f"{'keyed' if bound['semantic_scholar_keyed'] else 'unkeyed, rate-limited'}); "
        "outside the model budget"
    )


def print_result(record: dict) -> None:
    outcome = record.get("outcome")
    print("\n=== Final report " + "=" * 60)
    print(record.get("final_markdown") or "(no final report produced)")
    print("=" * 77)
    print(f"Outcome   : {outcome['status'] if outcome else 'failed'}  (exit {record['exit_code']})")
    for reason in (outcome or {}).get("reasons", []):
        print(f"            - {reason}")
    for error in record["errors"]:
        print(f"Error     : [{error['stage']}] {error['code']}: {error['message']}")
    claims = record.get("claims", [])
    if claims:
        print("\nCitation-specific support (pool verdict | cited passage outcomes | status):")
        for claim in claims:
            outcomes = ", ".join(f"{k}={v}" for k, v in claim["citation_outcomes"].items()) or "-"
            alias, pool, status = claim["alias"], claim["pool_status"], claim["claim_status"]
            print(f"  {alias:>4} {pool:<20} {outcomes:<40} {status}")
    open_claims = record.get("unresolved", [])
    print(f"\nUnresolved / contested claims: {len(open_claims)}")
    for claim in open_claims:
        reason = f", {claim['not_evaluated_reason']}" if claim["not_evaluated_reason"] else ""
        where = claim["placement"]
        print(f"  {claim['alias']:>4} {claim['claim_status']}{reason} -> {where}: {claim['text']}")
    budget = record.get("budget")
    if budget:
        print("\nStage usage (attempts | measured tokens | estimated tokens | refused | seconds):")
        for name, stage in budget["stages"].items():
            print(
                f"  {name:<16} {stage['attempts']:>3} | {stage['measured_tokens']:>7} | "
                f"{stage['estimated_tokens']:>7} | {stage['refused']:>2} | {stage['seconds']:.2f}"
            )
        limits = budget["limits"]
        print(
            f"  total            {budget['attempts']}/{limits['attempts']} attempts, "
            f"{budget['tokens_used']}/{limits['tokens']} tokens "
            f"({budget['tokens_measured']} measured + {budget['tokens_estimated']} estimated)"
        )
    print("Latency   : " + ", ".join(f"{k} {v:.1f}s" for k, v in record["stage_seconds"].items()))
    usage = record.get("usage")
    cost = usage and usage["cost"]
    print(
        "Cost      : "
        + (
            f"${cost['measured_usd']} measured + <=${cost['estimated_upper_bound_usd']} for "
            "estimated tokens (list prices)"
            if cost
            else "no listed price for this model"
        )
    )


def main(
    argv: list[str] | None = None,
    *,
    pipeline_factory: Callable[[Settings], ReportQualityPipeline] = default_pipeline,
    workspace_root: Path | None = None,
) -> int:
    args = parse_args(argv)
    try:
        with isolated_workspace(workspace_root, keep=args.keep_workspace) as workspace:
            settings = workspace.settings
            revision = migrate(settings)
            plan = budget_plan(settings, args.depth)
            mode = "EXECUTE (paid)" if args.execute else "preflight"
            print(f"report-quality-v1 | {args.depth} | {mode}")
            print(f"Question  : {args.question}")
            print_plan(plan, revision, workspace.path)

            refusals = missing_configuration(settings)
            try:
                preflight(settings, args.depth, RunBudget.for_depth(settings, args.depth))
            except BudgetInsufficient as exc:
                refusals.append(f"budget below the pipeline minimum: {exc.details}")
            for refusal in refusals:
                print(f"Not ready : {refusal}")
            if not args.execute:
                print("Preflight only: no provider was called and no research result was written.")
                if not refusals:
                    print("Ready. Add --execute to make the paid run.")
                return EXIT_REFUSED if refusals else EXIT_COMPLETE
            if refusals:
                print("Refused: --execute needs the configuration above.")
                return EXIT_REFUSED

            started = datetime.now(UTC)
            result = asyncio.run(
                run_pipeline(pipeline_factory(settings), args.question, args.depth)
            )
            record = {
                "runner_version": RUNNER_VERSION,
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "question": args.question,
                "configuration": configuration(settings, args.depth),
                "budget_plan": plan,
                **result,
            }
            record["unresolved"] = unresolved(record)
            record["usage"] = usage_and_cost(record.get("budget"), plan["model"])
            record["exit_code"] = exit_code(record)
            directory = write_artifact(args.out, record, settings)
            print_result(record)
            print(f"\nArtifact  : {directory}")
            return record["exit_code"]
    except UnsafeWorkspace as exc:
        print(f"Refused: {exc}")
        return EXIT_REFUSED


if __name__ == "__main__":
    # A report can hold any character; a legacy console code page must not crash it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    raise SystemExit(main())
