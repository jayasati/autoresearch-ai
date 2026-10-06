"""
The schema is written for PostgreSQL but tested on SQLite.

That gap is a real risk: a model can pass every SQLite test and fail to deploy.
These tests close it without needing a running PostgreSQL server, by compiling the
same metadata for the PostgreSQL dialect and checking the parts that differ
between backends.

PostgreSQL *was* listening on this machine while this was written, but the
credentials were not available, so a live check could not be run. Compiling the
DDL is the strongest verification possible without them.
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
        """Chosen so adding a member is a data migration, not a DDL one."""
        ddl = str(CreateTable(Base.metadata.tables["run_configuration"]).compile(dialect=PG))
        assert "mode VARCHAR(40)" in ddl
        assert "CREATE TYPE" not in ddl


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
