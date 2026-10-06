"""Repositories for measured metrics and the call log."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select

from app.core.constants import MetricKey, PipelineStage
from app.db.base import utcnow
from app.models import EvaluationResult, LlmCallLog, ResearchRun
from app.repositories.base import Repository


class EvaluationResultRepository(Repository[EvaluationResult]):
    model = EvaluationResult

    def record(
        self,
        *,
        run: ResearchRun,
        metric_key: MetricKey,
        value: float,
        metrics_version: str,
        numerator: int | None = None,
        denominator: int | None = None,
        unit: str | None = None,
        notes: str | None = None,
    ) -> EvaluationResult:
        """Store one metric.

        The numerator and denominator are parameters rather than optional extras
        because a ratio without its terms cannot be defended: 0.80 over five claims
        and 0.80 over five hundred are not the same finding.
        """
        result = EvaluationResult(
            research_run=run,
            metric_key=metric_key,
            value=value,
            numerator=numerator,
            denominator=denominator,
            unit=unit,
            metrics_version=metrics_version,
            notes=notes,
            computed_at=utcnow(),
        )
        self.add(result)
        self.flush()
        return result

    def list_for_run(
        self, run_id: uuid.UUID, *, metrics_version: str | None = None
    ) -> Sequence[EvaluationResult]:
        statement = select(EvaluationResult).where(EvaluationResult.research_run_id == run_id)
        if metrics_version is not None:
            statement = statement.where(EvaluationResult.metrics_version == metrics_version)
        return self.session.execute(statement).scalars().all()

    def get_metric(
        self, run_id: uuid.UUID, metric_key: MetricKey, metrics_version: str
    ) -> EvaluationResult | None:
        return self.session.execute(
            select(EvaluationResult).where(
                EvaluationResult.research_run_id == run_id,
                EvaluationResult.metric_key == metric_key,
                EvaluationResult.metrics_version == metrics_version,
            )
        ).scalar_one_or_none()


class LlmCallLogRepository(Repository[LlmCallLog]):
    model = LlmCallLog

    def record(
        self,
        *,
        stage: PipelineStage,
        model_name: str,
        run: ResearchRun | None = None,
        succeeded: bool = True,
        **values,
    ) -> LlmCallLog:
        entry = LlmCallLog(
            research_run=run,
            stage=stage,
            model=model_name,
            succeeded=succeeded,
            created_at=utcnow(),
            **values,
        )
        self.add(entry)
        self.flush()
        return entry

    def totals_for_run(self, run_id: uuid.UUID) -> dict[str, float | int]:
        """Token and cost totals, for the cost metric and for explaining a bill."""
        row = self.session.execute(
            select(
                func.coalesce(func.sum(LlmCallLog.total_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cost_usd), 0.0),
                func.count(),
            ).where(LlmCallLog.research_run_id == run_id)
        ).one()
        # COALESCE guarantees a value, but SQLAlchemy types it Optional.
        return {
            "total_tokens": int(row[0] or 0),
            "cost_usd": float(row[1] or 0.0),
            "calls": int(row[2] or 0),
        }
