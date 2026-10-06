"""
Schemas for measured metrics and the benchmark comparison.

The validator here guards against the single most embarrassing failure a project
like this can have: reporting a ratio that its own numerator and denominator do
not support.
"""

import uuid
from datetime import datetime

from pydantic import Field, model_validator

from app.core.constants import MetricKey, ResearchMode
from app.schemas.base import IdentifiedModel, ReadModel, WriteModel

# Floating-point division will not reproduce a stored value exactly.
RATIO_TOLERANCE = 1e-6


class EvaluationResultCreate(WriteModel):
    """One metric measured for one run."""

    research_run_id: uuid.UUID
    metric_key: MetricKey
    value: float
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=20)
    metrics_version: str = Field(min_length=1, max_length=40)
    notes: str | None = None

    @model_validator(mode="after")
    def value_must_follow_from_its_terms(self) -> "EvaluationResultCreate":
        """Three checks, each closing a way to publish a wrong number.

        A ratio outside [0, 1] is arithmetically impossible. A numerator larger
        than its denominator is a counting bug. And a value that does not equal
        numerator / denominator means the stored figure and its justification have
        drifted apart -- which is exactly the kind of error nobody notices in a
        results table.
        """
        if self.metric_key.is_ratio and not 0.0 <= self.value <= 1.0:
            raise ValueError(
                f"{self.metric_key.value} is a ratio, so its value must be in [0, 1], "
                f"got {self.value}"
            )

        if self.numerator is not None and self.denominator is not None:
            if self.numerator > self.denominator:
                raise ValueError(
                    f"numerator ({self.numerator}) cannot exceed denominator "
                    f"({self.denominator})"
                )
            if self.metric_key.is_ratio:
                expected = self.numerator / self.denominator
                if abs(expected - self.value) > RATIO_TOLERANCE:
                    raise ValueError(
                        f"value {self.value} does not match "
                        f"{self.numerator}/{self.denominator} = {expected:.6f}"
                    )

        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("numerator and denominator must be given together")

        if self.value < 0 and self.metric_key is not MetricKey.WALL_CLOCK_SECONDS:
            raise ValueError(f"{self.metric_key.value} cannot be negative")

        return self


class EvaluationResultRead(IdentifiedModel):
    research_run_id: uuid.UUID
    metric_key: MetricKey
    value: float
    numerator: int | None
    denominator: int | None
    unit: str | None
    metrics_version: str
    notes: str | None
    computed_at: datetime

    @property
    def terms(self) -> str | None:
        """"4/5", for showing a ratio alongside the evidence for it."""
        if self.numerator is None or self.denominator is None:
            return None
        return f"{self.numerator}/{self.denominator}"


class ConfigurationMetrics(ReadModel):
    """Every metric for one configuration, in one benchmark."""

    configuration_id: uuid.UUID
    mode: ResearchMode
    fingerprint: str
    run_count: int = Field(ge=0)
    metrics: dict[MetricKey, float] = Field(default_factory=dict)


class BenchmarkComparison(ReadModel):
    """The project's headline result: the same work under three configurations.

    `comparable` is computed, not asserted. A comparison across runs that used
    different topic sets or different prompt versions is not a result, and the
    response says so rather than letting a reader assume otherwise.
    """

    benchmark_run_id: uuid.UUID
    name: str
    topic_set: str
    topic_set_version: str
    metrics_version: str
    configurations: list[ConfigurationMetrics] = Field(default_factory=list)
    comparable: bool = True
    incomparable_reason: str | None = None

    @model_validator(mode="after")
    def flag_an_unbalanced_comparison(self) -> "BenchmarkComparison":
        """Configurations with different run counts cannot be compared directly.

        If `model_only` ran 10 topics and `hybrid` ran 7, the difference between
        their numbers includes the difference in what they were asked. Marking it
        is the honest response; silently printing the table is not.
        """
        counts = {c.run_count for c in self.configurations}
        if len(counts) > 1:
            object.__setattr__(self, "comparable", False)
            detail = ", ".join(f"{c.mode.value}={c.run_count}" for c in self.configurations)
            object.__setattr__(
                self,
                "incomparable_reason",
                f"configurations completed different numbers of runs ({detail})",
            )
        return self
