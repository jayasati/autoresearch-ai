"""Add claim typing, claim importance, and the 'unvalidated' citation status.

Claim extraction (stage 12) types every claim and rates its centrality, so `claim`
gains `claim_type` and `importance` â€” both VARCHAR-plus-CHECK like every other enum
column (see b4c1e7f29a05 for why not a native enum). The table is empty in every
environment (extraction has never run), so the columns can be added NOT NULL
without a backfill.

`citation.status` gains **unvalidated**: extraction records what the report cited
before validation has checked anything, and the previous vocabulary had no honest
value for that state â€” 'valid' would be a claim nobody verified, and the three
failure values would be failures nobody observed. With it, the
`failed_citations_have_a_reason` constraint changes meaning: 'unvalidated' is not a
failure, so it joins 'valid' in needing no failure_reason.

Alembic's autogenerate detects the new columns but not the CHECK changes, so the
constraint work is explicit here, following b4c1e7f29a05's naming (bare tokens; the
convention prefixes `ck_<table>_`).

Revision ID: c7d2a91e4b3f
Revises: b4c1e7f29a05
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d2a91e4b3f"
down_revision: str | Sequence[str] | None = "b4c1e7f29a05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLAIM_TYPES = (
    "factual",
    "numerical",
    "comparative",
    "methodological",
    "causal",
    "historical",
    "conclusion",
)
CLAIM_IMPORTANCE = ("high", "medium", "low")
CITATION_STATUSES = ("unvalidated", "valid", "broken", "misattributed", "fabricated")


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # --- claim: the two new typed columns, each with its vocabulary CHECK --------
    op.add_column(
        "claim",
        sa.Column(
            "claim_type",
            sa.Enum(*CLAIM_TYPES, name="claimtype", native_enum=False, length=40),
            nullable=False,
        ),
    )
    op.add_column(
        "claim",
        sa.Column(
            "importance",
            sa.Enum(*CLAIM_IMPORTANCE, name="claimimportance", native_enum=False, length=40),
            nullable=False,
        ),
    )
    with op.batch_alter_table("claim") as batch:
        batch.create_check_constraint(
            "claimtype", sa.text(f"claim_type IN ({_quoted(CLAIM_TYPES)})")
        )
        batch.create_check_constraint(
            "claimimportance", sa.text(f"importance IN ({_quoted(CLAIM_IMPORTANCE)})")
        )

    # --- citation: extend the status vocabulary, relax the failure-reason rule ---
    with op.batch_alter_table("citation") as batch:
        batch.drop_constraint("citationstatus", type_="check")
        batch.create_check_constraint(
            "citationstatus", sa.text(f"status IN ({_quoted(CITATION_STATUSES)})")
        )
        batch.drop_constraint("failed_citations_have_a_reason", type_="check")
        batch.create_check_constraint(
            "failed_citations_have_a_reason",
            sa.text("status IN ('valid', 'unvalidated') OR failure_reason IS NOT NULL"),
        )


def downgrade() -> None:
    with op.batch_alter_table("citation") as batch:
        batch.drop_constraint("failed_citations_have_a_reason", type_="check")
        batch.create_check_constraint(
            "failed_citations_have_a_reason",
            sa.text("status = 'valid' OR failure_reason IS NOT NULL"),
        )
        batch.drop_constraint("citationstatus", type_="check")
        batch.create_check_constraint(
            "citationstatus",
            sa.text(f"status IN ({_quoted(tuple(s for s in CITATION_STATUSES if s != 'unvalidated'))})"),
        )

    with op.batch_alter_table("claim") as batch:
        batch.drop_constraint("claimimportance", type_="check")
        batch.drop_constraint("claimtype", type_="check")
    op.drop_column("claim", "importance")
    op.drop_column("claim", "claim_type")

