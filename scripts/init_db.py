"""
Create the database and bring it to the latest migration.

Run from the backend directory with the project virtual environment:

    cd backend
    python ../scripts/init_db.py

Reads `DATABASE_URL` from the environment or `.env` — it never takes a password on
the command line, because that would put the credential in your shell history.

Idempotent: safe to run again. It creates the database only if it is missing, and
`alembic upgrade head` is a no-op once the schema is current.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, check_connection  # noqa: E402


def fail(message: str, *hints: str) -> None:
    print(f"\n  FAILED: {message}")
    for hint in hints:
        print(f"    - {hint}")
    raise SystemExit(1)


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

    if not settings.database_is_postgres:
        print(f"  Not a PostgreSQL URL ({url.drivername}); skipping database creation.")
    else:
        # Connect to the maintenance database to create ours. CREATE DATABASE cannot
        # run inside a transaction, hence AUTOCOMMIT.
        admin_url = url.set(database="postgres")
        admin_settings = settings.model_copy(update={"DATABASE_URL": str(admin_url)})
        admin_engine = build_engine(admin_settings)

        reachable, _, error = check_connection(admin_engine)
        if not reachable:
            fail(
                f"Cannot reach the server: {error}",
                "Is PostgreSQL running? On Windows: Get-Service *postgres*",
                "Is the password in DATABASE_URL correct?",
            )

        target = url.database
        with admin_engine.connect() as connection:
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target}
            ).scalar_one_or_none()
            if exists:
                print(f"  Database {target!r} already exists.")
            else:
                connection.execute(text(f'CREATE DATABASE "{target}"'))
                print(f"  Created database {target!r}.")
        admin_engine.dispose()

    print("\nApplying migrations...")
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(config, "head")

    engine = build_engine(settings)
    reachable, latency_ms, error = check_connection(engine)
    if not reachable:
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
    engine.dispose()

    print("\n  Ready.")
    print(f"    revision : {revision}")
    print(f"    tables   : {tables}")
    print(f"    latency  : {latency_ms:.1f} ms")
    print("\nVerify over HTTP once the backend is running:")
    print("    curl http://localhost:8000/api/health/ready")


if __name__ == "__main__":
    main()
