"""
Alembic migrations.

The test that matters most is `test_the_migration_matches_the_models`: it applies the
migration and then asks Alembic to autogenerate against the result. An empty diff
means the migration and the models agree. Without it, the two drift silently — the
tests pass because they build the schema from metadata, while the deployed database
is missing a column nobody noticed, and the failure only appears in production.
"""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.core.config import Settings
from app.db.session import build_engine
from app.models import Base

BACKEND_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {t.name for t in Base.metadata.sorted_tables}


def alembic_config() -> Config:
    """An Alembic config pointed at a throwaway database.

    The URL is never written into the config: env.py reads it from `Settings`, which
    the fixture patches. That is exactly how it works in normal use, so these tests
    exercise the real code path rather than a test-only shortcut.
    """
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return config


@pytest.fixture
def migration_target(tmp_path, monkeypatch):
    """A fresh empty database and an Alembic config bound to it."""
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    settings = Settings(DATABASE_URL=url, APP_ENV="test")
    # env.py does `from app.core.config import get_settings` and calls it at import
    # time, and Alembic re-imports env.py on every run -- so patching the function
    # here is enough. Patching `migrations.env` directly is not: importing that
    # module outside a migration run fails, because `alembic.context` is only
    # populated while one is in progress.
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    engine = build_engine(settings)
    try:
        yield alembic_config(), engine, settings
    finally:
        engine.dispose()


class TestMigrationScripts:
    def test_there_is_exactly_one_head(self):
        """Two heads mean a branch nobody merged, and `upgrade head` becomes ambiguous."""
        script = ScriptDirectory(str(BACKEND_ROOT / "migrations"))
        assert len(script.get_heads()) == 1

    def test_every_revision_has_a_downgrade(self):
        """A migration you cannot reverse is a deployment you cannot roll back."""
        script = ScriptDirectory(str(BACKEND_ROOT / "migrations"))
        for revision in script.walk_revisions():
            source = (BACKEND_ROOT / "migrations" / "versions").glob(f"*{revision.revision}*.py")
            text_body = next(source).read_text(encoding="utf-8")
            assert "def downgrade()" in text_body
            body = text_body.split("def downgrade()")[1]
            assert "pass" not in body.split("\n")[1:3][0] or "op." in body

    def test_no_connection_string_is_committed(self):
        """alembic.ini is in version control, so a URL there is a leaked credential."""
        ini = (BACKEND_ROOT / "alembic.ini").read_text(encoding="utf-8")
        active = [
            line
            for line in ini.splitlines()
            if line.strip().startswith("sqlalchemy.url") and "=" in line
        ]
        assert active == [], f"alembic.ini sets a URL: {active}"


class TestUpgrade:
    def test_upgrade_head_creates_every_table(self, migration_target):
        config, engine, _ = migration_target
        command.upgrade(config, "head")

        tables = set(inspect(engine).get_table_names())
        assert tables >= EXPECTED_TABLES
        assert "alembic_version" in tables

    def test_upgrade_stamps_the_revision(self, migration_target):
        config, engine, _ = migration_target
        command.upgrade(config, "head")

        with engine.connect() as connection:
            stamped = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        script = ScriptDirectory(str(BACKEND_ROOT / "migrations"))
        assert stamped == script.get_current_head()

    def test_the_migration_matches_the_models(self, migration_target):
        """The check that stops the schema and the models drifting apart.

        Apply the migration, then ask Alembic what would still need changing. Anything
        other than an empty list means the deployed schema is not the one the code
        expects — and because the test suite builds its schema from metadata, nothing
        else would catch it.
        """
        config, engine, _ = migration_target
        command.upgrade(config, "head")

        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            differences = compare_metadata(context, Base.metadata)

        assert differences == [], f"migration and models disagree: {differences}"

    def test_the_migration_creates_no_rows(self, migration_target):
        """The schema ships empty; a migration that seeded data would put fabricated
        research output into every environment that ran it."""
        config, engine, _ = migration_target
        command.upgrade(config, "head")

        with engine.connect() as connection:
            for table in sorted(EXPECTED_TABLES):
                count = connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608
                assert count == 0, f"{table} was seeded with {count} rows"

    def test_constraints_survive_the_migration(self, migration_target):
        """Check constraints are the schema's enforcement; a migration that dropped
        them would leave the tables looking right and validating nothing."""
        config, engine, _ = migration_target
        command.upgrade(config, "head")

        inspector = inspect(engine)
        citation_checks = {c["name"] for c in inspector.get_check_constraints("citation")}
        assert "ck_citation_only_fabricated_citations_lack_a_source" in citation_checks

        verification_checks = {
            c["name"] for c in inspector.get_check_constraints("verification_result")
        }
        assert (
            "ck_verification_result_evidence_bearing_verdicts_cite_evidence"
            in verification_checks
        )

    def test_the_migrated_schema_accepts_a_real_write(self, migration_target):
        """The migration produces a usable database, not merely a plausible one."""
        config, engine, _ = migration_target
        command.upgrade(config, "head")

        from sqlalchemy.orm import sessionmaker

        from app.core.constants import ResearchMode, RunStatus
        from app.db.base import utcnow
        from app.models import ResearchRun, RunConfiguration

        values = {
            "mode": ResearchMode.MODEL_ONLY,
            "model": "<model under test>",
            "temperature": 0.0,
            "max_subquestions": 3,
            "max_sources": 5,
        }
        with sessionmaker(bind=engine)() as session:
            configuration = RunConfiguration(
                **values,
                fingerprint=RunConfiguration.compute_fingerprint(values),
                created_at=utcnow(),
            )
            run = ResearchRun(
                topic="<topic under test>",
                normalized_topic="<topic under test>",
                status=RunStatus.PENDING,
                configuration=configuration,
            )
            session.add(run)
            session.commit()
            assert session.get(ResearchRun, run.id) is not None


class TestDowngrade:
    def test_downgrade_base_removes_every_table(self, migration_target):
        config, engine, _ = migration_target
        command.upgrade(config, "head")
        command.downgrade(config, "base")

        remaining = set(inspect(engine).get_table_names())
        assert remaining & EXPECTED_TABLES == set()

    def test_the_round_trip_is_repeatable(self, migration_target):
        """Up, down, up again. A migration that only works once is not a migration."""
        config, engine, _ = migration_target
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")

        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
