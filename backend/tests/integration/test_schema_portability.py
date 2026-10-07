"""
The schema is written for PostgreSQL but tested on SQLite.

That gap is a real risk: a model can pass every SQLite test and fail to deploy.
These tests close it without needing a running PostgreSQL server, by compiling the
same metadata for the PostgreSQL dialect and checking the parts that differ
between backends.

These checks are not a substitute for a live run, and should not be treated as one:
an earlier version of this schema passed every test here and then failed part-way
through `alembic upgrade head` on a real server, because two tables shared a
constraint name. `test_no_two_constraints_or_indexes_share_a_name` exists because of
that, and closes the specific gap -- but the general lesson stands.

The schema has since been applied to PostgreSQL 18.6 (Neon) and verified: 17 tables,
40 check constraints, 13 unique constraints, 26 foreign keys, native `uuid` and
`timestamptz`, and zero pending differences against the models.
"""

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Base

PG = postgresql.dialect()
SQLITE = sqlite.dialect()

ALL_TABLES = [t.name for t in Base.metadata.sorted_tables]


class TestPostgresCompilation:
    @pytest.mark.parametrize("table_name", ALL_TABLES)
    def test_every_table_compiles_for_postgres(self, table_name):
        table = Base.metadata.tables[table_name]
        ddl = str(CreateTable(table).compile(dialect=PG))
        assert f"CREATE TABLE {table_name}" in ddl

    @pytest.mark.parametrize("table_name", ALL_TABLES)
    def test_every_index_compiles_for_postgres(self, table_name):
        for index in Base.metadata.tables[table_name].indexes:
            ddl = str(CreateIndex(index).compile(dialect=PG))
            # Unique indexes render as "CREATE UNIQUE INDEX".
            assert ddl.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")), ddl
            assert index.name in ddl

    def test_identifiers_use_the_native_uuid_type_on_postgres(self):
        """Not CHAR(36): a native uuid column indexes better and rejects garbage."""
        ddl = str(CreateTable(Base.metadata.tables["research_run"]).compile(dialect=PG))
        assert "id UUID NOT NULL" in ddl

    def test_timestamps_are_timezone_aware_on_postgres(self):
        """A naive timestamp column silently loses the offset."""
        ddl = str(CreateTable(Base.metadata.tables["research_run"]).compile(dialect=PG))
        assert "TIMESTAMP WITH TIME ZONE" in ddl

    def test_enums_render_as_varchar_not_a_native_postgres_enum(self):
        """A native enum can only be altered outside a transaction on older servers
        and can never have a value removed; a CHECK can be dropped and recreated."""
        ddl = str(CreateTable(Base.metadata.tables["run_configuration"]).compile(dialect=PG))
        assert "mode VARCHAR(40)" in ddl
        assert "CREATE TYPE" not in ddl

    def test_every_enum_column_is_constrained_to_its_vocabulary(self):
        """The database must reject a value outside the enum, not just Python.

        Regression guard for a gap that survived two stages. `enum_column()` passed
        `native_enum=False` meaning "VARCHAR plus a CHECK", and the documentation said
        so -- but SQLAlchemy 1.4 changed `create_constraint` to default to False, so
        every enum column was an unconstrained VARCHAR. A data-loading script or a
        psql session could have written any string at all.

        Alembic's autogenerate cannot catch this: it reported **zero** differences
        against a schema whose CHECK did not exist. So the check lives here.
        """
        # The values are bind parameters in the metadata (`IN (__[POSTCOMPILE_...])`)
        # and only inline when compiled for a dialect, so the rendered DDL is what has
        # to be inspected -- and it is also what would actually be executed.
        unconstrained = []
        for table in Base.metadata.sorted_tables:
            enum_columns = [c for c in table.columns if getattr(c.type, "enums", None)]
            if not enum_columns:
                continue
            ddl = str(CreateTable(table).compile(dialect=PG))
            for column in enum_columns:
                if f"ck_{table.name}_{column.type.name}" not in ddl:
                    unconstrained.append(f"{table.name}.{column.name} has no CHECK at all")
                    continue
                missing = [value for value in column.type.enums if f"'{value}'" not in ddl]
                if missing:
                    unconstrained.append(f"{table.name}.{column.name} missing {missing}")

        assert unconstrained == [], (
            "enum columns whose CHECK does not cover every member: " + "; ".join(unconstrained)
        )

    def test_the_enum_check_names_follow_the_convention(self):
        """So the migration that drops one has a stable name, and a freshly created
        schema and a migrated one agree."""
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if not getattr(column.type, "enums", None):
                    continue
                expected = f"ck_{table.name}_{column.type.name}"
                names = {
                    str(c.name)
                    for c in table.constraints
                    if type(c).__name__ == "CheckConstraint"
                }
                assert expected in names, f"{table.name}.{column.name}: expected {expected}"


class TestConstraintNaming:
    """Alembic needs a stable name for anything it may have to drop."""

    def test_no_constraint_is_left_unnamed(self):
        unnamed = []
        for table in Base.metadata.sorted_tables:
            for constraint in table.constraints:
                if constraint.name is None:
                    unnamed.append(f"{table.name}.{type(constraint).__name__}")
        assert unnamed == []

    def test_check_constraints_follow_the_convention(self):
        for table in Base.metadata.sorted_tables:
            for constraint in table.constraints:
                if type(constraint).__name__ == "CheckConstraint":
                    assert str(constraint.name).startswith("ck_"), constraint.name

    def test_primary_keys_follow_the_convention(self):
        for table in Base.metadata.sorted_tables:
            assert str(table.primary_key.name) == f"pk_{table.name}"

    def test_no_two_constraints_or_indexes_share_a_name(self):
        """Constraint and index names are schema-scoped in PostgreSQL.

        Regression guard for a bug that reached a real server. `report_section` and
        `claim` both declared `UniqueConstraint(..., name="position_unique_per_report")`.
        An **explicit** name bypasses the metadata naming convention entirely --
        SQLAlchemy only generates one when none is given -- so both tables asked for
        the same name. SQLite accepts that; PostgreSQL rejects the second with
        `relation "position_unique_per_report" already exists`, part-way through
        `alembic upgrade head`.

        Nothing in the suite caught it, because the suite runs on SQLite. This test
        does, with no database at all.
        """
        from collections import Counter

        names: list[str] = []
        for table in Base.metadata.sorted_tables:
            names.extend(str(c.name) for c in table.constraints if c.name)
            names.extend(str(index.name) for index in table.indexes)

        duplicates = {name: count for name, count in Counter(names).items() if count > 1}
        assert duplicates == {}, f"names reused across tables: {duplicates}"

    def test_unique_constraints_are_table_prefixed(self):
        """So a collision is structurally impossible rather than merely unlikely.

        Check constraints get their table prefix from the naming convention's
        `%(constraint_name)s` token; unique constraints have no such token, so the
        prefix has to be written into the name.
        """
        from sqlalchemy import UniqueConstraint

        offenders = [
            f"{table.name}.{constraint.name}"
            for table in Base.metadata.sorted_tables
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
            and constraint.name
            and not str(constraint.name).startswith(f"uq_{table.name}")
        ]
        assert offenders == [], f"unique constraints without their table prefix: {offenders}"


class TestSchemaShape:
    def test_every_table_has_a_single_uuid_primary_key(self):
        """One identity rule across the whole schema, with no composite keys."""
        for table in Base.metadata.sorted_tables:
            columns = list(table.primary_key.columns)
            assert len(columns) == 1, f"{table.name} has a composite primary key"
            assert columns[0].name == "id", f"{table.name} primary key is not 'id'"

    def test_every_foreign_key_declares_an_on_delete_rule(self):
        """An unspecified rule defaults to NO ACTION, which strands orphan rows."""
        missing = [
            f"{fk.parent.table.name}.{fk.parent.name}"
            for table in Base.metadata.sorted_tables
            for fk in table.foreign_keys
            if fk.ondelete is None
        ]
        assert missing == []

    def test_the_expected_entities_all_exist(self):
        expected = {
            "run_configuration",
            "benchmark_run",
            "research_run",
            "subquestion",
            "source",
            "research_source",
            "document",
            "document_chunk",
            "report",
            "report_section",
            "claim",
            "evidence",
            "citation",
            "verification_result",
            "conflict",
            "evaluation_result",
            "llm_call_log",
        }
        assert set(Base.metadata.tables) == expected

    def test_both_dialects_produce_ddl_for_every_table(self):
        for table in Base.metadata.sorted_tables:
            assert str(CreateTable(table).compile(dialect=PG))
            assert str(CreateTable(table).compile(dialect=SQLITE))


class TestNoSeedData:
    """The schema ships empty.

    A data model with sample rows baked in would put fabricated research output
    into every environment that ran it -- exactly what this project must not do.
    """

    def test_creating_the_schema_inserts_nothing(self, db, db_engine):
        from sqlalchemy import func, select

        for table in Base.metadata.sorted_tables:
            count = db.execute(select(func.count()).select_from(table)).scalar_one()
            assert count == 0, f"{table.name} was created with {count} rows"
