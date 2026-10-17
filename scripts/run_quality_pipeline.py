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

The artifact directory `<out>/<timestamp>-<run>/` holds `run.json` (question,
configuration, budget limits and use, outcome, per-claim citation results, the
persisted audit, errors), `final_report.md` when a report was produced, and — for
error analysis — `diagnostics.json` (selected passage texts and sources, each claim's
verification result and evidence rows, every citation and final claim check) and
`model_calls.json` (every model attempt the budget let through: prompt, raw answer or
error, model, usage, latency). Never a credential.

Exit codes: 0 complete (or preflight ready) · 1 failed (an error, or
finalization_failed) · 2 refused (invalid arguments, missing provider configuration,
insufficient budget, non-isolated database) · 3 degraded (a report was produced but
the run is incomplete).
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
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
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.constants import ResearchMode  # noqa: E402
from app.core.exceptions import AppError  # noqa: E402
from app.db.session import build_engine, reset_engine, unit_of_work  # noqa: E402
from app.models import (  # noqa: E402
    CitationCheck,
    DocumentChunk,
    FinalClaimCheck,
    ResearchRun,
    VerificationResult,
)
from app.schemas.research import ResearchStartRequest  # noqa: E402
from app.services.benchmark.metrics import MODEL_PRICES_PER_TOKEN  # noqa: E402
from app.services.claims.prompts import CLAIM_EXTRACTION_PROMPT_VERSION  # noqa: E402
from app.services.llm import LLMService  # noqa: E402
from app.services.quality.budget import (  # noqa: E402
    BudgetInsufficient,
    RunBudget,
    no_sdk_retries,
)
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


def release_vector_stores(path: Path) -> int:
    """Stop ChromaDB's cached systems whose persist directory is inside `path`.

    ChromaDB keeps one system per persist directory in a process-wide cache and
    holds its files open until that system is stopped; `clear_system_cache()` only
    forgets the cache without stopping anything, which on Windows left
    `data_level0.bin` locked (first paid run, 2026-10-17). Systems for other
    directories are left alone. Returns how many were stopped."""
    if "chromadb" not in sys.modules:
        return 0
    from chromadb.api.shared_system_client import SharedSystemClient

    gc.collect()  # drop unreachable clients first
    stopped = 0
    for identifier, system in list(SharedSystemClient._identifier_to_system.items()):
        try:
            location = Path(identifier).resolve()
        except (OSError, ValueError):
            continue
        if location == path or path in location.parents:
            SharedSystemClient._identifier_to_system.pop(identifier, None)
            SharedSystemClient._identifier_to_refcount.pop(identifier, None)
            system.stop()
            stopped += 1
    return stopped


def remove_workspace(path: Path, *, attempts: int = 5, pause: float = 0.5) -> bool:
    """Delete a workspace this runner created — identified by its marker — and
    nothing else. The marker is deleted last, so a workspace that could not be
    fully removed still carries it and can be removed later. Returns False if
    anything was left behind."""
    if not (path / MARKER).is_file():
        print(f"  Not removing {path}: no runner marker.")
        return False
    release_vector_stores(path)
    error: OSError | None = None
    for attempt in range(attempts):
        try:
            for child in path.iterdir():
                if child.name == MARKER:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            (path / MARKER).unlink()
            path.rmdir()
            return True
        except OSError as exc:  # a file still held open; give the OS a moment
            error = exc
            time.sleep(pause * (attempt + 1))
    print(f"  Workspace could not be fully removed ({error}); left at {path} (marker kept)")
    return False


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


def default_llm(settings: Settings) -> LLMService:
    """The real client with SDK retries off — what the pipeline builds for itself
    when no client is injected — so every attempt is one counted HTTP request."""
    return LLMService(settings=no_sdk_retries(settings))


def default_pipeline(settings: Settings, llm) -> ReportQualityPipeline:
    return ReportQualityPipeline(settings=settings, llm=llm)


def error_record(stage: str, exc: BaseException) -> dict:
    if isinstance(exc, AppError):
        return {"stage": stage, "code": exc.code, "message": exc.message, "details": exc.details}
    return {"stage": stage, "code": type(exc).__name__, "message": str(exc)[:1000]}


# --- diagnostics (error analysis R1) --------------------------------------------------


_SCHEMA_PROMPTS = {
    "CoveragePlan": PLANNER_PROMPT_VERSION,
    "ClaimExtractionModel": CLAIM_EXTRACTION_PROMPT_VERSION,
    "ClaimVerificationModel": VERIFICATION_PROMPT_VERSION,  # pool, citation and final checks
}


def prompt_version(schema: str | None, prompt: str) -> str | None:
    """The versioned prompt a call used, from its schema (the structured report is the
    draft or, when it carries the verified-claim table, the revision)."""
    if schema == "StructuredReport":
        revision = "Verified claims (alias" in prompt
        return FINAL_REPORT_PROMPT_VERSION if revision else DRAFT_PROMPT_VERSION
    return _SCHEMA_PROMPTS.get(schema or "")


def verifier_calls(calls: list[dict], text: str) -> list[int]:
    """Indexes of the verifier calls that judged exactly this claim or sentence text."""
    needle = f"Claim to verify:\n{text}\n"
    return [
        c["index"]
        for c in calls
        if c["schema"] == "ClaimVerificationModel" and needle in c["prompt"]
    ]


class RecordingLLM:
    """The run's model client, keeping every attempt it sends: prompt, raw response (or
    error), model, usage and latency. It sits INSIDE the pipeline's `BudgetedLLM`, so it
    sees exactly the attempts the budget let through and changes nothing about what is
    counted or refused. System prompts are stored once, keyed by a short hash."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[dict] = []
        self.systems: dict[str, str] = {}

    async def generate_structured_output(self, prompt, schema, **kwargs):
        return await self._record("generate_structured_output", prompt, kwargs, schema)

    async def generate_text(self, prompt, **kwargs):
        return await self._record("generate_text", prompt, kwargs, None)

    async def _record(self, method: str, prompt: str, kwargs: dict, schema):
        system: str | None = kwargs.get("system")
        key = None
        if system:
            key = hashlib.sha256(system.encode()).hexdigest()[:12]
            self.systems.setdefault(key, system)
        call: dict = {
            "index": len(self.calls),
            "method": method,
            "schema": getattr(schema, "__name__", None),
            "system": key,
            "max_tokens": kwargs.get("max_tokens"),
            "prompt_version": prompt_version(getattr(schema, "__name__", None), prompt),
            "prompt": prompt,
        }
        self.calls.append(call)
        args = (prompt,) if schema is None else (prompt, schema)
        started = time.perf_counter()
        try:
            response = await getattr(self._inner, method)(*args, **kwargs)
        except Exception as exc:
            call["error"] = error_record("model_call", exc)
            raise
        finally:
            call["seconds"] = round(time.perf_counter() - started, 3)
        usage = getattr(response, "usage", None)
        raw = getattr(response, "text", None)
        if raw is None and hasattr(response, "data"):  # a response without raw text
            data = response.data
            raw = data.model_dump_json() if hasattr(data, "model_dump_json") else repr(data)
        call.update(
            model=getattr(response, "model", None),
            finish_reason=getattr(response, "finish_reason", None),
            usage=usage.model_dump() if usage is not None else None,
            response_text=raw,
        )
        return response

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


def _columns(row) -> dict:
    return {attr.key: getattr(row, attr.key) for attr in row.__mapper__.column_attrs}


def read_diagnostics(payload: dict | None) -> dict:
    """What the workspace database holds that explains the verdicts, read before the
    workspace is deleted: the selected passages' texts and sources, each claim's
    verification-v2 result and evidence rows (relation, enforced quote, rank), every
    citation check and every final claim check, rationales included. Read-only."""
    evidence = (payload or {}).get("evidence") or {}
    passages = (evidence.get("aliases") or {}).get("passages", {})
    subquestion_of = {
        alias: entry["alias"]
        for entry in evidence.get("per_subquestion", [])
        for alias in entry["passages"]
    }
    alias_of_chunk = {ref["chunk_id"]: alias for alias, ref in passages.items()}
    with unit_of_work() as session:
        runs = session.scalars(select(ResearchRun)).all()
        if len(runs) != 1:
            return {"error": f"expected one run in the workspace, found {len(runs)}"}
        report = runs[0].report
        selected = []
        for alias, ref in passages.items():
            chunk = session.get(DocumentChunk, uuid.UUID(ref["chunk_id"]))
            source = chunk.document.source if chunk is not None else None
            selected.append(
                {
                    "alias": alias,
                    "source_label": ref["source_label"],
                    "subquestion": subquestion_of.get(alias),
                    "chunk_id": ref["chunk_id"],
                    "source_id": source.id if source is not None else None,
                    "text": chunk.text if chunk is not None else None,
                    "source": None
                    if source is None
                    else {
                        "title": source.title,
                        "url": source.url,
                        "doi": source.doi,
                        "venue": source.venue,
                        "year": source.publication_year,
                        "source_type": source.source_type,
                    },
                }
            )
        claims = sorted(report.claims, key=lambda c: c.position) if report else []
        claim_ids = [claim.id for claim in claims]
        verified = []
        for claim in claims:
            result = session.scalars(
                select(VerificationResult).where(VerificationResult.claim_id == claim.id)
            ).first()
            verified.append(
                {
                    "claim_id": claim.id,
                    "text": claim.text,
                    "importance": claim.importance,
                    "verification": None if result is None else _columns(result),
                    "evidence": [
                        {
                            "evidence_id": row.id,
                            "passage": alias_of_chunk.get(str(row.document_chunk_id)),
                            "chunk_id": row.document_chunk_id,
                            "relation": row.relation,
                            "quoted_text": row.quoted_text,
                            "similarity": row.similarity,
                            "rank": row.rank,
                        }
                        for row in sorted(claim.evidence, key=lambda r: r.rank or 0)
                    ],
                }
            )
        checks = session.scalars(
            select(CitationCheck).where(CitationCheck.claim_id.in_(claim_ids))
        ).all()
        finals = (
            session.scalars(
                select(FinalClaimCheck).where(FinalClaimCheck.report_id == report.id)
            ).all()
            if report
            else []
        )
        return {
            "passages": selected,
            "claims": verified,
            "citation_checks": [_columns(row) for row in checks],
            "final_claim_checks": [_columns(row) for row in finals],
        }


async def run_pipeline(
    settings: Settings,
    question: str,
    depth: str,
    *,
    pipeline_factory: Callable[[Settings, object], ReportQualityPipeline] = default_pipeline,
    llm_factory: Callable[[Settings], object] = default_llm,
) -> dict:
    """The four pipeline stages in order; the first error stops the run and is kept.
    The client and pipeline are built here, inside the event loop that uses them."""
    recorder = RecordingLLM(llm_factory(settings))
    pipeline = pipeline_factory(settings, recorder)
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
    finally:
        await recorder.aclose()  # injected, so the pipeline does not close it

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
    try:
        audit = (result.get("persisted") or {}).get("audit") or {}
        result["diagnostics"] = read_diagnostics(audit.get("payload"))
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        result["errors"].append(error_record("diagnostics", exc))
    diagnostics = result.get("diagnostics") or {}
    for entry in (*diagnostics.get("claims", []), *diagnostics.get("final_claim_checks", [])):
        entry["verifier_calls"] = verifier_calls(recorder.calls, entry["text"])
    result["model_calls"] = recorder.calls
    result["system_prompts"] = recorder.systems
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
    record = dict(record)
    files = {
        # bulky diagnostics live beside run.json so it stays readable
        "model_calls.json": {
            "system_prompts": record.pop("system_prompts", {}),
            "calls": record.pop("model_calls", []),
        },
        "diagnostics.json": record.pop("diagnostics", None),
        "run.json": record,
    }
    contents = {
        name: json.dumps(value, indent=2, default=str, ensure_ascii=False) + "\n"
        for name, value in files.items()
        if value is not None
    }
    if record.get("final_markdown"):
        contents["final_report.md"] = record["final_markdown"]
    for name, content in contents.items():
        for secret in secret_values(settings):
            content = content.replace(secret, "[redacted]")
        (directory / name).write_text(content, encoding="utf-8")
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
    pipeline_factory: Callable[[Settings, object], ReportQualityPipeline] = default_pipeline,
    llm_factory: Callable[[Settings], object] = default_llm,
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
                run_pipeline(
                    settings,
                    args.question,
                    args.depth,
                    pipeline_factory=pipeline_factory,
                    llm_factory=llm_factory,
                )
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
