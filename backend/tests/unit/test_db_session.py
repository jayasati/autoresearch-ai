"""Engine and session plumbing."""

import pytest
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.db.session import _redact, get_engine, get_session_factory


class TestCredentialRedaction:
    """A connection string must never reach a log with its password intact."""

    def test_the_password_is_hidden(self):
        redacted = _redact("postgresql+psycopg://me:s3cret@localhost:5432/autoresearch")
        assert "s3cret" not in redacted
        assert redacted == "postgresql+psycopg://me:***@localhost:5432/autoresearch"

    def test_the_user_and_host_survive(self):
        """Still useful for diagnosing a wrong host or user."""
        redacted = _redact("postgresql://alice:pw@db.internal:5432/app")
        assert "alice" in redacted and "db.internal" in redacted

    def test_a_url_with_no_credentials_is_unchanged(self):
        assert _redact("sqlite:///./local.db") == "sqlite:///./local.db"

    def test_a_malformed_url_is_returned_as_is(self):
        assert _redact("not-a-url") == "not-a-url"


class TestEngine:
    @pytest.fixture(autouse=True)
    def _clear_caches(self, monkeypatch):
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        yield
        get_engine.cache_clear()
        get_session_factory.cache_clear()

    def test_the_engine_is_created_lazily_and_shared(self, monkeypatch):
        """Shared, so the connection pool is not duplicated per caller."""
        monkeypatch.setattr(
            "app.db.session.get_settings", lambda: Settings(DATABASE_URL="sqlite://")
        )
        first = get_engine()
        assert isinstance(first, Engine)
        assert get_engine() is first

    def test_a_sqlite_engine_actually_connects(self, monkeypatch):
        monkeypatch.setattr(
            "app.db.session.get_settings", lambda: Settings(DATABASE_URL="sqlite://")
        )
        with get_engine().connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1

    def test_the_session_factory_is_shared_too(self, monkeypatch):
        monkeypatch.setattr(
            "app.db.session.get_settings", lambda: Settings(DATABASE_URL="sqlite://")
        )
        assert get_session_factory() is get_session_factory()

    def test_the_app_boots_without_touching_the_database(self, client):
        """Health must not depend on the database being reachable.

        The engine is built on first use, not at import, which is what makes the
        liveness check meaningful rather than a second database check.
        """
        assert client.get("/api/health").status_code == 200
