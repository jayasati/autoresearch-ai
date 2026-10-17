"""report-quality-v1: the opt-in pipeline's configuration fields and four audit tables.

Stage 29 (S2) of docs/REPORT_QUALITY_PIPELINE.md, decision D1 (full design set).
**Additive only**: no existing column, constraint or row changes.

- `run_configuration.pipeline_version` and `options`, both nullable. NULL is the
  legacy pipeline, so every existing row — and its content-addressed fingerprint —
  is untouched (`RunConfiguration.compute_fingerprint` hashes the two fields only
  when set). Two CHECKs: legacy is NULL, never the string; options need a pipeline.
- `report_quality_audit`, `claim_audit`, `citation_check`, `final_claim_check`:
  empty until later stages write them; nothing in the legacy pipeline reads them.

Drafted with autogenerate and corrected by hand: autogenerate rendered every enum
CHECK three times (the `sa.Enum` constraint plus two explicit copies whose names
collide under the naming convention on PostgreSQL) and missed the two table-level
CHECKs on the existing `run_configuration` table. Enum columns therefore carry only
`sa.Enum(..., create_constraint=True)`, which renders exactly the
`ck_<table>_<enum>` constraint the models generate.

Revision ID: ed421047b86d
Revises: e8f3b52a7c91
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ed421047b86d"
down_revision: str | Sequence[str] | None = "e8f3b52a7c91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POOL_STATUS = (
    "supported",
    "partially_supported",
    "contradicted",
    "unsupported",
    "not_enough_evidence",
    "not_evaluated",
)
NOT_EVALUATED_REASON = (
    "budget",
    "deadline",
    "technical",
    "claim_cap",
    "unaligned",
    "awaiting_reverification",
)
CLAIM_STATUS = (
    "supported_by_cited_source",
    "supported_citation_not_confirmed",
    "contested",
    "insufficient_evidence",
    "not_evaluated",
)
PLACEMENT = (
    "body",
    "qualified",
    "citation_not_confirmed",
    "contested",
    "unresolved",
    "unverified_list",
    "not_evaluated",
)
CITATION_OUTCOME = (
    "supports",
    "partially_supports",
    "contradicts",
    "does_not_establish",
    "invalid_reference",
    "not_evaluated",
)
REASON_IFF_NOT_EVALUATED = (
    "(pool_status = 'not_evaluated' AND not_evaluated_reason IS NOT NULL) "
    "OR (pool_status <> 'not_evaluated' AND not_evaluated_reason IS NULL)"
)


def _enum(name: str, values: Sequence[str]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=40)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "report_quality_audit",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column(
            "pipeline_version",
            _enum("pipelineversion", ("legacy", "report-quality-v1")),
            nullable=False,
        ),
        sa.Column(
            "quality_status",
            _enum(
                "reportqualitystatus", ("running", "complete", "degraded", "finalization_failed")
            ),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=40), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "pipeline_version <> 'legacy'", name=op.f("ck_report_quality_audit_audit_is_not_legacy")
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["report.id"],
            name=op.f("fk_report_quality_audit_report_id_report"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_quality_audit")),
        sa.UniqueConstraint("report_id", name=op.f("uq_report_quality_audit_report_id")),
    )

    op.create_table(
        "claim_audit",
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("pool_status", _enum("poolstatus", POOL_STATUS), nullable=False),
        sa.Column(
            "not_evaluated_reason", _enum("notevaluatedreason", NOT_EVALUATED_REASON), nullable=True
        ),
        sa.Column("claim_status", _enum("claimstatus", CLAIM_STATUS), nullable=True),
        sa.Column("alignment_status", sa.String(length=20), nullable=True),
        sa.Column("aligned_sentence_index", sa.Integer(), nullable=True),
        sa.Column("alignment_score", sa.Float(), nullable=True),
        sa.Column("priority_rank", sa.Integer(), nullable=True),
        sa.Column("final_placement", _enum("placement", PLACEMENT), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            REASON_IFF_NOT_EVALUATED, name=op.f("ck_claim_audit_not_evaluated_has_a_reason")
        ),
        sa.CheckConstraint(
            "pool_status <> 'not_evaluated' OR claim_status IS NULL "
            "OR claim_status = 'not_evaluated'",
            name=op.f("ck_claim_audit_unevaluated_claims_are_not_presented"),
        ),
        sa.CheckConstraint(
            "alignment_status IS NULL OR alignment_status IN ('aligned', 'unaligned')",
            name=op.f("ck_claim_audit_alignment_status_vocabulary"),
        ),
        sa.CheckConstraint(
            "(alignment_status = 'aligned') = (aligned_sentence_index IS NOT NULL) "
            "OR alignment_status IS NULL",
            name=op.f("ck_claim_audit_aligned_claims_name_a_sentence"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claim.id"],
            name=op.f("fk_claim_audit_claim_id_claim"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_audit")),
        sa.UniqueConstraint("claim_id", name=op.f("uq_claim_audit_claim_id")),
    )

    op.create_table(
        "final_claim_check",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("derived_from_claim_id", sa.Uuid(), nullable=True),
        sa.Column("section_index", sa.Integer(), nullable=False),
        sa.Column("sentence_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("change_kinds", sa.JSON(), nullable=False),
        sa.Column("cited_chunk_ids", sa.JSON(), nullable=False),
        sa.Column("pool_status", _enum("poolstatus", POOL_STATUS), nullable=False),
        sa.Column(
            "not_evaluated_reason", _enum("notevaluatedreason", NOT_EVALUATED_REASON), nullable=True
        ),
        sa.Column("claim_status", _enum("claimstatus", CLAIM_STATUS), nullable=False),
        sa.Column("supported_chunk_ids", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "section_index >= 0 AND sentence_index >= 0",
            name=op.f("ck_final_claim_check_indices_non_negative"),
        ),
        sa.CheckConstraint(
            REASON_IFF_NOT_EVALUATED, name=op.f("ck_final_claim_check_not_evaluated_has_a_reason")
        ),
        sa.CheckConstraint(
            "pool_status <> 'not_evaluated' OR claim_status = 'not_evaluated'",
            name=op.f("ck_final_claim_check_unevaluated_sentences_are_not_presented"),
        ),
        sa.CheckConstraint(
            "claim_status <> 'supported_by_cited_source' OR "
            "(pool_status IN ('supported', 'partially_supported') "
            "AND model IS NOT NULL AND prompt_version IS NOT NULL)",
            name=op.f("ck_final_claim_check_support_requires_a_real_verification"),
        ),
        sa.CheckConstraint(
            "pool_status = 'not_evaluated' OR prompt_version IS NOT NULL",
            name=op.f("ck_final_claim_check_verdicts_name_their_procedure"),
        ),
        sa.CheckConstraint(
            "pool_status IN ('not_evaluated', 'not_enough_evidence') OR model IS NOT NULL",
            name=op.f("ck_final_claim_check_judged_verdicts_name_a_model"),
        ),
        sa.ForeignKeyConstraint(
            ["derived_from_claim_id"],
            ["claim.id"],
            name=op.f("fk_final_claim_check_derived_from_claim_id_claim"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["report.id"],
            name=op.f("fk_final_claim_check_report_id_report"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_final_claim_check")),
        sa.UniqueConstraint(
            "report_id", "section_index", "sentence_index", name="uq_final_claim_check_sentence"
        ),
    )

    op.create_table(
        "citation_check",
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("citation_id", sa.Uuid(), nullable=True),
        sa.Column("passage_alias", sa.String(length=20), nullable=False),
        sa.Column("cited_chunk_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", _enum("citationoutcome", CITATION_OUTCOME), nullable=False),
        sa.Column(
            "not_evaluated_reason", _enum("notevaluatedreason", NOT_EVALUATED_REASON), nullable=True
        ),
        sa.Column(
            "established_by",
            _enum("establishedby", ("pool_verification", "citation_check")),
            nullable=True,
        ),
        sa.Column("quoted_text", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("procedure_version", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(outcome = 'not_evaluated' AND not_evaluated_reason IS NOT NULL) "
            "OR (outcome <> 'not_evaluated' AND not_evaluated_reason IS NULL)",
            name=op.f("ck_citation_check_not_evaluated_has_a_reason"),
        ),
        sa.CheckConstraint(
            "(outcome = 'invalid_reference') = (cited_chunk_id IS NULL)",
            name=op.f("ck_citation_check_only_invalid_references_lack_a_chunk"),
        ),
        sa.CheckConstraint(
            "outcome NOT IN ('supports', 'partially_supports', 'contradicts') "
            "OR (quoted_text IS NOT NULL AND established_by IS NOT NULL)",
            name=op.f("ck_citation_check_established_relations_are_quoted"),
        ),
        sa.CheckConstraint(
            "outcome IN ('supports', 'partially_supports', 'contradicts') "
            "OR established_by IS NULL",
            name=op.f("ck_citation_check_only_relations_are_established"),
        ),
        sa.CheckConstraint(
            "established_by IS NULL OR established_by <> 'citation_check' OR model IS NOT NULL",
            name=op.f("ck_citation_check_checked_relations_name_a_model"),
        ),
        sa.ForeignKeyConstraint(
            ["citation_id"],
            ["citation.id"],
            name=op.f("fk_citation_check_citation_id_citation"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["cited_chunk_id"],
            ["document_chunk.id"],
            name=op.f("fk_citation_check_cited_chunk_id_document_chunk"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claim.id"],
            name=op.f("fk_citation_check_claim_id_claim"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citation_check")),
        sa.UniqueConstraint("claim_id", "passage_alias", name="uq_citation_check_claim_passage"),
    )
    with op.batch_alter_table("citation_check", schema=None) as batch_op:
        batch_op.create_index("ix_citation_check_outcome", ["outcome"], unique=False)

    with op.batch_alter_table("run_configuration", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "pipeline_version",
                _enum("pipelineversion", ("legacy", "report-quality-v1")),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("options", sa.JSON(), nullable=True))
        # Bare tokens: the naming convention prefixes `ck_run_configuration_`.
        batch_op.create_check_constraint(
            "legacy_pipeline_is_null",
            sa.text("pipeline_version IS NULL OR pipeline_version <> 'legacy'"),
        )
        batch_op.create_check_constraint(
            "options_belong_to_a_pipeline",
            sa.text("options IS NULL OR pipeline_version IS NOT NULL"),
        )


def downgrade() -> None:
    """Drop exactly what `upgrade` added. Reversing loses any report-quality-v1 rows,
    which the legacy pipeline never reads."""
    with op.batch_alter_table("run_configuration", schema=None) as batch_op:
        batch_op.drop_constraint("options_belong_to_a_pipeline", type_="check")
        batch_op.drop_constraint("legacy_pipeline_is_null", type_="check")
        batch_op.drop_constraint("pipelineversion", type_="check")
        batch_op.drop_column("options")
        batch_op.drop_column("pipeline_version")

    with op.batch_alter_table("citation_check", schema=None) as batch_op:
        batch_op.drop_index("ix_citation_check_outcome")

    op.drop_table("citation_check")
    op.drop_table("final_claim_check")
    op.drop_table("claim_audit")
    op.drop_table("report_quality_audit")
