"""
Repositories for runs, configurations and sources.

Each method here exists because a caller needs it, not for symmetry. The two that
earn their place most clearly are `get_or_create` on configurations and sources —
both implement deduplication by content fingerprint, which several metrics depend on
and which is easy to get subtly wrong in a handler.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.constants import EvidenceDepth, ResearchMode, RunStatus
from app.db.base import utcnow
from app.models import (
    BenchmarkRun,
    ResearchRun,
    ResearchSource,
    RunConfiguration,
    Source,
    SubQuestion,
)
from app.repositories.base import Repository


class RunConfigurationRepository(Repository[RunConfiguration]):
    model = RunConfiguration

    def get_by_fingerprint(self, fingerprint: str) -> RunConfiguration | None:
        return self.session.execute(
            select(RunConfiguration).where(RunConfiguration.fingerprint == fingerprint)
        ).scalar_one_or_none()

    def get_or_create(self, **values) -> tuple[RunConfiguration, bool]:
        """Return the configuration matching these settings, creating it if new.

        Deduplicated by fingerprint, because two rows for one configuration would
        split a benchmark group in half and make the comparison wrong rather than
        merely untidy. Returns `(configuration, created)` so a caller can tell
        whether it is reusing an existing experiment's settings.
        """
        fingerprint = RunConfiguration.compute_fingerprint(values)
        existing = self.get_by_fingerprint(fingerprint)
        if existing is not None:
            return existing, False

        configuration = RunConfiguration(
            **values, fingerprint=fingerprint, created_at=utcnow()
        )
        self.add(configuration)
        self.flush()  # surface a race on the unique index now, not at commit
        return configuration, True


class SourceRepository(Repository[Source]):
    model = Source

    def get_by_fingerprint(self, fingerprint: str) -> Source | None:
        return self.session.execute(
            select(Source).where(Source.fingerprint == fingerprint)
        ).scalar_one_or_none()

    def get_or_create(self, fingerprint: str | None, **values) -> tuple[Source, bool]:
        """Find or create a source, deduplicated across *all* runs.

        Sources are global on purpose: two runs citing the same paper must reference
        one row, or source diversity cannot be computed and the same paper could be
        judged fabricated in one run and valid in another.

        A null fingerprint means an unidentifiable source — a model assertion with no
        external reference — which cannot be deduplicated and is always a new row.
        """
        if fingerprint is not None:
            existing = self.get_by_fingerprint(fingerprint)
            if existing is not None:
                return existing, False

        source = Source(
            **values,
            fingerprint=fingerprint,
            first_retrieved_at=values.pop("first_retrieved_at", utcnow()),
        )
        self.add(source)
        self.flush()
        return source, True

    def link_to_run(
        self,
        run: ResearchRun,
        source: Source,
        *,
        citation_label: str | None = None,
        evidence_depth: EvidenceDepth = EvidenceDepth.SNIPPET,
        retriever: str | None = None,
        retrieval_query: str | None = None,
        rank: int | None = None,
        subquestion_id: uuid.UUID | None = None,
    ) -> ResearchSource:
        """Record that this run retrieved this source, and how deeply.

        The per-run facts live on the association row rather than the source,
        because they differ between runs — one run may read full text where another
        saw only an abstract.
        """
        link = ResearchSource(
            research_run=run,
            source=source,
            citation_label=citation_label,
            evidence_depth=evidence_depth,
            retriever=retriever,
            retrieval_query=retrieval_query,
            rank=rank,
            subquestion_id=subquestion_id,
            created_at=utcnow(),
        )
        self.session.add(link)
        return link


class ResearchRunRepository(Repository[ResearchRun]):
    model = ResearchRun

    def create(
        self,
        *,
        topic: str,
        configuration: RunConfiguration,
        benchmark_run: BenchmarkRun | None = None,
        status: RunStatus = RunStatus.PENDING,
    ) -> ResearchRun:
        """Create a run in `pending`. Does not commit.

        `normalized_topic` is derived here rather than left to the caller, since it
        is the key benchmark comparison groups on — if two callers normalised
        differently, the same topic would split into two groups.
        """
        run = ResearchRun(
            topic=topic,
            normalized_topic=ResearchRun.normalize_topic(topic),
            status=status,
            configuration=configuration,
            benchmark_run=benchmark_run,
        )
        self.add(run)
        self.flush()
        return run

    def get_with_relations(self, run_id: uuid.UUID) -> ResearchRun | None:
        """Load a run with the collections a detail view needs, in one round trip.

        `selectinload` rather than lazy access: rendering a run touches its
        configuration, plan, sources and report, and lazy loading would issue a
        query per collection per run — the N+1 that makes a list endpoint slow once
        there is real data.
        """
        return self.session.execute(
            select(ResearchRun)
            .where(ResearchRun.id == run_id)
            .options(
                selectinload(ResearchRun.configuration),
                selectinload(ResearchRun.subquestions),
                selectinload(ResearchRun.research_sources).selectinload(ResearchSource.source),
                selectinload(ResearchRun.report),
                selectinload(ResearchRun.evaluation_results),
            )
        ).scalar_one_or_none()

    def list_recent(self, *, limit: int = 20, offset: int = 0) -> Sequence[ResearchRun]:
        return (
            self.session.execute(
                select(ResearchRun)
                .order_by(ResearchRun.created_at.desc())
                .limit(limit)
                .offset(offset)
                .options(selectinload(ResearchRun.configuration))
            )
            .scalars()
            .all()
        )

    def list_by_status(self, status: RunStatus, *, limit: int = 50) -> Sequence[ResearchRun]:
        return (
            self.session.execute(
                select(ResearchRun).where(ResearchRun.status == status).limit(limit)
            )
            .scalars()
            .all()
        )

    def list_for_topic_and_mode(
        self, normalized_topic: str, mode: ResearchMode
    ) -> Sequence[ResearchRun]:
        """Every run of one topic under one mode. The benchmark's unit of comparison."""
        return (
            self.session.execute(
                select(ResearchRun)
                .join(ResearchRun.configuration)
                .where(
                    ResearchRun.normalized_topic == normalized_topic,
                    RunConfiguration.mode == mode,
                )
            )
            .scalars()
            .all()
        )

    def mark_started(self, run: ResearchRun, status: RunStatus = RunStatus.PLANNING) -> ResearchRun:
        run.status = status
        run.started_at = run.started_at or utcnow()
        return run

    def mark_status(self, run: ResearchRun, status: RunStatus) -> ResearchRun:
        run.status = status
        return run

    def mark_completed(self, run: ResearchRun, when: datetime | None = None) -> ResearchRun:
        run.status = RunStatus.COMPLETED
        run.completed_at = when or utcnow()
        return run

    def mark_failed(
        self, run: ResearchRun, *, code: str, message: str | None = None
    ) -> ResearchRun:
        """Record a failure with its cause.

        `code` is required because the database requires it: a failed run with no
        reason would be indistinguishable from a run that produced nothing, and
        would be counted as such by every metric over the set.
        """
        run.status = RunStatus.FAILED
        run.error_code = code
        run.error_message = message
        run.completed_at = run.completed_at or utcnow()
        return run

    def add_subquestions(self, run: ResearchRun, texts: Sequence[str]) -> list[SubQuestion]:
        """Persist the plan. Positions are assigned here, so they cannot collide."""
        questions = [
            SubQuestion(research_run=run, position=index, text=text, created_at=utcnow())
            for index, text in enumerate(texts)
        ]
        self.session.add_all(questions)
        return questions


class BenchmarkRunRepository(Repository[BenchmarkRun]):
    model = BenchmarkRun

    def create(
        self, *, name: str, topic_set: str, topic_set_version: str, notes: str | None = None
    ) -> BenchmarkRun:
        benchmark = BenchmarkRun(
            name=name,
            topic_set=topic_set,
            topic_set_version=topic_set_version,
            notes=notes,
        )
        self.add(benchmark)
        self.flush()
        return benchmark
