"""Add the final-report columns to `report`.

Stage 14 produces a verification-aware final report. It is stored as a second text
on the same row rather than a second row or an edit, because:

- the one-report-per-run invariant stays intact (the unique constraint on
  `research_run_id` is untouched);
- `markdown` — the draft — must stay immutable: claim character offsets point into
  it, and rewriting it would silently invalidate every claim's position. The final
  report derives from the draft and its verification results; it never replaces it.

All three columns are nullable: a run that was never finalized simply has none.

Revision ID: e8f3b52a7c91
Revises: c7d2a91e4b3f
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8f3b52a7c91"
down_revision: str | Sequence[str] | None = "c7d2a91e4b3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("report", sa.Column("final_markdown", sa.Text(), nullable=True))
    op.add_column("report", sa.Column("final_model", sa.String(length=100), nullable=True))
    op.add_column(
        "report", sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("report", "finalized_at")
    op.drop_column("report", "final_model")
    op.drop_column("report", "final_markdown")
