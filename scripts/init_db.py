"""
Create the database if needed and bring it to the latest migration.

Run from the repository root (or from backend/) with the project virtual environment:

    backend/.venv/Scripts/python.exe scripts/init_db.py

Reads `DATABASE_URL` from the environment or `.env` — it never takes a password on
the command line, because that would put the credential in your shell history.

Idempotent: safe to run again. It creates the database only if it is missing, and
`alembic upgrade head` is a no-op once the schema is current.

Works against a local PostgreSQL and against managed providers (Neon, Supabase, RDS),
which pre-provision the database and usually forbid `CREATE DATABASE` — see
`ensure_database` for how that case is handled.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import URL, make_url  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.db.session import build_engine, check_connection  # noqa: E402


def fail(message: str, *hints: str) -> None:
    print(f"\n  FAILED: {message}")
    for hint in hints:
        print(f"    - {hint}")
    raise SystemExit(1)


def settings_for(settings: Settings, url: URL) -> Settings:
    """Copy the settings with a different URL.

    `url.render_as_string(hide_password=False)` is **not** optional here.
    `str(url)` renders the password as `***`, so passing it on would try to
    authenticate with three asterisks and report "password authentication failed" —
    a failure that looks exactly like a wrong password and sends you looking in the
    wrong place entirely.
    """
    return settings.model_copy(
        update={"DATABASE_URL": url.render_as_string(hide_password=False)}
    )


def ensure_database(settings: Settings, url: URL) -> None:
    """Make sure the target database exists.

    Tries the target first. If it answers, there is nothing to create — and this
    matters for more than efficiency: managed PostgreSQL (Neon, Supabase, RDS)
    provisions the database for you and the application role usually cannot
    `CREATE DATABASE` at all. Attempting creation first would fail against every
    hosted database, for no reason.

    Only when the target is unreachable does it connect to the `postgres`
    maintenance database and create it — the local-development case.
    """
    engine = build_engine(settings)
    reachable, latency_ms, error = check_connection(engine)
    engine.dispose()

    if reachable:
        print(f"  Database {url.database!r} is reachable ({latency_ms:.0f} ms); nothing to create.")
        return

    if not settings.database_is_postgres:
        # SQLite creates its file on connect; nothing to do.
        print(f"  {url.get_backend_name()} database will be created on connect.")
        return

    print(f"  Database {url.database!r} not reachable: {error}")
    print("  Trying the 'postgres' maintenance database to create it...")

    admin = settings_for(settings, url.set(database="postgres"))
    admin_engine = build_engine(admin)
    admin_reachable, _, admin_error = check_connection(admin_engine)
    if not admin_reachable:
        admin_engine.dispose()
        fail(
            f"Cannot reach the server: {admin_error}",
            "Is PostgreSQL running? On Windows: Get-Service *postgres*",
            "Is the password in DATABASE_URL correct?",
            "On a managed provider, create the database in its dashboard first.",
        )

    try:
        with admin_engine.connect() as connection:
            # CREATE DATABASE cannot run inside a transaction.
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            ).scalar_one_or_none()
            if exists:
                # The database exists but we could not reach it directly: a
                # permissions problem, not a missing database.
                fail(
                    f"Database {url.database!r} exists but could not be opened.",
                    "Does the role have CONNECT privilege on it?",
                )
            connection.execute(text(f'CREATE DATABASE "{url.database}"'))
            print(f"  Created database {url.database!r}.")
    finally:
        admin_engine.dispose()


def main() -> None:
    settings = get_settings()
    url = make_url(settings.DATABASE_URL)

    print(f"Target: {settings.database_url_safe}")

    if settings.database_credentials_look_unset:
        fail(
            "DATABASE_URL still contains the CHANGEME placeholder.",
            "Copy .env.example to .env and set a real DATABASE_URL.",
            "Example: postgresql+psycopg://postgres:<your password>@localhost:5432/autoresearch",
        )

    ensure_database(settings, url)

    print("\nApplying migrations...")
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(config, "head")

    engine = build_engine(settings)
    reachable, latency_ms, error = check_connection(engine)
    if not reachable:
        engine.dispose()
        fail(f"Migrations ran but the database is unreachable: {error}")

    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        tables = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            if settings.database_is_postgres
            else text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'")
        ).scalar_one()
        server = connection.execute(text("SELECT version()")).scalar_one()
    engine.dispose()

    print("\n  Ready.")
    print(f"    server   : {server.split(' on ')[0]}")
    print(f"    revision : {revision}")
    print(f"    tables   : {tables}")
    print(f"    latency  : {latency_ms:.1f} ms")
    print("\nVerify over HTTP once the backend is running:")
    print("    curl http://localhost:8000/api/health/ready")


if __name__ == "__main__":
    main()
