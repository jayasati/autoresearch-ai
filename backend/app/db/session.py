"""
Engine, sessions and transaction boundaries.

Two rules this module exists to enforce:

1. **The engine is created lazily**, on first use, never at import. The application
   must boot and answer `/api/health` with no database reachable — otherwise the
   liveness check is really a database check, and a database outage turns into a
   service that will not start.

2. **One transaction per unit of work, committed in one place.** Repositories never
   commit (see `app/repositories/`); the caller that opened the unit of work does.
   Scattered commits are how half-written object graphs reach the database: a run
   with a report but no claims, or claims whose verification never landed.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)


def build_engine(settings: Settings) -> Engine:
    """Create an engine for these settings. Pure: no caching, no globals."""
    url = settings.DATABASE_URL
    kwargs: dict = {"echo": settings.DB_ECHO, "future": True}

    if url.startswith("sqlite"):
        # SQLite is used only by the test suite. An in-memory database needs a
        # StaticPool to survive across connections, since each new connection would
        # otherwise get its own empty database.
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(
            # A bounded connect, so an unreachable database fails fast instead of
            # hanging the caller -- including the readiness probe.
            connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT},
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT,
            # Recycle below any server-side idle timeout, and check a connection is
            # alive before handing it out. Without pre-ping, the first request after
            # a database restart fails with a stale connection instead of reconnecting.
            pool_recycle=settings.DB_POOL_RECYCLE,
            pool_pre_ping=True,
        )

    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        enable_sqlite_foreign_keys(engine)
    logger.info("Database engine created for %s", settings.database_url_safe)
    return engine


@lru_cache
def get_engine() -> Engine:
    """The process-wide engine. Cached so the connection pool is shared."""
    return build_engine(get_settings())


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,  # objects stay usable after commit, for responses
        # Autoflush ON, which is SQLAlchemy's default and deliberate here. With it
        # off, a query in the same unit of work does not see rows added but not yet
        # flushed -- so a repository read can silently miss a write the same
        # operation just made. That is a stale read that looks like a missing row,
        # and it is much harder to spot than an early flush.
        autoflush=True,
    )


def reset_engine() -> None:
    """Drop the cached engine and factory. For tests and for a settings change."""
    with suppress(Exception):  # nothing to dispose on a first call
        get_engine().dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request.

    Deliberately does **not** commit. A GET must not write, and a handler that does
    write states so by calling `session.commit()` or by using `unit_of_work`. An
    auto-committing dependency would make every read a potential write.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def unit_of_work(session: Session | None = None) -> Iterator[Session]:
    """A single transaction: commit on success, roll back on any exception.

    This is the only place a commit belongs. Passing an existing `session` joins the
    caller's transaction instead of opening a second one, so a service method can be
    called standalone or as part of a larger operation without committing half of it.
    """
    if session is not None:
        # Already inside someone else's unit of work; let them commit.
        yield session
        return

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Kept as the documented name used by scripts; `unit_of_work` is the same thing.
session_scope = unit_of_work


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Turn on foreign key enforcement for SQLite.

    SQLite ignores foreign keys unless asked, per connection. Without this, tests
    would pass against constraints PostgreSQL would reject — the schema would look
    correct and be wrong.
    """

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # pragma: no cover - event hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def check_connection(engine: Engine | None = None) -> tuple[bool, float, str | None]:
    """Execute the cheapest possible statement against the database.

    Returns `(reachable, latency_ms, error)`. It returns rather than raises because
    the caller is a health endpoint: an unreachable database is a fact to report,
    not an exception to propagate as a 500.
    """
    engine = engine or get_engine()
    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        # The driver's message can contain the host and user; never the password,
        # but it is still worth keeping to one line.
        detail = str(exc.__cause__ or exc).strip().splitlines()[0][:200]
        return False, elapsed, detail
    return True, (time.perf_counter() - started) * 1000, None


def require_database(engine: Engine | None = None) -> None:
    """Raise if the database is unreachable. For code paths that need it."""
    reachable, _, error = check_connection(engine)
    if not reachable:
        raise ExternalServiceError(
            "postgresql",
            "The database is unreachable.",
            details={"error": error} if error else {},
        )


def _redact(url: str) -> str:
    """Hide the password before a connection string reaches a log.

    Retained as a module-level helper because Alembic's env.py uses it too, where a
    `Settings` instance is not necessarily in hand.
    """
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
