"""
Database health endpoints, and the no-hardcoded-credentials rule.

The point of these tests is the *separation* between liveness and readiness.
Conflating them is a real operational mistake: a liveness probe that fails because
the database is slow makes the orchestrator restart a healthy process, removing
capacity exactly when the system is already struggling.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.db import session as session_module
from app.main import create_app


@pytest.fixture
def healthy_client(db_engine, db_settings):
    """A client whose database is reachable."""
    session_module.get_engine.cache_clear()
    original = session_module.get_engine
    session_module.get_engine = lambda: db_engine  # type: ignore[assignment]
    try:
        app = create_app(settings=db_settings)
        app.dependency_overrides[get_settings] = lambda: db_settings
        with TestClient(app) as client:
            yield client
    finally:
        session_module.get_engine = original  # type: ignore[assignment]
        session_module.get_engine.cache_clear()


@pytest.fixture
def unreachable_client():
    """A client pointed at a database that is not there.

    Port 1 on localhost: nothing listens, so the connect is refused at once. The
    engine also carries DB_CONNECT_TIMEOUT, so even a host that black-holes the
    packets cannot make this hang.
    """
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="CRITICAL",
        DATABASE_URL="postgresql+psycopg://nobody:nothing@127.0.0.1:1/absent",
    )
    session_module.get_engine.cache_clear()
    original = session_module.get_engine
    engine = session_module.build_engine(settings)
    session_module.get_engine = lambda: engine  # type: ignore[assignment]
    try:
        app = create_app(settings=settings)
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as client:
            yield client
    finally:
        session_module.get_engine = original  # type: ignore[assignment]
        session_module.get_engine.cache_clear()
        engine.dispose()


class TestLivenessIsIndependentOfTheDatabase:
    def test_liveness_succeeds_with_the_database_down(self, unreachable_client):
        """The whole reason liveness and readiness are separate.

        If this returned 503, a container orchestrator would kill and restart a
        perfectly healthy process every time the database hiccupped — which fixes
        nothing and removes capacity when it is most needed.
        """
        response = unreachable_client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_liveness_does_no_database_work_at_all(self, unreachable_client):
        """Cheap enough to be probed every second: it never touches the engine."""
        response = unreachable_client.get("/api/health")
        assert set(response.json()) == {"status", "version", "environment"}

    def test_the_app_boots_with_the_database_down(self, unreachable_client):
        assert unreachable_client.get("/").status_code == 200


class TestReadinessWhenHealthy:
    def test_reports_ready(self, healthy_client):
        response = healthy_client.get("/api/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_names_the_database_dependency_and_its_latency(self, healthy_client, db_settings):
        body = healthy_client.get("/api/health/ready").json()
        database = body["dependencies"][0]
        # The name reflects the URL actually configured, not a hardcoded label.
        assert database["name"] == db_settings.database_backend
        assert database["healthy"] is True
        assert database["latency_ms"] >= 0
        assert database["detail"] is None

    def test_the_database_endpoint_agrees(self, healthy_client):
        response = healthy_client.get("/api/health/database")
        assert response.status_code == 200
        assert response.json()["healthy"] is True


class TestReadinessWhenTheDatabaseIsDown:
    def test_returns_503(self, unreachable_client):
        """Takes the instance out of the load balancer without killing it, so it
        recovers on its own when the dependency does."""
        assert unreachable_client.get("/api/health/ready").status_code == 503

    def test_still_returns_the_full_body(self, unreachable_client):
        """A probe that says only "not ready" forces whoever is paged to go and
        find out why."""
        body = unreachable_client.get("/api/health/ready").json()
        assert body["status"] == "not_ready"
        assert body["dependencies"][0]["healthy"] is False
        assert body["dependencies"][0]["detail"]

    def test_the_database_endpoint_returns_503_too(self, unreachable_client):
        response = unreachable_client.get("/api/health/database")
        assert response.status_code == 503
        assert response.json()["healthy"] is False

    def test_the_failure_detail_never_contains_the_password(self):
        """The driver's error text mentions host and user; it must not leak more."""
        settings = Settings(
            DATABASE_URL="postgresql+psycopg://user:sup3rs3cret@127.0.0.1:1/absent"
        )
        engine = session_module.build_engine(settings)
        try:
            reachable, _, detail = session_module.check_connection(engine)
        finally:
            engine.dispose()
        assert reachable is False
        assert "sup3rs3cret" not in (detail or "")


class TestReadinessShowsWhichDatabase:
    def test_the_backend_is_named_from_the_url(self, unreachable_client):
        body = unreachable_client.get("/api/health/ready").json()
        assert body["dependencies"][0]["name"] == "postgresql"

    def test_the_target_is_reported_with_the_password_redacted(self, unreachable_client):
        """Knowing which database an instance points at is most of the diagnosis for
        a misconfigured deployment — so it is reported, but never in full."""
        body = unreachable_client.get("/api/health/ready").json()
        target = body["dependencies"][0]["target"]
        assert "127.0.0.1:1/absent" in target
        assert "nothing" not in target
        assert ":***@" in target


class TestRequireDatabase:
    def test_passes_when_reachable(self, db_engine):
        from app.db.session import require_database

        require_database(db_engine)  # does not raise

    def test_raises_an_upstream_error_when_not(self):
        """`external_service_error`, not `internal_error`: a run that failed because
        the database was down is a different result from one that failed because our
        logic is wrong, and the benchmark must not conflate them."""
        from app.core.exceptions import ExternalServiceError
        from app.db.session import require_database

        engine = session_module.build_engine(
            Settings(DATABASE_URL="postgresql+psycopg://nobody:nothing@127.0.0.1:1/absent")
        )
        try:
            with pytest.raises(ExternalServiceError) as caught:
                require_database(engine)
        finally:
            engine.dispose()
        assert caught.value.service == "postgresql"
        assert caught.value.status_code == 502


class TestNoHardcodedCredentials:
    """Requirement 7, asserted rather than asserted-to.

    A credential reaches version control by accident, not by decision, so the check
    belongs in the suite where it runs on every commit.
    """

    def test_the_shipped_default_is_an_obvious_placeholder(self):
        """So a fresh checkout fails to connect rather than silently reaching
        something real."""
        settings = Settings(_env_file=None)
        assert settings.database_credentials_look_unset is True
        assert "CHANGEME" in settings.DATABASE_URL

    def test_an_unset_database_is_reported_as_not_configured(self):
        settings = Settings(_env_file=None)
        assert settings.integration_status["postgres"] is False
        assert "DATABASE_URL" in settings.missing_credentials

    def test_a_configured_database_is_reported_as_configured(self):
        settings = Settings(DATABASE_URL="postgresql://u:realpassword@h:5432/d")
        assert settings.integration_status["postgres"] is True
        assert "DATABASE_URL" not in settings.missing_credentials

    def test_no_tracked_file_contains_a_plausible_connection_string(self):
        """Scan the source tree for a postgres URL carrying a real-looking password.

        `.env` is gitignored and excluded; `.env.example` and the default in
        `config.py` carry visible placeholders, which are the point.
        """
        import os
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[3]
        pattern = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s:/@]+:([^\s@]+)@")
        # Placeholders and test fixtures. Anything else is a finding.
        allowed = {
            "CHANGEME", "postgres", "password", "***", "nothing",
            "s3cret", "sup3rs3cret", "pw", "realpassword", "p",
        }
        skip_dirs = {".venv", "node_modules", ".git", "dist", "__pycache__", ".pytest_cache",
                     ".ruff_cache", ".mypy_cache", "htmlcov"}
        suffixes = {
            ".py", ".md", ".ini", ".yml", ".yaml",
            ".example", ".txt", ".js", ".jsx", ".json",
        }

        offenders: list[str] = []
        # os.walk with pruning, not rglob: rglob descends into node_modules, which is
        # tens of thousands of files and turns this test into a minute of I/O.
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for filename in filenames:
                path = pathlib.Path(dirpath) / filename
                if path.suffix not in suffixes or path.name == ".env":
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:  # pragma: no cover
                    continue
                for match in pattern.finditer(content):
                    password = match.group(1)
                    if password not in allowed:
                        offenders.append(f"{path.relative_to(root)}: {password[:4]}...")

        assert offenders == [], f"possible hardcoded credentials: {offenders}"

    def test_alembic_ini_sets_no_url(self):
        import pathlib

        ini = (pathlib.Path(__file__).resolve().parents[2] / "alembic.ini").read_text(
            encoding="utf-8"
        )
        assert not any(
            line.strip().startswith("sqlalchemy.url") and "=" in line
            for line in ini.splitlines()
        )

    def test_dotenv_is_gitignored(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3]
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gitignore
        assert "!.env.example" in gitignore
