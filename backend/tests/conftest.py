"""
Shared test fixtures.

The settings accessor is cached with ``lru_cache``, so any test that changes the
environment must clear that cache -- otherwise it silently gets the previous
test's configuration. The ``reset_settings_cache`` fixture is autouse so this
cannot be forgotten.
"""

import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import build_engine
from app.main import create_app
from app.models import Base


@pytest.fixture(autouse=True)
def reset_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        APP_ENV="test",
        DEBUG=True,
        LOG_LEVEL="WARNING",
        CORS_ORIGINS="http://localhost:5173,http://localhost:3000",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    application = create_app(settings=settings)
    # create_app() reads settings through the cached accessor for its
    # dependencies, so point that cache at the test settings too.
    application.dependency_overrides[get_settings] = lambda: settings
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Normal client: triggers lifespan, so startup logging is exercised."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def raw_client(app: FastAPI) -> Iterator[TestClient]:
    """Client that returns 500 responses instead of re-raising.

    Needed to assert on the catch-all exception handler's output; by default
    TestClient propagates unhandled exceptions to the test.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove AutoResearch variables so defaults can be asserted."""
    for key in list(os.environ):
        if key.startswith(("APP_", "API_", "OPENAI_", "TAVILY_", "CORS_", "LOG_", "DEBUG")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def db_engine(tmp_path):
    """A throwaway database with the real schema.

    SQLite rather than PostgreSQL so the suite needs no running server, built through
    the application's own `build_engine` so the real construction path is exercised
    rather than bypassed -- which also switches on foreign key enforcement. SQLite
    ignores foreign keys by default, and without that pragma these tests would pass
    against constraints PostgreSQL would reject: the schema would look correct and be
    wrong.

    **File-backed, not :memory:**, so a genuinely separate connection can observe
    what this one has and has not committed. An in-memory database is private to its
    connection, which would make every transaction test vacuous.

    `tests/integration/test_schema_portability.py` separately checks that the same
    metadata compiles for the PostgreSQL dialect.
    """
    engine = build_engine(Settings(DATABASE_URL=f"sqlite:///{tmp_path / 'test.db'}"))
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db(db_engine) -> Iterator[Session]:
    """A session on the throwaway database.

    Every test starts from an empty schema: no seed data, no fixtures that look
    like real research output. Rows are built per test and discarded with it.
    """
    factory = sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    with factory() as session:
        yield session


@pytest.fixture
def db_settings(tmp_path) -> Settings:
    """Settings pointing at a throwaway file-backed SQLite database.

    A file rather than :memory: because Alembic opens its own connection, and an
    in-memory database is private to the connection that created it.
    """
    return Settings(
        APP_ENV="test",
        DEBUG=True,
        LOG_LEVEL="WARNING",
        DATABASE_URL=f"sqlite:///{tmp_path / 'test.db'}",
    )


@pytest.fixture
def service_session(db_engine) -> Iterator[Session]:
    """A session wired into the module-level factory the service layer uses.

    `unit_of_work()` and the repositories reach for `get_session_factory()`, so a test
    that wants them to use the throwaway database has to point that factory at it.
    Restored afterwards, so one test cannot leak its engine into the next.
    """
    from app.db import session as session_module

    factory = sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    session_module.get_session_factory.cache_clear()
    session_module.get_engine.cache_clear()
    original_factory = session_module.get_session_factory
    original_engine = session_module.get_engine
    session_module.get_session_factory = lambda: factory  # type: ignore[assignment]
    session_module.get_engine = lambda: db_engine  # type: ignore[assignment]
    try:
        with factory() as session:
            yield session
    finally:
        session_module.get_session_factory = original_factory  # type: ignore[assignment]
        session_module.get_engine = original_engine  # type: ignore[assignment]
        session_module.get_session_factory.cache_clear()
        session_module.get_engine.cache_clear()
