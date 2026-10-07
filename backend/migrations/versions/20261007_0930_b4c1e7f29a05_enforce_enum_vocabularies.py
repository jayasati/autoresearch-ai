"""Enforce the enum vocabularies in the database, and add EvidenceDepth.METADATA.

Two changes, both discovered while adding academic search.

**The enum columns had no CHECK constraint.** `enum_column()` passed
`native_enum=False` intending "VARCHAR plus a CHECK", and the documentation said so --
but SQLAlchemy 1.4 changed `create_constraint` to default to `False`, so every enum
column was an unconstrained `VARCHAR(40)`. The Python layer still validated, and
Pydantic still validated, but a data-loading script, a migration or a `psql` session
could have written any string at all. That is precisely the gap this project closes
everywhere else by enforcing an invariant in both places, so the constraints are added
here for all eleven enum columns.

**`EvidenceDepth` gains `metadata`.** Semantic Scholar does not always return an
abstract. A paper with no abstract is still a real source worth recording -- dropping
it would bias retrieval toward whatever happens to have an abstract indexed -- but we
have no text for it, so labelling it `abstract` would be a lie that the groundedness
metrics would later repeat. `metadata` says what is true: title and bibliographic data,
no text.

Alembic's autogenerate does **not** detect either change -- it reported zero
differences against a schema whose CHECK did not exist and whose enum was missing a
value. `tests/integration/test_schema_portability.py` now asserts that every enum
column has a constraint covering every member, which is the check autogenerate cannot
perform.

Revision ID: b4c1e7f29a05
Revises: 253b523fd670
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c1e7f29a05"
down_revision: str | Sequence[str] | None = "253b523fd670"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, column, bare constraint token, allowed values).
#
# The token is deliberately bare. The metadata naming convention is
# `ck_%(table_name)s_%(constraint_name)s` and Alembic applies it to whatever is passed
# here, so passing an already-prefixed name yields
# `ck_document_ck_document_evidencedepth`. Passing `evidencedepth` yields
# `ck_document_evidencedepth` -- which is exactly what `CreateTable` generates from the
# models, so a freshly created schema and a migrated one carry identical names.
ENUM_CONSTRAINTS: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "run_configuration",
        "mode",
        "researchmode",
        ("model_only", "hybrid", "search_grounded"),
    ),
    (
        "research_run",
        "status",
        "runstatus",
        ("pending", "planning", "retrieving", "drafting", "verifying", "completed", "failed"),
    ),
    ("source", "source_type", "sourcetype", ("web", "academic", "model")),
    (
        "document",
        "depth",
        "evidencedepth",
        ("metadata", "snippet", "abstract", "full_text"),
    ),
    (
        "research_source",
        "evidence_depth",
        "evidencedepth",
        ("metadata", "snippet", "abstract", "full_text"),
    ),
    (
        "evidence",
        "relation",
        "evidencerelation",
        ("supports", "contradicts", "neutral"),
    ),
    (
        "citation",
        "status",
        "citationstatus",
        ("valid", "broken", "misattributed", "fabricated"),
    ),
    (
        "verification_result",
        "verdict",
        "verificationverdict",
        (
            "supported",
            "partially_supported",
            "unsupported",
            "contradicted",
            "not_enough_evidence",
        ),
    ),
    (
        "conflict",
        "conflict_type",
        "conflicttype",
        (
            "numeric_disagreement",
            "directional_disagreement",
            "temporal_staleness",
            "scope_mismatch",
        ),
    ),
    (
        "evaluation_result",
        "metric_key",
        "metrickey",
        (
            "claim_support_rate",
            "hallucination_rate",
            "citation_precision",
            "citation_recall",
            "fabrication_rate",
            "coverage",
            "source_diversity",
            "full_text_grounding_rate",
            "conflicts_detected",
            "total_tokens",
            "cost_usd",
            "wall_clock_seconds",
        ),
    ),
    (
        "llm_call_log",
        "stage",
        "pipelinestage",
        (
            "planning",
            "retrieval",
            "synthesis",
            "critique",
            "claim_extraction",
            "verification",
            "citation_validation",
            "conflict_detection",
        ),
    ),
]


def _condition(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Add a CHECK per enum column.

    `batch_alter_table` so the same migration works on SQLite, which cannot add a
    constraint in place and has to rebuild the table. On PostgreSQL it issues a plain
    `ALTER TABLE ... ADD CONSTRAINT`.

    No data is rewritten: every existing row already holds a valid value, because the
    Python layer has been validating all along. If a row did not, this migration would
    fail loudly rather than silently truncate the vocabulary -- which is the right way
    round.
    """
    for table, column, name, values in ENUM_CONSTRAINTS:
        with op.batch_alter_table(table, schema=None) as batch:
            batch.create_check_constraint(name, sa.text(_condition(column, values)))


def downgrade() -> None:
    """Drop the constraints again, leaving plain VARCHAR columns.

    Reversible on purpose, but note what reversing costs: the database stops
    validating the vocabulary and only the Python layer does.
    """
    for table, _column, name, _values in reversed(ENUM_CONSTRAINTS):
        with op.batch_alter_table(table, schema=None) as batch:
            batch.drop_constraint(name, type_="check")
