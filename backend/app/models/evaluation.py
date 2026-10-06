"""
Evaluation results: one measured metric for one run.

Stored long (one row per metric) rather than wide (one column per metric). Adding
a metric then needs no migration, and the benchmark table the UI renders is
metric x configuration -- which is exactly this shape, pivoted.

`numerator` and `denominator` are kept alongside the value because "claim support
rate: 0.80" is not a reportable finding on its own. Over five claims it is weak
evidence; over five hundred it is strong. A ratio without its terms cannot be
defended, pooled across topics, or checked for an arithmetic mistake.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import MetricKey
from app.db.base import Base, UUIDPrimaryKey, enum_column

if TYPE_CHECKING:
    from app.models.research import ResearchRun


class EvaluationResult(UUIDPrimaryKey, Base):
    """One metric, measured for one run, by one version of the metrics code.

    `metrics_version` is part of the uniqueness key on purpose. If the definition
    of a metric changes, the old numbers are not wrong -- they measured something
    else. Overwriting them would quietly make an old benchmark incomparable with a
    new one; keeping both versions makes the change visible.
    """

    __tablename__ = "evaluation_result"

    research_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_run.id", ondelete="CASCADE"), nullable=False
    )

    metric_key: Mapped[MetricKey] = mapped_column(enum_column(MetricKey), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)

    # The terms behind a ratio. Null for metrics that are not ratios (cost, tokens).
    numerator: Mapped[int | None] = mapped_column(Integer)
    denominator: Mapped[int | None] = mapped_column(Integer)

    unit: Mapped[str | None] = mapped_column(String(20))  # "ratio", "usd", "tokens", "seconds"
    metrics_version: Mapped[str] = mapped_column(String(40), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    research_run: Mapped["ResearchRun"] = relationship(back_populates="evaluation_results")

    __table_args__ = (
        UniqueConstraint(
            "research_run_id",
            "metric_key",
            "metrics_version",
            name="one_value_per_metric_per_version",
        ),
        # A denominator of zero is not a measurement. If a run produced no claims,
        # the support rate is undefined and no row should exist -- recording 0.0
        # would make a run that generated nothing look maximally unreliable.
        CheckConstraint(
            "denominator IS NULL OR denominator > 0", name="denominator_is_positive"
        ),
        CheckConstraint(
            "numerator IS NULL OR numerator >= 0", name="numerator_non_negative"
        ),
        CheckConstraint(
            "numerator IS NULL OR denominator IS NULL OR numerator <= denominator",
            name="numerator_within_denominator",
        ),
        Index("ix_evaluation_result_metric", "metric_key", "metrics_version"),
    )

    def __repr__(self) -> str:
        terms = f" ({self.numerator}/{self.denominator})" if self.denominator else ""
        return f"<EvaluationResult {self.metric_key}={self.value}{terms}>"
