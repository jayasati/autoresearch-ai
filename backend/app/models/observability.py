"""
The LLM call log.

Not an afterthought and not optional. Without a per-call record of model, prompt
version, tokens and cost, neither a benchmark result nor an API bill can be
explained after the fact -- and "this configuration is better" is only a finding if
you can also say what it cost.

It is also the only place a *failed* provider call is recorded, which is what keeps
"the run failed because Tavily was down" distinguishable from "the run failed
because our logic is wrong".
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import PipelineStage
from app.db.base import Base, UUIDPrimaryKey, enum_column

if TYPE_CHECKING:
    from app.models.research import ResearchRun


class LlmCallLog(UUIDPrimaryKey, Base):
    """One call to the foundation model.

    Nullable `research_run_id`: some calls (a connectivity check, an ad-hoc
    evaluation) belong to no run, and they still cost money.
    """

    __tablename__ = "llm_call_log"

    research_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_run.id", ondelete="CASCADE")
    )

    stage: Mapped[PipelineStage] = mapped_column(enum_column(PipelineStage), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    temperature: Mapped[float | None] = mapped_column(Float)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)

    latency_ms: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(40))

    succeeded: Mapped[bool] = mapped_column(nullable=False, default=True)
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)

    # The correlation id of the HTTP request that triggered this call, so a log
    # line in the server output and a row here can be joined up.
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    research_run: Mapped["ResearchRun | None"] = relationship(back_populates="llm_calls")

    __table_args__ = (
        CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0", name="prompt_tokens_non_negative"
        ),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="completion_tokens_non_negative",
        ),
        CheckConstraint("cost_usd IS NULL OR cost_usd >= 0", name="cost_non_negative"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_non_negative"),
        # A failed call must say why, for the same reason a failed run must.
        CheckConstraint("succeeded OR error_code IS NOT NULL", name="failures_have_an_error_code"),
        Index("ix_llm_call_log_run_stage", "research_run_id", "stage"),
    )

    def __repr__(self) -> str:
        return f"<LlmCallLog {self.stage} {self.model} tokens={self.total_tokens}>"
