"""
Request and response schemas for a research run.

`ResearchRequest` is the one schema a user controls directly, so it validates
hardest. Everything it rejects is something that would otherwise waste a paid API
call or quietly corrupt a benchmark.
"""

import re
import uuid
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from app.core.constants import ResearchMode, RunStatus
from app.schemas.base import IdentifiedModel, ReadModel, TimestampedModel, WriteModel

MIN_TOPIC_LENGTH = 12
MAX_TOPIC_LENGTH = 500

# A topic has to contain letters. "1234567890123" is long enough and useless.
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


class ResearchRequest(WriteModel):
    """A request to research a topic under one configuration."""

    topic: str = Field(
        min_length=MIN_TOPIC_LENGTH,
        max_length=MAX_TOPIC_LENGTH,
        description="The research question. A question decomposes better than a keyword.",
        examples=["Does retrieval-augmented generation reduce factual errors?"],
    )
    mode: ResearchMode = Field(
        default=ResearchMode.SEARCH_GROUNDED,
        description="Which research configuration to run.",
    )

    # Per-request budget overrides, bounded so a request cannot ask for an
    # unbounded amount of paid work. Omitted means "use the configured default".
    max_subquestions: int | None = Field(default=None, ge=1, le=12)
    max_sources: int | None = Field(default=None, ge=1, le=60)

    benchmark_run_id: uuid.UUID | None = Field(
        default=None, description="Set when this run is part of a benchmark experiment."
    )

    @field_validator("topic")
    @classmethod
    def topic_must_be_substantive(cls, value: str) -> str:
        """Collapse whitespace, then insist on actual words.

        The length check alone passes a string of digits or punctuation, which
        would reach the planner and spend a model call producing nothing.
        """
        collapsed = " ".join(value.split())
        if len(collapsed) < MIN_TOPIC_LENGTH:
            raise ValueError(
                f"topic must be at least {MIN_TOPIC_LENGTH} characters once "
                "whitespace is collapsed"
            )
        if not _HAS_LETTER.search(collapsed):
            raise ValueError("topic must contain words, not only digits or punctuation")
        return collapsed

    @property
    def normalized_topic(self) -> str:
        """The grouping key for benchmark comparison across configurations."""
        return " ".join(self.topic.lower().split())


class RunConfigurationCreate(WriteModel):
    """The settings a run will execute under.

    Validated against the mode, because a configuration that names a chunk size
    for `model_only` is not a harmless extra: it implies retrieval happened, and
    would make the fingerprint of a no-retrieval run depend on retrieval settings
    that never applied.
    """

    mode: ResearchMode
    model: str = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    embedding_model: str | None = Field(default=None, max_length=200)
    chunk_size: int | None = Field(default=None, ge=100, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4000)
    top_k: int | None = Field(default=None, ge=1, le=50)

    max_subquestions: int = Field(ge=1, le=12)
    max_sources: int = Field(ge=1, le=60)

    planner_prompt_version: str | None = Field(default=None, max_length=40)
    synthesizer_prompt_version: str | None = Field(default=None, max_length=40)
    claim_prompt_version: str | None = Field(default=None, max_length=40)
    verifier_prompt_version: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def retrieval_settings_match_the_mode(self) -> "RunConfigurationCreate":
        retrieval_fields = {
            "embedding_model": self.embedding_model,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "top_k": self.top_k,
        }
        set_fields = {name for name, value in retrieval_fields.items() if value is not None}

        if self.mode is ResearchMode.MODEL_ONLY:
            if set_fields:
                raise ValueError(
                    "model_only performs no retrieval, so it must not set "
                    f"{', '.join(sorted(set_fields))}"
                )
        else:
            missing = {name for name, value in retrieval_fields.items() if value is None}
            if missing:
                raise ValueError(
                    f"{self.mode.value} retrieves, so it requires "
                    f"{', '.join(sorted(missing))}"
                )

        if (
            self.chunk_overlap is not None
            and self.chunk_size is not None
            and self.chunk_overlap >= self.chunk_size
        ):
            raise ValueError("chunk_overlap must be smaller than chunk_size")

        return self

    @property
    def fingerprint(self) -> str:
        """Content hash of this configuration, matching the ORM's computation."""
        from app.models.research import RunConfiguration

        return RunConfiguration.compute_fingerprint(self.model_dump())


class RunConfigurationRead(IdentifiedModel):
    fingerprint: str
    mode: ResearchMode
    model: str
    temperature: float
    embedding_model: str | None
    chunk_size: int | None
    chunk_overlap: int | None
    top_k: int | None
    max_subquestions: int
    max_sources: int
    planner_prompt_version: str | None
    synthesizer_prompt_version: str | None
    claim_prompt_version: str | None
    verifier_prompt_version: str | None
    created_at: datetime


class SubQuestionRead(IdentifiedModel):
    position: int
    text: str
    search_query: str | None
    addressed: bool | None


class ResearchRunSummary(TimestampedModel):
    """A run without its contents. What a list endpoint returns."""

    topic: str
    status: RunStatus
    configuration_id: uuid.UUID
    benchmark_run_id: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()


class ResearchRunRead(ResearchRunSummary):
    """A run with its plan and configuration expanded."""

    normalized_topic: str
    error_message: str | None
    configuration: RunConfigurationRead
    subquestions: list[SubQuestionRead] = Field(default_factory=list)


class ResearchResponse(ReadModel):
    """The response to creating or fetching a research run.

    A wrapper rather than the run alone, so that progress and the links to the
    other views have an obvious home as the pipeline is built out -- and so adding
    one does not change the shape of `run`.
    """

    run: ResearchRunRead
    report_id: uuid.UUID | None = Field(
        default=None, description="Null until the run has produced a report."
    )
    claim_count: int = Field(default=0, ge=0)
    source_count: int = Field(default=0, ge=0)
    conflict_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def counts_require_a_report(self) -> "ResearchResponse":
        """Claims come from a report, so claims without one is a contradiction."""
        if self.report_id is None and self.claim_count:
            raise ValueError("claim_count must be 0 when there is no report")
        return self


class BenchmarkRunRead(TimestampedModel):
    name: str
    topic_set: str
    topic_set_version: str
    notes: str | None
    completed_at: datetime | None
