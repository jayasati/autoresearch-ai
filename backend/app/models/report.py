"""
The generated report and its sections.

Sits between the run and the claims, completing the required chain:
Research -> Report -> Claim. Keeping the report as its own row rather than a text
column on the run means the model and prompt version that produced it are recorded
next to the text itself, which is what makes a result reproducible.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.evidence import Claim
    from app.models.research import ResearchRun


class Report(UUIDPrimaryKey, Base):
    """The report produced by one run. At most one per run.

    `markdown` is the authoritative text: claim offsets point into it, so editing
    it after claims are extracted would silently invalidate every claim's position.
    Treat it as immutable once written.
    """

    __tablename__ = "report"

    research_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_run.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # What produced it. Recorded here and not only on the configuration, because a
    # retry could in principle use a different model than the run began with.
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    research_run: Mapped["ResearchRun"] = relationship(back_populates="report")
    sections: Mapped[list["ReportSection"]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportSection.position",
    )
    claims: Mapped[list["Claim"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="Claim.position"
    )

    __table_args__ = (CheckConstraint("word_count >= 0", name="word_count_non_negative"),)

    def __repr__(self) -> str:
        return f"<Report {self.title[:40]!r} {self.word_count} words>"


class ReportSection(UUIDPrimaryKey, Base):
    """One section of a report, with its span in the report markdown.

    Offsets are kept for the same reason chunks keep theirs: a claim can then be
    attributed to a section without duplicating the text.
    """

    __tablename__ = "report_section"

    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("report.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str] = mapped_column(Text, nullable=False)

    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)

    # Which sub-question this section answers, when the planner mapped one.
    subquestion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subquestion.id", ondelete="SET NULL")
    )

    report: Mapped[Report] = relationship(back_populates="sections")
    claims: Mapped[list["Claim"]] = relationship(back_populates="report_section")

    __table_args__ = (
        UniqueConstraint("report_id", "position", name="uq_report_section_position_per_report"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint("start_char >= 0", name="start_char_non_negative"),
        CheckConstraint("end_char > start_char", name="span_is_non_empty"),
    )

    def __repr__(self) -> str:
        return f"<ReportSection {self.position}: {self.heading[:40]!r}>"
