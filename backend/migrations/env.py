"""
Alembic environment.

Two things differ from the generated template, both deliberate:

1. **The URL comes from `Settings`, not from `alembic.ini`.** `alembic.ini` is
   committed, so a URL written there is a credential in version control. Reading
   `DATABASE_URL` through the application's own configuration means migrations and
   the running service can never disagree about which database they mean.

2. **`target_metadata` is the application's metadata**, imported through
   `app.models`, which registers every model. Importing a submodule directly would
   leave tables out of autogenerate and silently produce an incomplete migration.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.db.session import _redact, build_engine

# Importing app.models registers all 17 tables on Base.metadata.
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()

# Recorded in the config so Alembic's own logging shows which database it is
# touching -- with the password removed.
config.set_main_option("sqlalchemy.url", settings.database_url_safe)


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    """Shared context options, so offline and online modes cannot drift apart."""
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        # Detect a changed column type, not just added and dropped columns.
        compare_type=True,
        # Detect a changed server default, which otherwise silently diverges.
        compare_server_default=True,
        # Needed on SQLite, which cannot ALTER a column: Alembic rewrites the table
        # instead. Harmless on PostgreSQL, and it keeps one migration file working
        # against both, which is what lets the test suite run the real migration.
        render_as_batch=True,
        include_schemas=False,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Useful for review, and for a deployment where a DBA applies the SQL by hand.
    """
    _configure(url=settings.DATABASE_URL)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    connectable = config.attributes.get("connection", None)

    if connectable is not None:
        # A caller (the test suite) handed us a connection; use it and do not
        # dispose of it.
        _configure(connection=connectable)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = build_engine(settings)
    try:
        with engine.connect() as connection:
            _configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

# Silence the unused-import warning while documenting why the import exists.
_ = (pool, _redact)
