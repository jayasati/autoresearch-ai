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
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import enable_sqlite_foreign_keys
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
def db_engine():
    """A throwaway in-memory database with the real schema.

    SQLite rather than PostgreSQL so the suite needs no running server, with
    foreign key enforcement switched on -- SQLite ignores foreign keys by default,
    and without the pragma these tests would pass against constraints PostgreSQL
    would reject. The schema would look correct and be wrong.

    `tests/integration/test_schema_portability.py` separately checks that the same
    metadata compiles for the PostgreSQL dialect.
    """
    engine = create_engine("sqlite://")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
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
