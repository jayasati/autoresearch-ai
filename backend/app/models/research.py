"""
The run itself: configuration, benchmark grouping, the run, and its plan.

`RunConfiguration` is a separate table rather than columns on the run, because
the benchmark's entire validity rests on knowing that two runs differed *only*
in the thing under test. A configuration row is immutable and content-addressed,
so "same configuration" is a fact you can join on, not a claim.
"""

import hashlib
import json
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

from app.core.constants import ResearchMode, RunStatus
from app.db.base import Base, Timestamped, UUIDPrimaryKey, enum_column

if TYPE_CHECKING:
    from app.models.evaluation import EvaluationResult
    from app.models.evidence import Conflict
    from app.models.observability import LlmCallLog
    from app.models.report import Report
    from app.models.source import ResearchSource, Source


class RunConfiguration(UUIDPrimaryKey, Base):
    """An immutable, content-addressed snapshot of everything that shapes a run.

    The `fingerprint` is a hash of the fields below. Two runs with the same
    fingerprint are genuinely comparable; two with different fingerprints are not,
    whatever their labels say. This is what makes a benchmark result defensible
    rather than anecdotal -- including the prompt versions, since changing a prompt
    changes the output and must therefore change the configuration.
    """

    __tablename__ = "run_configuration"

    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    mode: Mapped[ResearchMode] = mapped_column(enum_column(ResearchMode), nullable=False)

    # Foundation model
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Retrieval -- null for model_only, where none of it applies
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    chunk_size: Mapped[int | None] = mapped_column(Integer)
    chunk_overlap: Mapped[int | None] = mapped_column(Integer)
    top_k: Mapped[int | None] = mapped_column(Integer)

    # Budgets
    max_subquestions: Mapped[int] = mapped_column(Integer, nullable=False)
    max_sources: Mapped[int] = mapped_column(Integer, nullable=False)

    # Prompt versions: a changed prompt is a changed configuration
    planner_prompt_version: Mapped[str | None] = mapped_column(String(40))
    synthesizer_prompt_version: Mapped[str | None] = mapped_column(String(40))
    claim_prompt_version: Mapped[str | None] = mapped_column(String(40))
    verifier_prompt_version: Mapped[str | None] = mapped_column(String(40))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    runs: Mapped[list["ResearchRun"]] = relationship(back_populates="configuration")

    __table_args__ = (
        CheckConstraint("temperature >= 0 AND temperature <= 2", name="temperature_range"),
        CheckConstraint("max_subquestions > 0", name="max_subquestions_positive"),
        CheckConstraint("max_sources > 0", name="max_sources_positive"),
        CheckConstraint(
            "chunk_overlap IS NULL OR chunk_size IS NULL OR chunk_overlap < chunk_size",
            name="overlap_smaller_than_chunk",
        ),
    )

    FINGERPRINT_FIELDS = (
        "mode",
        "model",
        "temperature",
        "embedding_model",
        "chunk_size",
        "chunk_overlap",
        "top_k",
        "max_subquestions",
        "max_sources",
        "planner_prompt_version",
        "synthesizer_prompt_version",
        "claim_prompt_version",
        "verifier_prompt_version",
    )

    @classmethod
    def compute_fingerprint(cls, values: dict) -> str:
        """Deterministic hash of the configuration.

        Sorted keys and a canonical JSON encoding, so the same configuration always
        hashes the same way regardless of insertion order or Python version.
        """
        payload = {
            key: (str(values[key]) if values.get(key) is not None else None)
            for key in cls.FINGERPRINT_FIELDS
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return f"<RunConfiguration {self.mode} {self.fingerprint[:12]}>"


class BenchmarkRun(UUIDPrimaryKey, Timestamped, Base):
    """One execution of the topic set across every configuration under comparison.

    Groups the `ResearchRun` rows that make up a single experiment, so a
    comparison table can never accidentally mix runs from different experiments.
    """

    __tablename__ = "benchmark_run"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    topic_set: Mapped[str] = mapped_column(String(200), nullable=False)
    topic_set_version: Mapped[str] = mapped_column(String(40), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    runs: Mapped[list["ResearchRun"]] = relationship(
        back_populates="benchmark_run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<BenchmarkRun {self.name!r} {self.topic_set}@{self.topic_set_version}>"


class ResearchRun(UUIDPrimaryKey, Timestamped, Base):
    """A single research request and everything produced for it.

    The root of the traceability graph: every claim, source, chunk, verdict and
    metric in the system reaches this row by following foreign keys.
    """

    __tablename__ = "research_run"

    topic: Mapped[str] = mapped_column(Text, nullable=False)

    # Lower-cased, whitespace-collapsed topic. Benchmark comparison groups on
    # this, so "RAG and hallucination" and "rag and  hallucination" are one topic.
    normalized_topic: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus), nullable=False, default=RunStatus.PENDING, index=True
    )

    configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_configuration.id", ondelete="RESTRICT"), nullable=False
    )
    benchmark_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("benchmark_run.id", ondelete="CASCADE")
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Populated only when status is FAILED. `error_code` reuses the API's stable
    # code vocabulary, so a failed run can be told apart from a failed *provider*.
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)

    configuration: Mapped[RunConfiguration] = relationship(back_populates="runs")
    benchmark_run: Mapped[BenchmarkRun | None] = relationship(back_populates="runs")

    subquestions: Mapped[list["SubQuestion"]] = relationship(
        back_populates="research_run",
        cascade="all, delete-orphan",
        order_by="SubQuestion.position",
    )
    research_sources: Mapped[list["ResearchSource"]] = relationship(
        back_populates="research_run", cascade="all, delete-orphan"
    )
    # One report per run: a run either produced one or failed before doing so.
    report: Mapped["Report | None"] = relationship(
        back_populates="research_run", cascade="all, delete-orphan", uselist=False
    )
    conflicts: Mapped[list["Conflict"]] = relationship(
        back_populates="research_run", cascade="all, delete-orphan"
    )
    evaluation_results: Mapped[list["EvaluationResult"]] = relationship(
        back_populates="research_run", cascade="all, delete-orphan"
    )
    llm_calls: Mapped[list["LlmCallLog"]] = relationship(
        back_populates="research_run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completed_after_started",
        ),
        # A failure must say why. Silent failed runs would corrupt the benchmark.
        CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL",
            name="failed_runs_have_an_error_code",
        ),
        Index("ix_research_run_benchmark_configuration", "benchmark_run_id", "configuration_id"),
    )

    @staticmethod
    def normalize_topic(topic: str) -> str:
        return " ".join(topic.lower().split())

    @property
    def sources(self) -> list["Source"]:
        """The sources this run retrieved, through the association rows."""
        return [link.source for link in self.research_sources]

    def __repr__(self) -> str:
        return f"<ResearchRun {self.topic[:40]!r} status={self.status}>"


class SubQuestion(UUIDPrimaryKey, Base):
    """One decomposed question from the planning step.

    Exists as a table because the coverage metric is "how many planned
    sub-questions did the report actually address" -- which is unanswerable unless
    the plan is persisted alongside the result.
    """

    __tablename__ = "subquestion"

    research_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_run.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # The query actually sent to a search backend, which is often not the
    # sub-question verbatim. Kept so a retrieval failure can be reproduced.
    search_query: Mapped[str | None] = mapped_column(Text)

    # Set during evaluation, not planning.
    addressed: Mapped[bool | None] = mapped_column()

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    research_run: Mapped[ResearchRun] = relationship(back_populates="subquestions")
    research_sources: Mapped[list["ResearchSource"]] = relationship(back_populates="subquestion")

    __table_args__ = (
        UniqueConstraint("research_run_id", "position", name="uq_subquestion_position_per_run"),
        CheckConstraint("position >= 0", name="position_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<SubQuestion {self.position}: {self.text[:40]!r}>"
