"""
The controlled experiment runner.

    for each benchmark question × arm:
        product phase     run the arm's pipeline (what a user would get, and pay for)
        evaluation phase  the SAME methodology for every arm: extract claims, verify
                          them against the run's own evidence, judge retrieval
                          relevance against the item's expected_evidence
        measure           the eleven metrics, from stored rows and run receipts

Four arms over the same questions:

    A  model_only                  topic → LLM → report
    B  hybrid                      topic → web search → LLM → report
    C  search_grounded             topic → web + academic → RAG → LLM → report
    D  full_autoresearch_verified  C + claim extraction + verification + final report
                                   *as the product* (their cost and latency count)

Held constant, recorded in every results file: the model and generation settings
(one Settings object for every run), the evaluation methodology (same extractor,
verifier, judge and prompt versions for every arm), the benchmark set (its version
and content fingerprint are stamped into the results). For arms A–C the evaluation's
claims/verification work is **overhead, billed separately** in the results — charging
C's product budget for verification it does not perform would blur exactly the
C-versus-D comparison the experiment exists to make.

Honesty rules, enforced here:
- No arbitrary overall score exists anywhere in the outputs.
- A failed item×arm is recorded with its error and null metrics — never retried into
  silence, never dropped, never invented.
- Raw results are stored (results.json carries run ids, every per-item number, the
  judge's raw pairs) and per-run metric rows land in `evaluation_result`; the
  summary is derived from the same payload that is written to disk.
- The final test set is NOT the default; running it is an explicit choice governed
  by data/benchmarks/README.md.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from app.core.config import Settings, get_settings
from app.core.constants import MetricKey, ResearchMode
from app.core.exceptions import AppError
from app.schemas.benchmark import BenchmarkItem, BenchmarkSet
from app.schemas.research import ResearchStartRequest
from app.services.benchmark import metrics as formulas
from app.services.benchmark.dataset import load_set
from app.services.benchmark.external_judge import (
    EXTERNAL_JUDGE_VERSION,
    judge_claims_against_pool,
)
from app.services.benchmark.external_pool import (
    POOL_VERSION,
    PoolUnavailable,
    load_or_build_pool,
)
from app.services.benchmark.relevance import RELEVANCE_JUDGE_VERSION, judge_relevance
from app.services.claims import CLAIM_EXTRACTION_PROMPT_VERSION, ClaimExtractor
from app.services.llm import LLMService
from app.services.orchestration import (
    DEPTH_PROFILES,
    ReportFinalizer,
    ResearchOrchestrator,
)
from app.services.research_service import ResearchService
from app.services.verification import VERIFICATION_PROMPT_VERSION, ClaimVerifier

logger = logging.getLogger(__name__)

ARMS = ("model_only", "hybrid", "search_grounded", "full_autoresearch_verified")
ARM_TO_MODE: dict[str, ResearchMode] = {
    "model_only": ResearchMode.MODEL_ONLY,
    "hybrid": ResearchMode.HYBRID,
    "search_grounded": ResearchMode.SEARCH_GROUNDED,
    "full_autoresearch_verified": ResearchMode.SEARCH_GROUNDED,
}

#: evaluation_result rows written per run, where a MetricKey exists for the metric.
DB_METRIC_KEYS = {
    "claim_support_rate": MetricKey.CLAIM_SUPPORT_RATE,
    "unsupported_claim_rate": MetricKey.HALLUCINATION_RATE,
    "citation_precision": MetricKey.CITATION_PRECISION,
    "conflicts_detected": MetricKey.CONFLICTS_DETECTED,
    "product_total_tokens": MetricKey.TOTAL_TOKENS,
    "product_cost_usd": MetricKey.COST_USD,
    "product_latency_seconds": MetricKey.WALL_CLOCK_SECONDS,
}


class ExperimentRunner:
    """Runs one benchmark set through the four arms and writes the results."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        research_service: ResearchService | None = None,
        orchestrator: ResearchOrchestrator | None = None,
        extractor: ClaimExtractor | None = None,
        verifier: ClaimVerifier | None = None,
        finalizer: ReportFinalizer | None = None,
        judge_llm: LLMService | None = None,
        depth: str = "quick",
        output_root: str | None = None,
        pool_store_factory=None,
        pool_web_search=None,
        pool_academic_search=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.research = research_service or ResearchService(settings=self.settings)
        self.orchestrator = orchestrator or ResearchOrchestrator(settings=self.settings)
        self.extractor = extractor or ClaimExtractor(settings=self.settings)
        self.verifier = verifier or ClaimVerifier(settings=self.settings)
        self.finalizer = finalizer or ReportFinalizer(settings=self.settings)
        self._judge_llm = judge_llm
        self._pool_store_factory = pool_store_factory
        self._pool_web_search = pool_web_search
        self._pool_academic_search = pool_academic_search
        self.depth = depth
        self.output_root = Path(
            output_root or f"{self.settings.BENCHMARK_DATA_DIR}/results"
        )

    # --- the experiment ------------------------------------------------------------

    async def run_experiment(
        self,
        set_name: str = "development",
        arms: tuple[str, ...] = ARMS,
        limit: int | None = None,
    ) -> dict:
        benchmark_set = load_set(set_name, self.settings)
        items = benchmark_set.items[:limit] if limit else benchmark_set.items
        started_at = datetime.now(UTC)
        benchmark_run_id = self.research.create_benchmark_run(
            name=f"experiment-{started_at:%Y%m%dT%H%M%SZ}",
            topic_set=benchmark_set.name,
            topic_set_version=benchmark_set.version,
            notes=f"arms={','.join(arms)} depth={self.depth} items={len(items)}",
        )
        logger.info(
            "Experiment %s: %d items × %d arms on %s v%s",
            benchmark_run_id,
            len(items),
            len(arms),
            benchmark_set.name,
            benchmark_set.version,
        )

        rows: list[dict] = []
        for item in items:
            for arm in arms:
                rows.append(await self._run_cell(item, arm, benchmark_run_id))

        self.research.complete_benchmark_run(benchmark_run_id)
        payload = self._payload(benchmark_set, items, arms, rows, benchmark_run_id, started_at)
        output_dir = self._write_outputs(payload, benchmark_set.name, started_at)
        payload["output_dir"] = str(output_dir)
        logger.info("Experiment complete; outputs in %s", output_dir)
        return payload

    async def _run_cell(self, item: BenchmarkItem, arm: str, benchmark_run_id) -> dict:
        """One question through one arm, measured. Failures become recorded rows."""
        row: dict[str, Any] = {
            "question_id": item.question_id,
            "category": item.category.value,
            "difficulty": item.difficulty,
            "arm": arm,
            "error": None,
        }
        try:
            # --- product phase -------------------------------------------------
            run = await self.orchestrator.run(
                ResearchStartRequest(
                    topic=item.question, mode=ARM_TO_MODE[arm], depth=self.depth
                ),
                benchmark_run_id=benchmark_run_id,
            )
            row["research_id"] = str(run.research_id)
            row["model"] = run.execution_metadata.model
            product_latency = run.execution_metadata.total_seconds
            product_usage = dict(run.execution_metadata.token_usage or {})

            is_full_arm = arm == "full_autoresearch_verified"

            # --- claims + verification: product for D, evaluation overhead for A-C
            extraction = await self.extractor.extract(run.research_id)
            verification = await self.verifier.verify(run.research_id)
            pipeline_latency = extraction.duration_seconds + verification.duration_seconds
            pipeline_usage = _add_usage(extraction.token_usage, verification.token_usage)

            if is_full_arm:
                final = await self.finalizer.finalize(run.research_id)
                product_latency += pipeline_latency + final.duration_seconds
                product_usage = _add_usage(
                    _add_usage(product_usage, pipeline_usage), final.token_usage
                )
                evaluation_usage: dict[str, int] = {}
                evaluation_latency = 0.0
            else:
                evaluation_usage = pipeline_usage
                evaluation_latency = pipeline_latency

            # --- measurement ---------------------------------------------------
            summary = self.research.verification_summary(run.research_id)
            audit = ReportFinalizer._audit(summary["claims"], summary["conflicts"])

            row.update(
                total_claims=audit.total_claims,
                verdicts={
                    "supported": audit.supported,
                    "partially_supported": audit.partially_supported,
                    "unsupported": audit.unsupported,
                    "contradicted": audit.contradicted,
                    "not_enough_evidence": audit.insufficient_evidence,
                },
                claim_support_rate=formulas.claim_support_rate(
                    _verdict_counts(audit)
                ),
                unsupported_claim_rate=formulas.unsupported_claim_rate(
                    _verdict_counts(audit)
                ),
                citation_precision=formulas.citation_precision(audit.citations_by_status),
                citation_coverage=formulas.citation_coverage(
                    audit.claims_with_citations, audit.claims_requiring_citation
                ),
                citations_by_status=audit.citations_by_status,
                conflicts_detected=audit.conflicts_detected,
                conflict_expected=item.expected_behavior.expect_conflicts,
                downgraded_verdicts=audit.downgraded_verdicts,
                claims_skipped_in_verification=verification.claims_skipped,
            )

            row.update(await self._retrieval_metrics(item, run, arm))
            row.update(await self._external_evaluation(item, summary))

            row.update(
                product_latency_seconds=round(product_latency, 3),
                evaluation_latency_seconds=round(evaluation_latency, 3),
                product_tokens=product_usage or None,
                evaluation_tokens=evaluation_usage or None,
                product_total_tokens=product_usage.get("total_tokens", 0),
                product_cost_usd=formulas.estimated_cost_usd(
                    row["model"],
                    product_usage.get("prompt_tokens", 0),
                    product_usage.get("completion_tokens", 0),
                ),
                evaluation_cost_usd=formulas.estimated_cost_usd(
                    row["model"],
                    evaluation_usage.get("prompt_tokens", 0),
                    evaluation_usage.get("completion_tokens", 0),
                ),
            )
            self._store_metrics(run.research_id, row)
        except AppError as exc:
            row["error"] = {"code": exc.code, "message": exc.message}
            logger.warning("%s × %s failed: %s", item.question_id, arm, exc.message)
        except Exception as exc:  # noqa: BLE001 - one cell must not sink the grid
            # Observed live: Neon closed in-flight connections mid-experiment and a
            # single OperationalError killed the remaining 21 cells. A cell failure
            # of any kind becomes a recorded row; the grid continues.
            row["error"] = {
                "code": "internal_error",
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
            logger.exception("%s × %s crashed; recorded and continuing", item.question_id, arm)
            await self._await_database_recovery()
        return row

    @staticmethod
    async def _await_database_recovery(attempts: int = 6) -> None:
        """After a cell dies on a database blip, wait for the database to answer
        before starting the next cell — otherwise one outage cascades into a column
        of identical error rows that measured nothing."""
        from sqlalchemy import text

        from app.db.session import get_engine

        for attempt in range(1, attempts + 1):
            try:
                with get_engine().connect() as connection:
                    connection.execute(text("SELECT 1"))
                return
            except Exception:  # noqa: BLE001 - probing, not handling
                wait = min(5 * attempt, 30)
                logger.warning(
                    "Database not answering (attempt %d/%d); waiting %ds",
                    attempt,
                    attempts,
                    wait,
                )
                await asyncio.sleep(wait)

    async def _retrieval_metrics(self, item: BenchmarkItem, run, arm: str) -> dict:
        """P@K, R@K, nDCG@K — only where ground truth exists and the arm retrieves."""
        empty = {
            "retrieval_precision_at_k": None,
            "retrieval_recall_at_k": None,
            "retrieval_ndcg_at_k": None,
            "retrieval_k": None,
            "relevance_pairs": None,
        }
        if arm == "model_only" or not item.expected_evidence:
            return empty

        k = DEPTH_PROFILES[self.depth].top_k
        ranked = self.research.persisted_chunks_for_run(run.research_id)
        if arm in ("search_grounded", "full_autoresearch_verified"):
            # Replay the run's own deterministic evidence ranking.
            store = self.verifier._evidence_store(run.research_id, ResearchMode.SEARCH_GROUNDED)
            assert store is not None  # only MODEL_ONLY yields no store
            hits = await asyncio.to_thread(store.search, item.question, k)
            passages = [hit.text for hit in hits]
        else:
            passages = [chunk.text for chunk in ranked[:k]]
        if not passages:
            return empty

        judge = self._judge_llm or LLMService(settings=self.settings)
        try:
            judged = await judge_relevance(judge, item.expected_evidence, passages)
        finally:
            if self._judge_llm is None:
                await judge.aclose()
        if judged is None:
            return empty
        relevant, covered, raw_pairs = judged
        return {
            "retrieval_precision_at_k": formulas.precision_at_k(relevant, k),
            "retrieval_recall_at_k": formulas.recall_at_k(
                len(covered), len(item.expected_evidence)
            ),
            "retrieval_ndcg_at_k": formulas.ndcg_at_k(relevant, k),
            "retrieval_k": k,
            "relevance_pairs": raw_pairs,
        }

    async def _external_evaluation(self, item: BenchmarkItem, summary: dict) -> dict:
        """metrics-v2: judge every eligible claim against the question's external
        pool — identical pool, judge and enforcement for every arm (arm-blind by
        construction: only claim text and pool passages reach the judge).

        No cap on the development set: every extracted claim is judged. A pool or
        judging failure yields an explicit status with None metrics, never zeros.
        """
        unavailable = {
            "external_support_rate": None,
            "external_contradiction_rate": None,
            "external_judged": None,
            "external_judging_failures": None,
            "external_pool_hash": None,
        }
        claims = [
            {"claim_id": claim["claim_id"], "text": claim["text"]}
            for claim in summary["claims"]
        ]
        if not claims:
            return {**unavailable, "external": {"status": "no_claims"}}
        try:
            pool, store = await load_or_build_pool(
                item.question_id,
                item.question,
                settings=self.settings,
                store_factory=self._pool_store_factory,
                web_search=self._pool_web_search,
                academic_search=self._pool_academic_search,
            )
        except PoolUnavailable as exc:
            logger.warning("External pool unavailable for %s: %s", item.question_id, exc.reason)
            return {
                **unavailable,
                "external": {"status": "unavailable", "reason": exc.reason},
            }

        llm = self._judge_llm or LLMService(settings=self.settings)
        try:
            outcome = await judge_claims_against_pool(llm, store, claims)
        finally:
            if self._judge_llm is None:
                await llm.aclose()

        return {
            "external": {
                "status": "judged",
                "pool_version": pool.pool_version,
                "pool_hash": pool.content_hash,
                "pool_chunks": pool.chunk_count,
                "pool_sources": pool.source_count,
                **outcome,
            },
            "external_pool_hash": pool.content_hash,
            "external_support_rate": formulas.external_support_rate(outcome["verdicts"]),
            "external_contradiction_rate": formulas.external_contradiction_rate(
                outcome["verdicts"]
            ),
            "external_judged": outcome["judged"],
            "external_judging_failures": outcome["judging_failures"],
        }

    def _store_metrics(self, research_id, row: dict) -> None:
        entries = []
        for name, key in DB_METRIC_KEYS.items():
            value = row.get(name)
            if value is not None:
                entries.append({"metric_key": key, "value": float(value)})
        self.research.record_metrics(
            research_id, entries=entries, metrics_version=formulas.METRICS_VERSION
        )

    # --- re-evaluation (final analysis plan §6, Option B) ----------------------------

    #: v1 fields copied verbatim from the source rows — never recomputed.
    V1_IMPORT_FIELDS = (
        "total_claims", "verdicts", "claim_support_rate", "citation_precision",
        "citation_coverage", "unsupported_claim_rate", "citations_by_status",
        "conflicts_detected", "downgraded_verdicts", "product_latency_seconds",
        "product_total_tokens", "product_cost_usd",
    )

    async def run_reevaluation(
        self,
        benchmark_run_id: str,
        source_dir: str,
        set_name: str = "test",
        resume_dir: str | None = None,
    ) -> dict:
        """metrics-v2 over an EXISTING benchmark run's stored claims. No new product
        runs, no run-owned database writes: the source runs stay the only exposure.

        The source directory's files are hashed before and after (§6b); v1 values
        are imported verbatim from its rows (§6c); per-cell progress is written
        incrementally so a partial failure is visible and a re-run with
        `resume_dir` reuses already-judged cells instead of re-spending them (§6d).
        """
        source = Path(source_dir)
        raw_path = next(
            path for name in ("raw_results.json", "results.json")
            if (path := source / name).exists()
        )
        source_files = sorted(p for p in source.iterdir() if p.is_file())
        hashes_before = {p.name: _sha256_file(p) for p in source_files}

        source_payload = json.loads(raw_path.read_text(encoding="utf-8"))
        source_experiment = source_payload["experiment"]
        if source_experiment["benchmark_run_id"] != str(benchmark_run_id):
            raise ValueError(
                f"source directory holds benchmark run "
                f"{source_experiment['benchmark_run_id']}, not {benchmark_run_id}"
            )
        benchmark_set = load_set(set_name, self.settings)
        if source_experiment["set_fingerprint"] != benchmark_set.fingerprint:
            raise ValueError("source results were produced from a different set version")
        items_by_id = {item.question_id: item for item in benchmark_set.items}

        started_at = datetime.now(UTC)
        if resume_dir is not None:
            # D1: fail closed BEFORE any write. A resume target must be a prior
            # re-evaluation directory — never the source artifacts, never an
            # arbitrary existing directory whose files a resume would overwrite.
            output_dir = Path(resume_dir).resolve()
            source_resolved = source.resolve()
            if (
                output_dir == source_resolved
                or source_resolved in output_dir.parents
                or output_dir in source_resolved.parents
            ):
                raise ValueError(
                    "resume_dir must not be, contain, or live inside the source "
                    "directory; the source artifacts are read-only inputs"
                )
            if not (output_dir / "progress.jsonl").exists():
                raise ValueError(
                    f"resume_dir {output_dir} holds no progress.jsonl; refusing to "
                    "write into an existing directory that is not a prior "
                    "re-evaluation — omit --resume-dir to start fresh"
                )
        else:
            output_dir = _fresh_output_dir(
                self.output_root / set_name / f"{started_at:%Y%m%dT%H%M%SZ}-v2eval"
            )

        progress_path = output_dir / "progress.jsonl"
        progress = _load_progress(
            progress_path,
            benchmark_run_id=str(benchmark_run_id),
            set_fingerprint=benchmark_set.fingerprint,
        )
        if not progress_path.exists():
            _write_progress_header(
                progress_path,
                benchmark_run_id=str(benchmark_run_id),
                set_fingerprint=benchmark_set.fingerprint,
            )

        rows: list[dict] = []
        for src_row in source_payload["rows"]:
            key = f"{src_row['question_id']}|{src_row['arm']}"
            if key in progress:
                cached = progress[key]
                # D2: reuse only provenance-matched work. The header already ties
                # the file to this benchmark run; the row must also name the same
                # research run as the current source record.
                if cached.get("research_id") != src_row.get("research_id"):
                    raise ValueError(
                        f"progress for {key} was judged for research run "
                        f"{cached.get('research_id')}, but the source names "
                        f"{src_row.get('research_id')}; refusing to reuse "
                        "incompatible progress"
                    )
                rows.append(cached)  # §6d: already judged, provenance verified
                continue
            row = await self._reevaluate_cell(src_row, items_by_id)
            rows.append(row)
            _append_progress(progress_path, key, row)

        hashes_after = {p.name: _sha256_file(p) for p in source_files}
        payload = {
            "reevaluation": {
                "benchmark_run_id": str(benchmark_run_id),
                "source_dir": str(source),
                "source_file_sha256": hashes_before,
                "source_files_unchanged": hashes_before == hashes_after,
                "set": benchmark_set.name,
                "set_version": benchmark_set.version,
                "set_fingerprint": benchmark_set.fingerprint,
                "started_at": started_at.isoformat(),
                "constants": {
                    "metrics_v2_version": formulas.METRICS_V2_VERSION,
                    "external_judge": EXTERNAL_JUDGE_VERSION,
                    "pool_version": POOL_VERSION,
                    "v1_source": "imported verbatim from source rows; not recomputed",
                },
            },
            "rows": rows,
            "aggregates_v2": self._aggregate_external(rows),
            "primary_matched": self.matched_primary(rows),
        }
        write_results_json(payload, output_dir / "raw_results.json")
        _write_reeval_csv(payload, output_dir / "raw_results.csv")
        _write_reeval_report(payload, output_dir / "evaluation_report.md")
        logger.info("Re-evaluation complete; outputs in %s", output_dir)
        payload["output_dir"] = str(output_dir)
        return payload

    async def _reevaluate_cell(self, src_row: dict, items_by_id: dict) -> dict:
        row: dict[str, Any] = {
            "question_id": src_row["question_id"],
            "category": src_row.get("category"),
            "arm": src_row["arm"],
            "research_id": src_row.get("research_id"),
            "v1": {field: src_row.get(field) for field in self.V1_IMPORT_FIELDS},
            "status": "judged",
            "external_support_rate": None,
            "external_contradiction_rate": None,
            "external_judged": None,
            "external_judging_failures": None,
            "external_pool_hash": None,
        }
        if src_row.get("error"):
            row["status"] = "missing_product_cell"
            row["source_error"] = src_row["error"]
            return row
        item = items_by_id[src_row["question_id"]]
        try:
            try:
                pool, store = await load_or_build_pool(
                    item.question_id,
                    item.question,
                    settings=self.settings,
                    store_factory=self._pool_store_factory,
                    web_search=self._pool_web_search,
                    academic_search=self._pool_academic_search,
                )
            except PoolUnavailable as exc:
                row["status"] = "pool_unavailable"
                row["reason"] = exc.reason
                return row

            summary = self.research.verification_summary(
                uuid.UUID(src_row["research_id"])
            )
            claims = [
                {"claim_id": claim["claim_id"], "text": claim["text"]}
                for claim in summary["claims"]
            ]
            if not claims:
                row["status"] = "no_claims"
                return row

            llm = self._judge_llm or LLMService(settings=self.settings)
            try:
                outcome = await judge_claims_against_pool(llm, store, claims)
            finally:
                if self._judge_llm is None:
                    await llm.aclose()
            row.update(
                external={"status": "judged", "pool_version": pool.pool_version,
                          "pool_hash": pool.content_hash, **outcome},
                external_pool_hash=pool.content_hash,
                external_support_rate=formulas.external_support_rate(outcome["verdicts"]),
                external_contradiction_rate=formulas.external_contradiction_rate(
                    outcome["verdicts"]
                ),
                external_judged=outcome["judged"],
                external_judging_failures=outcome["judging_failures"],
            )
        except Exception as exc:  # noqa: BLE001 - §6d: visible, recoverable
            row["status"] = "judging_error"
            row["reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            logger.exception(
                "Re-evaluation cell %s × %s failed; recorded", row["question_id"], row["arm"]
            )
        return row

    @staticmethod
    def _aggregate_external(rows: list[dict]) -> dict:
        """All-available v2 aggregates per arm: macro AND micro, with n."""
        aggregates: dict[str, dict] = {}
        for arm in sorted({row["arm"] for row in rows}):
            arm_rows = [
                row for row in rows
                if row["arm"] == arm and row.get("external_support_rate") is not None
            ]
            support = [row["external_support_rate"] for row in arm_rows]
            contradiction = [row["external_contradiction_rate"] for row in arm_rows]
            pooled: Counter = Counter()
            for row in arm_rows:
                pooled.update(row["external"]["verdicts"])
            aggregates[arm] = {
                "eligible_questions": len(arm_rows),
                "items": sorted(row["question_id"] for row in arm_rows),
                "external_support_rate_macro": (
                    round(mean(support), 4) if support else None
                ),
                "external_contradiction_rate_macro": (
                    round(mean(contradiction), 4) if contradiction else None
                ),
                "external_support_rate_micro": formulas.external_support_rate(dict(pooled)),
                "external_contradiction_rate_micro": formulas.external_contradiction_rate(
                    dict(pooled)
                ),
                "statuses": dict(Counter(row["status"] for row in rows if row["arm"] == arm)),
            }
        return aggregates

    @staticmethod
    def matched_primary(
        rows: list[dict],
        baseline_arm: str = "model_only",
        system_arm: str = "full_autoresearch_verified",
    ) -> dict:
        """§3.1: one matched denominator for both arms — a question qualifies only
        when BOTH arms have a judged cell with at least one judged claim. Every
        exclusion is named with its per-arm reason; macro is primary, micro is the
        secondary view over the same matched set."""
        eligible: dict[str, dict[str, dict]] = {baseline_arm: {}, system_arm: {}}
        exclusions: list[dict] = []
        for row in rows:
            arm = row["arm"]
            if arm not in eligible:
                continue
            if row["status"] == "judged" and (row.get("external_judged") or 0) > 0:
                eligible[arm][row["question_id"]] = row
            else:
                reason = {
                    "missing_product_cell": "missing product cell",
                    "pool_unavailable": "pool unavailable",
                    "judging_error": "judging error",
                    "no_claims": "no extracted claims",
                }.get(row["status"], "zero judged claims")
                exclusions.append(
                    {"question_id": row["question_id"], "arm": arm, "reason": reason}
                )
        matched = sorted(set(eligible[baseline_arm]) & set(eligible[system_arm]))
        for arm in (baseline_arm, system_arm):
            for question_id in sorted(set(eligible[arm]) - set(matched)):
                exclusions.append(
                    {
                        "question_id": question_id,
                        "arm": arm,
                        "reason": "unmatched: the other arm is ineligible here",
                    }
                )

        def arm_view(arm: str) -> dict:
            arm_rows = [eligible[arm][question_id] for question_id in matched]
            pooled: Counter = Counter()
            for row in arm_rows:
                pooled.update(row["external"]["verdicts"])
            return {
                "per_question": {
                    row["question_id"]: row["external_support_rate"] for row in arm_rows
                },
                "macro": (
                    round(mean(row["external_support_rate"] for row in arm_rows), 4)
                    if arm_rows else None
                ),
                "micro": formulas.external_support_rate(dict(pooled)),
            }

        baseline_view, system_view = arm_view(baseline_arm), arm_view(system_arm)
        difference = (
            round(system_view["macro"] - baseline_view["macro"], 4)
            if baseline_view["macro"] is not None and system_view["macro"] is not None
            else None
        )
        return {
            "baseline_arm": baseline_arm,
            "system_arm": system_arm,
            "matched_questions": matched,
            "n": len(matched),
            "exclusions": exclusions,
            "baseline": baseline_view,
            "system": system_view,
            "macro_difference_system_minus_baseline": difference,
        }

    # --- outputs --------------------------------------------------------------------

    def _payload(
        self, benchmark_set: BenchmarkSet, items, arms, rows, benchmark_run_id, started_at
    ) -> dict:
        return {
            "experiment": {
                "benchmark_run_id": str(benchmark_run_id),
                "started_at": started_at.isoformat(),
                "set": benchmark_set.name,
                "set_version": benchmark_set.version,
                "set_fingerprint": benchmark_set.fingerprint,
                "items": [item.question_id for item in items],
                "arms": list(arms),
                "constants": {
                    "model": self.settings.llm_default_model,
                    "temperature": self.settings.OPENAI_TEMPERATURE,
                    "depth": self.depth,
                    "depth_profile": DEPTH_PROFILES[self.depth].__dict__,
                    "metrics_version": formulas.METRICS_VERSION,
                    "metrics_v2_version": formulas.METRICS_V2_VERSION,
                    "extraction_prompt": CLAIM_EXTRACTION_PROMPT_VERSION,
                    "verification_prompt": VERIFICATION_PROMPT_VERSION,
                    "relevance_judge": RELEVANCE_JUDGE_VERSION,
                    "external_judge": EXTERNAL_JUDGE_VERSION,
                    "pool_version": POOL_VERSION,
                },
            },
            "rows": rows,
            "aggregates": (aggregates := self._aggregate(rows, arms)),
            "comparison": self.build_comparison(aggregates),
            "comparison_matched": self._matched_comparison(rows, self._aggregate),
        }

    #: Values that are fixed by construction for an arm, not observed (audit CD-2).
    #: The comparison still classifies them mechanically; these notes stop a reader
    #: mistaking a definition for a measurement.
    STRUCTURAL_VALUES: dict[str, dict[str, str]] = {
        "model_only": {
            "claim_support_rate": (
                "structural: the arm retrieves no evidence, so no claim can verify "
                "above not_enough_evidence — 0.0 is the only possible value"
            ),
            "citation_coverage": (
                "structural: the arm's prompt forbids citations, so coverage "
                "cannot exceed 0.0"
            ),
            "unsupported_claim_rate": (
                "structural: claims citing nothing cannot be contradicted by their "
                "citations — 0.0 here is not a quality signal"
            ),
        }
    }

    #: Which direction is better, per compared metric. Needed to classify a
    #: difference as improvement or regression mechanically, from the numbers alone.
    HIGHER_IS_BETTER = {
        "claim_support_rate": True,
        "citation_precision": True,
        "citation_coverage": True,
        "unsupported_claim_rate": False,
        "retrieval_precision_at_k": True,
        "retrieval_recall_at_k": True,
        "retrieval_ndcg_at_k": True,
        "external_support_rate": True,
        "external_contradiction_rate": False,
        "product_latency_seconds": False,
        "product_total_tokens": False,
        "product_cost_usd": False,
        "failure_rate": False,
    }

    @classmethod
    def build_comparison(
        cls,
        aggregates: dict,
        baseline_arm: str = "model_only",
        system_arm: str = "full_autoresearch_verified",
    ) -> list[dict]:
        """Baseline versus AutoResearch, per metric, computed — never asserted.

        absolute_difference = autoresearch − baseline.
        relative_difference = (autoresearch − baseline) / |baseline|, only where
        meaningful: None when either value is undefined or the baseline is 0 (a
        ratio over zero asserts an infinity nobody measured).
        `direction` classifies the difference against HIGHER_IS_BETTER — improved,
        worse, or unchanged — mechanically, so no prose can soften a regression.
        """
        if baseline_arm not in aggregates or system_arm not in aggregates:
            return []
        comparison = []
        for metric, higher_better in cls.HIGHER_IS_BETTER.items():
            baseline_entry = aggregates[baseline_arm].get(metric)
            system_entry = aggregates[system_arm].get(metric)
            baseline = (
                baseline_entry.get("mean") if isinstance(baseline_entry, dict) else baseline_entry
            )
            system = (
                system_entry.get("mean") if isinstance(system_entry, dict) else system_entry
            )
            absolute = relative = direction = None
            if baseline is not None and system is not None:
                absolute = round(system - baseline, 4)
                if baseline != 0:
                    relative = round((system - baseline) / abs(baseline), 4)
                if absolute == 0:
                    direction = "unchanged"
                else:
                    improved = (absolute > 0) == higher_better
                    direction = "improved" if improved else "worse"
            comparison.append(
                {
                    "metric": metric,
                    "higher_is_better": higher_better,
                    "baseline_arm": baseline_arm,
                    "baseline": baseline,
                    "autoresearch_arm": system_arm,
                    "autoresearch": system,
                    "absolute_difference": absolute,
                    "relative_difference": relative,
                    "direction": direction,
                    "baseline_note": cls.STRUCTURAL_VALUES.get(baseline_arm, {}).get(metric),
                }
            )
        return comparison

    @classmethod
    def _matched_comparison(
        cls,
        rows: list[dict],
        aggregate,
        baseline_arm: str = "model_only",
        system_arm: str = "full_autoresearch_verified",
    ) -> dict | None:
        """The baseline-vs-AutoResearch comparison over only the items BOTH arms
        completed (audit CD-1). None when the subsets are already identical —
        the headline table then needs no companion."""
        completed = {
            arm: {row["question_id"] for row in rows if row["arm"] == arm and row["error"] is None}
            for arm in (baseline_arm, system_arm)
        }
        matched = completed[baseline_arm] & completed[system_arm]
        union = completed[baseline_arm] | completed[system_arm]
        if matched == completed[baseline_arm] == completed[system_arm]:
            return None
        subset = [
            row
            for row in rows
            if row["question_id"] in matched and row["arm"] in (baseline_arm, system_arm)
        ]
        return {
            "matched_items": sorted(matched),
            "baseline_missing": sorted(union - completed[baseline_arm]),
            "autoresearch_missing": sorted(union - completed[system_arm]),
            "comparison": cls.build_comparison(
                aggregate(subset, (baseline_arm, system_arm)), baseline_arm, system_arm
            ),
        }

    @staticmethod
    def _aggregate(rows: list[dict], arms) -> dict:
        """Per-arm means over non-null values, with n — and nothing combined."""
        mean_metrics = (
            "claim_support_rate",
            "citation_precision",
            "citation_coverage",
            "unsupported_claim_rate",
            "retrieval_precision_at_k",
            "retrieval_recall_at_k",
            "retrieval_ndcg_at_k",
            "external_support_rate",
            "external_contradiction_rate",
            "product_latency_seconds",
            "product_total_tokens",
            "product_cost_usd",
        )
        aggregates: dict[str, dict] = {}
        for arm in arms:
            arm_rows = [row for row in rows if row["arm"] == arm and row["error"] is None]
            failed = [row for row in rows if row["arm"] == arm and row["error"] is not None]
            total_cells = len(arm_rows) + len(failed)
            summary: dict[str, Any] = {
                "items_completed": len(arm_rows),
                "items_failed": len(failed),
                # failure_rate = items_failed / items_attempted, per arm.
                "failure_rate": round(len(failed) / total_cells, 4) if total_cells else None,
            }
            for metric in mean_metrics:
                values = [row[metric] for row in arm_rows if row.get(metric) is not None]
                summary[metric] = {
                    "mean": round(mean(values), 4) if values else None,
                    "n": len(values),
                }
            expected = [row for row in arm_rows if row.get("conflict_expected")]
            detected = [row for row in expected if row.get("conflicts_detected", 0) > 0]
            summary["conflict_detection_rate"] = {
                "value": formulas.conflict_detection_rate(len(detected), len(expected)),
                "detected": len(detected),
                "expected": len(expected),
            }
            aggregates[arm] = summary
        return aggregates

    def _write_outputs(self, payload: dict, set_name: str, started_at: datetime) -> Path:
        output_dir = _fresh_output_dir(
            self.output_root / set_name / f"{started_at:%Y%m%dT%H%M%SZ}"
        )
        # The final test-set evaluation uses the names the write-up names.
        final = set_name == "test"
        write_results_json(
            payload, output_dir / ("raw_results.json" if final else "results.json")
        )
        write_results_csv(
            payload, output_dir / ("raw_results.csv" if final else "results.csv")
        )
        write_summary_markdown(
            payload,
            output_dir / ("evaluation_report.md" if final else "evaluation_summary.md"),
        )
        return output_dir


def _add_usage(a: dict | None, b: dict | None) -> dict:
    a, b = a or {}, b or {}
    return {
        key: a.get(key, 0) + b.get(key, 0)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def _verdict_counts(audit) -> dict[str, int]:
    return {
        "supported": audit.supported,
        "partially_supported": audit.partially_supported,
        "unsupported": audit.unsupported,
        "contradicted": audit.contradicted,
        "not_enough_evidence": audit.insufficient_evidence,
    }


def _fresh_output_dir(base: Path) -> Path:
    """`base`, or the first `base-N` sibling that does not exist yet — created.

    Output directories are named to the second, so two runs started within the
    same second would otherwise share one: the later run's files silently
    overwrite the earlier run's artifacts, and a fresh re-evaluation would read
    a stranger's progress file and trip the provenance check. Creation doubles
    as the existence test so the check-then-create cannot race.
    """
    candidate, counter = base, 2
    while True:
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            candidate = base.with_name(f"{base.name}-{counter}")
            counter += 1


# --- re-evaluation helpers -----------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_progress_header(path: Path, *, benchmark_run_id: str, set_fingerprint: str) -> None:
    """First line of every progress file: which re-evaluation this progress
    belongs to, so a resume can verify provenance before reusing anything (D2)."""
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"meta": {"benchmark_run_id": benchmark_run_id,
                          "set_fingerprint": set_fingerprint}}
            )
            + "\n"
        )


def _load_progress(path: Path, *, benchmark_run_id: str, set_fingerprint: str) -> dict[str, dict]:
    """§6d: per-cell results persisted incrementally; a resume reuses them — but
    only after the header proves the progress belongs to this benchmark run and
    set version (D2). Incompatible progress is rejected, never silently reused."""
    progress: dict[str, dict] = {}
    if not path.exists():
        return progress
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return progress
    meta = json.loads(lines[0]).get("meta")
    if (
        not meta
        or meta.get("benchmark_run_id") != benchmark_run_id
        or meta.get("set_fingerprint") != set_fingerprint
    ):
        header_run = meta.get("benchmark_run_id") if meta else "unknown"
        raise ValueError(
            "resume_dir progress belongs to a different re-evaluation "
            f"(its header says benchmark run {header_run}); "
            "refusing to reuse incompatible progress"
        )
    for line in lines[1:]:
        entry = json.loads(line)
        # A failed cell is retried on resume; a judged one is reused.
        if entry["row"].get("status") == "judged":
            progress[entry["key"]] = entry["row"]
    return progress


def _append_progress(path: Path, key: str, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"key": key, "row": row}, default=str) + "\n")


REEVAL_CSV_COLUMNS = (
    "question_id", "category", "arm", "research_id", "status",
    "external_support_rate", "external_contradiction_rate", "external_judged",
    "external_judging_failures", "external_pool_hash",
    "v1_claim_support_rate", "v1_citation_precision", "v1_citation_coverage",
    "v1_unsupported_claim_rate",
)


def _write_reeval_csv(payload: dict, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REEVAL_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in payload["rows"]:
            flat = dict(row)
            for field in ("claim_support_rate", "citation_precision",
                          "citation_coverage", "unsupported_claim_rate"):
                flat[f"v1_{field}"] = (row.get("v1") or {}).get(field)
            writer.writerow(flat)


def _write_reeval_report(payload: dict, path: Path) -> None:
    meta = payload["reevaluation"]
    primary = payload["primary_matched"]
    lines = [
        # The title names the actual set: a development re-evaluation must never
        # wear the frozen test set's label (the stage-22 original hardcoded it).
        f"# Metrics-v2 re-evaluation of the stored {meta['set']}-set runs",
        "",
        f"- Source: `{meta['source_dir']}` (benchmark run `{meta['benchmark_run_id']}`)",
        f"- Source files unchanged during re-evaluation: **{meta['source_files_unchanged']}**",
        f"- Set: {meta['set']} v{meta['set_version']} "
        f"(fingerprint `{meta['set_fingerprint'][:12]}`)",
        "- Constants: " + ", ".join(f"{k}={v}" for k, v in meta["constants"].items()),
        "",
        "## Primary analysis (§3.1): external support, matched questions, one shared denominator",
        "",
        f"- Matched questions (n={primary['n']}): "
        f"{', '.join(primary['matched_questions']) or 'none'}",
    ]
    for exclusion in primary["exclusions"]:
        lines.append(
            f"- Excluded: {exclusion['question_id']} × {exclusion['arm']} — "
            f"{exclusion['reason']}"
        )
    lines += [
        "",
        "| Question | baseline | system |",
        "|---|---|---|",
    ]
    for question_id in primary["matched_questions"]:
        lines.append(
            f"| {question_id} | {primary['baseline']['per_question'].get(question_id)} "
            f"| {primary['system']['per_question'].get(question_id)} |"
        )
    lines += [
        f"| **macro (primary)** | {primary['baseline']['macro']} | {primary['system']['macro']} |",
        f"| micro (secondary) | {primary['baseline']['micro']} | {primary['system']['micro']} |",
        "",
        f"Macro difference (system − baseline): "
        f"**{primary['macro_difference_system_minus_baseline']}** over n={primary['n']}.",
        "",
        "## All-available v2 aggregates (secondary view; per-arm item subsets named)",
        "",
        "| Arm | n | support macro | support micro | contradiction macro | contradiction micro |",
        "|---|---|---|---|---|---|",
    ]
    for arm, aggregate in payload["aggregates_v2"].items():
        lines.append(
            f"| {arm} | {aggregate['eligible_questions']} "
            f"| {aggregate['external_support_rate_macro']} "
            f"| {aggregate['external_support_rate_micro']} "
            f"| {aggregate['external_contradiction_rate_macro']} "
            f"| {aggregate['external_contradiction_rate_micro']} |"
        )
    lines += [
        "",
        "v1 values in the rows are imported verbatim from the source files and were "
        "not recomputed. Terminology per METRICS.md: external supportability is not "
        "ground-truth factual accuracy; contradiction-vs-pool is not all "
        "hallucinations.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# --- writers (pure functions over the payload; nothing is recomputed) ----------------

CSV_COLUMNS = (
    "question_id", "category", "difficulty", "arm", "research_id", "error",
    "total_claims", "claim_support_rate", "citation_precision", "citation_coverage",
    "unsupported_claim_rate", "conflicts_detected", "conflict_expected",
    "retrieval_precision_at_k", "retrieval_recall_at_k", "retrieval_ndcg_at_k",
    "retrieval_k", "external_support_rate", "external_contradiction_rate",
    "external_judged", "external_judging_failures", "external_pool_hash",
    "downgraded_verdicts", "product_latency_seconds",
    "evaluation_latency_seconds", "product_total_tokens", "product_cost_usd",
    "evaluation_cost_usd",
)


def write_results_json(payload: dict, path: Path) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_results_csv(payload: dict, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in payload["rows"]:
            flat = dict(row)
            if flat.get("error"):
                flat["error"] = flat["error"]["code"]
            writer.writerow(flat)


def _comparison_table_rows(comparison: list[dict]) -> list[str]:
    rows = []
    for entry in comparison:
        marker = "†" if entry.get("baseline_note") else ""
        rows.append(
            "| {metric} | {better} | {baseline}{marker} | {autoresearch} | {absolute} "
            "| {relative} | {direction} |".format(
                metric=entry["metric"],
                better="higher" if entry["higher_is_better"] else "lower",
                baseline="—" if entry["baseline"] is None else entry["baseline"],
                marker=marker,
                autoresearch="—" if entry["autoresearch"] is None else entry["autoresearch"],
                absolute="—" if entry["absolute_difference"] is None
                else entry["absolute_difference"],
                relative="—" if entry["relative_difference"] is None
                else entry["relative_difference"],
                direction=entry["direction"] or "—",
            )
        )
    return rows


def write_summary_markdown(payload: dict, path: Path) -> None:
    experiment = payload["experiment"]
    aggregates = payload["aggregates"]
    arms = experiment["arms"]
    lines = [
        "# Evaluation summary",
        "",
        f"- Benchmark set: **{experiment['set']} v{experiment['set_version']}** "
        f"(fingerprint `{experiment['set_fingerprint'][:12]}`)",
        f"- Items: {len(experiment['items'])} × arms: {', '.join(arms)}",
        f"- Started: {experiment['started_at']}  ·  benchmark_run: "
        f"`{experiment['benchmark_run_id']}`",
        "- Constants: " + ", ".join(
            f"{key}={value}" for key, value in experiment["constants"].items()
            if key != "depth_profile"
        ),
        "",
        "Per-arm means over items where the metric is defined (n in parentheses). "
        "**There is deliberately no overall score**: each metric answers one "
        "question, and the formulas are in `data/benchmarks/METRICS.md` and "
        "`app/services/benchmark/metrics.py`.",
        "",
    ]

    metric_rows = (
        ("Claim support rate", "claim_support_rate"),
        ("Citation precision", "citation_precision"),
        ("Citation coverage", "citation_coverage"),
        ("Unsupported claim rate", "unsupported_claim_rate"),
        ("Retrieval precision@K", "retrieval_precision_at_k"),
        ("Retrieval recall@K", "retrieval_recall_at_k"),
        ("Retrieval nDCG@K", "retrieval_ndcg_at_k"),
        ("External support rate (pool, v2)", "external_support_rate"),
        ("External contradiction rate (pool, v2)", "external_contradiction_rate"),
        ("Product latency (s)", "product_latency_seconds"),
        ("Product tokens", "product_total_tokens"),
        ("Product cost (USD)", "product_cost_usd"),
    )
    header = "| Metric | " + " | ".join(arms) + " |"
    lines += [header, "|" + "---|" * (len(arms) + 1)]
    for label, key in metric_rows:
        cells = []
        for arm in arms:
            entry = aggregates[arm][key]
            cells.append(
                f"{entry['mean']} (n={entry['n']})" if entry["mean"] is not None else "—"
            )
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    conflict_cells = []
    for arm in arms:
        entry = aggregates[arm]["conflict_detection_rate"]
        conflict_cells.append(
            f"{entry['value']} ({entry['detected']}/{entry['expected']})"
            if entry["value"] is not None
            else "—"
        )
    lines.append("| Conflict detection rate | " + " | ".join(conflict_cells) + " |")
    completion = [
        f"{aggregates[arm]['items_completed']} ok / {aggregates[arm]['items_failed']} failed"
        for arm in arms
    ]
    lines.append("| Items | " + " | ".join(completion) + " |")
    failure = [
        str(aggregates[arm]["failure_rate"]) if aggregates[arm]["failure_rate"] is not None
        else "—"
        for arm in arms
    ]
    lines.append("| Failure rate | " + " | ".join(failure) + " |")

    comparison = payload.get("comparison") or []
    if comparison:
        lines += [
            "",
            "## Baseline vs AutoResearch",
            "",
            f"Baseline: `{comparison[0]['baseline_arm']}` · AutoResearch: "
            f"`{comparison[0]['autoresearch_arm']}`. Differences are computed, and the "
            "direction column classifies them against each metric's better direction — "
            "mechanically, so prose cannot soften a regression. Relative difference is "
            "omitted where not meaningful (undefined values, or a zero baseline).",
            "",
            "| Metric | better is | baseline | AutoResearch | abs. diff | rel. diff | direction |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += _comparison_table_rows(comparison)
        footnotes = [entry for entry in comparison if entry.get("baseline_note")]
        if footnotes:
            lines += [
                "",
                "Structural baseline values — definitions, not measurements "
                "(audit CD-2):",
                "",
            ]
            for entry in footnotes:
                lines.append(f"- † `{entry['metric']}`: {entry['baseline_note']}.")

        matched = payload.get("comparison_matched")
        if matched:
            lines += [
                "",
                "### Matched-items comparison",
                "",
                "The arms above completed different item subsets (audit CD-1) — "
                f"baseline missing: {', '.join(matched['baseline_missing']) or 'none'}; "
                f"AutoResearch missing: {', '.join(matched['autoresearch_missing']) or 'none'}. "
                f"Over the {len(matched['matched_items'])} items BOTH completed:",
                "",
                "| Metric | better is | baseline | AutoResearch | abs. diff "
                "| rel. diff | direction |",
                "|---|---|---|---|---|---|---|",
            ]
            lines += _comparison_table_rows(matched["comparison"])

    lines += [
        "",
        "Raw per-item numbers, run ids and the relevance judge's labelled pairs are "
        "in the raw results JSON; the flat table is the CSV. Nothing in this file "
        "was computed independently of those.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Run the controlled experiment.")
    parser.add_argument(
        "--set", default="development", choices=("development", "validation", "test")
    )
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--depth", default="quick", choices=tuple(DEPTH_PROFILES))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--reevaluate",
        default=None,
        metavar="BENCHMARK_RUN_ID",
        help="metrics-v2 over an existing benchmark run's stored claims; no new "
        "product runs (final analysis plan, Option B). Requires --source.",
    )
    parser.add_argument("--source", default=None, help="results directory of the source run")
    parser.add_argument("--resume-dir", default=None, help="prior re-evaluation output to resume")
    args = parser.parse_args()
    runner = ExperimentRunner(depth=args.depth)
    if args.reevaluate:
        if not args.source:
            parser.error("--reevaluate requires --source <results dir>")
        payload = await runner.run_reevaluation(
            args.reevaluate, args.source, set_name=args.set, resume_dir=args.resume_dir
        )
    else:
        if args.set == "test":
            print("NOTE: the frozen test set runs once per final configuration; see "
                  "data/benchmarks/README.md before proceeding.")
        payload = await runner.run_experiment(
            set_name=args.set, arms=tuple(args.arms.split(",")), limit=args.limit
        )
    print(f"outputs: {payload['output_dir']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_main())
