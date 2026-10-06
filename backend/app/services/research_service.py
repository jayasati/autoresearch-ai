"""
The research service: use cases, and the transaction boundary.

This is the layer that answers "what happened, and did all of it happen". Each
public method is one atomic operation: it either completes and commits, or raises and
leaves the database exactly as it was.

Why that boundary lives here and not lower down: a research run produces a connected
graph — a report, its sections, its claims, each claim's evidence, citations and
verdict. Persisting that in pieces, each committed as it is built, means a failure
partway through leaves a run whose claim set is silently incomplete. Every metric
computed over it would then be wrong in a way that looks like a finding. One
transaction per operation makes partial state impossible rather than unlikely.

No LLM calls and no retrieval here — this service persists what those stages will
eventually produce.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.constants import ResearchMode, RunStatus
from app.core.exceptions import ConflictError, NotFoundError
from app.db.session import unit_of_work
from app.models import ResearchRun, RunConfiguration
from app.repositories import (
    BenchmarkRunRepository,
    ClaimRepository,
    ReportRepository,
    ResearchRunRepository,
    RunConfigurationRepository,
    SourceRepository,
)
from app.schemas.research import ResearchRequest

logger = logging.getLogger(__name__)


class ResearchService:
    """Use cases over the research graph.

    Constructed with a session when joining a caller's transaction, or without one
    to manage its own. Every write method is wrapped in `unit_of_work`, which joins
    an existing transaction rather than opening a second — so a service method is
    safe to call standalone *or* as one step of a larger operation, and in the
    second case it does not commit work the caller may still abandon.
    """

    def __init__(self, session: Session | None = None, settings: Settings | None = None) -> None:
        self._session = session
        self.settings = settings or get_settings()

    # --- configuration -------------------------------------------------------

    def configuration_values_for(self, request: ResearchRequest) -> dict:
        """Turn a request plus the current settings into a configuration.

        Retrieval settings are included only for the modes that retrieve. A chunk
        size on a `model_only` run would imply retrieval that never happened, and
        would make the fingerprint — and therefore comparability — depend on
        settings that never applied.
        """
        values: dict = {
            "mode": request.mode,
            "model": self.settings.OPENAI_MODEL,
            "temperature": 0.0,
            "max_subquestions": request.max_subquestions or self.settings.MAX_SUBQUESTIONS,
            "max_sources": request.max_sources or self.settings.MAX_SOURCES_PER_RUN,
        }
        if request.mode is not ResearchMode.MODEL_ONLY:
            values |= {
                "embedding_model": self.settings.EMBEDDING_MODEL,
                "chunk_size": self.settings.CHUNK_SIZE,
                "chunk_overlap": self.settings.CHUNK_OVERLAP,
                "top_k": self.settings.TOP_K,
            }
        return values

    def resolve_configuration(
        self, request: ResearchRequest, *, session: Session | None = None
    ) -> tuple[RunConfiguration, bool]:
        """Find or create the configuration for a request."""
        with unit_of_work(session or self._session) as active:
            return RunConfigurationRepository(active).get_or_create(
                **self.configuration_values_for(request)
            )

    # --- creating a run ------------------------------------------------------

    def create_run(self, request: ResearchRequest) -> ResearchRun:
        """Create a research run in `pending`, with its configuration and plan slot.

        One transaction covering both the configuration and the run. Committing the
        configuration separately would leave an orphan settings row behind whenever
        run creation failed — harmless individually, but it accumulates and makes
        "which configurations has this project actually used" unanswerable.
        """
        values = self.configuration_values_for(request)

        with unit_of_work(self._session) as session:
            configurations = RunConfigurationRepository(session)
            runs = ResearchRunRepository(session)

            configuration, created = configurations.get_or_create(**values)

            benchmark = None
            if request.benchmark_run_id is not None:
                benchmark = BenchmarkRunRepository(session).get(request.benchmark_run_id)
                if benchmark is None:
                    raise NotFoundError(
                        f"No benchmark_run with id {request.benchmark_run_id}.",
                        details={"benchmark_run_id": str(request.benchmark_run_id)},
                    )

            run = runs.create(
                topic=request.topic, configuration=configuration, benchmark_run=benchmark
            )

            logger.info(
                "Created research run %s (mode=%s, configuration=%s%s)",
                run.id,
                configuration.mode.value,
                configuration.fingerprint[:12],
                ", new" if created else "",
            )
            return run

    # --- reading -------------------------------------------------------------

    def get_run(self, run_id: uuid.UUID) -> ResearchRun:
        """A run with its configuration, plan, sources and report loaded."""
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_with_relations(run_id)
            if run is None:
                raise NotFoundError(
                    f"No research run with id {run_id}.", details={"run_id": str(run_id)}
                )
            return run

    def list_runs(self, *, limit: int = 20, offset: int = 0) -> Sequence[ResearchRun]:
        with unit_of_work(self._session) as session:
            return ResearchRunRepository(session).list_recent(limit=limit, offset=offset)

    def run_summary(self, run_id: uuid.UUID) -> dict:
        """Counts for the response wrapper, computed rather than cached.

        Cached counters drift the moment anything writes outside the service; a
        count over the rows cannot.
        """
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_with_relations(run_id)
            if run is None:
                raise NotFoundError(f"No research run with id {run_id}.")
            report = ReportRepository(session).get_for_run(run_id)
            claim_count = (
                ClaimRepository(session).count_for_report(report.id) if report else 0
            )
            return {
                "report_id": report.id if report else None,
                "claim_count": claim_count,
                "source_count": len(run.research_sources),
                "conflict_count": len(run.conflicts),
            }

    # --- status transitions --------------------------------------------------

    # Which transitions are legal. A run cannot go back to pending, and a completed
    # or failed run is final: re-running a topic creates a new run, which is what
    # keeps a benchmark reproducible instead of letting a result be edited.
    ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
        RunStatus.PENDING: frozenset({RunStatus.PLANNING, RunStatus.FAILED}),
        RunStatus.PLANNING: frozenset({RunStatus.RETRIEVING, RunStatus.DRAFTING, RunStatus.FAILED}),
        RunStatus.RETRIEVING: frozenset({RunStatus.DRAFTING, RunStatus.FAILED}),
        RunStatus.DRAFTING: frozenset({RunStatus.VERIFYING, RunStatus.COMPLETED, RunStatus.FAILED}),
        RunStatus.VERIFYING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED}),
        RunStatus.COMPLETED: frozenset(),
        RunStatus.FAILED: frozenset(),
    }

    def advance_status(self, run_id: uuid.UUID, status: RunStatus) -> ResearchRun:
        """Move a run to the next status, refusing an illegal transition.

        Enforced in the service rather than left to callers, because a run that goes
        `completed` → `retrieving` would produce a second report for the same run and
        break the one-report-per-run invariant from the far side.
        """
        with unit_of_work(self._session) as session:
            runs = ResearchRunRepository(session)
            run = runs.get_or_raise(run_id)

            if status not in self.ALLOWED_TRANSITIONS[run.status]:
                raise ConflictError(
                    f"Cannot move a run from '{run.status.value}' to '{status.value}'.",
                    details={
                        "run_id": str(run_id),
                        "from": run.status.value,
                        "to": status.value,
                        "allowed": sorted(s.value for s in self.ALLOWED_TRANSITIONS[run.status]),
                    },
                )

            if run.status is RunStatus.PENDING:
                runs.mark_started(run, status)
            elif status is RunStatus.COMPLETED:
                runs.mark_completed(run)
            else:
                runs.mark_status(run, status)

            logger.info("Run %s -> %s", run_id, status.value)
            return run

    def fail_run(self, run_id: uuid.UUID, *, code: str, message: str | None = None) -> ResearchRun:
        """Mark a run failed, with its cause.

        `code` reuses the API's stable error vocabulary, so a run that failed because
        a provider was down (`external_service_error`) stays distinguishable from one
        that failed because our logic is wrong (`internal_error`). The benchmark must
        not conflate them.
        """
        with unit_of_work(self._session) as session:
            runs = ResearchRunRepository(session)
            run = runs.get_or_raise(run_id)
            runs.mark_failed(run, code=code, message=message)
            logger.warning("Run %s failed: %s (%s)", run_id, code, message or "no detail")
            return run

    def record_plan(self, run_id: uuid.UUID, subquestions: Sequence[str]) -> ResearchRun:
        """Persist the plan for a run, all of it or none.

        Partially recorded sub-questions would make coverage — addressed divided by
        planned — wrong in the project's favour, since the denominator would be too
        small.
        """
        with unit_of_work(self._session) as session:
            runs = ResearchRunRepository(session)
            run = runs.get_or_raise(run_id)
            if run.subquestions:
                raise ConflictError(
                    f"Run {run_id} already has a plan.",
                    details={"run_id": str(run_id), "existing": len(run.subquestions)},
                )
            runs.add_subquestions(run, subquestions)
            return run

    # --- sources -------------------------------------------------------------

    def attach_source(
        self,
        run_id: uuid.UUID,
        *,
        fingerprint: str | None,
        citation_label: str | None = None,
        **source_values,
    ):
        """Deduplicate a source and link it to a run, in one transaction.

        Both halves must land together: a source row with no link is invisible to the
        run that found it, and a link to a source that was never created is
        impossible. Doing them in separate transactions would allow the first.
        """
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_or_raise(run_id)
            sources = SourceRepository(session)
            source, created = sources.get_or_create(fingerprint, **source_values)
            link = sources.link_to_run(run, source, citation_label=citation_label)
            logger.info(
                "Attached source %s to run %s (%s)",
                source.id,
                run_id,
                "new" if created else "deduplicated",
            )
            return source, link
