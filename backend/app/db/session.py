"""
Engine and session management.

The engine is created lazily, on first use, rather than at import time: the
application must be able to boot and answer /api/health with no database
reachable, which is what makes the liveness check meaningful.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine:
    """The process-wide engine. Cached, so the connection pool is shared."""
    settings = get_settings()
    url = settings.DATABASE_URL

    kwargs: dict = {"echo": False, "future": True}
    if url.startswith("sqlite"):
        # SQLite needs these to behave sanely under a pooled, threaded server.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True  # drop connections the server already closed

    engine = create_engine(url, **kwargs)
    logger.info("Database engine created for %s", _redact(url))
    return engine


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with get_session_factory()() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional scope for scripts and background work."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Turn on foreign key enforcement for SQLite.

    SQLite ignores foreign keys unless asked, per connection. Without this, tests
    would pass against constraints PostgreSQL would reject -- the schema would
    look correct and be wrong.
    """

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # pragma: no cover - event hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _redact(url: str) -> str:
    """Hide the password before a connection string reaches a log."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
