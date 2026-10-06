"""The endpoints this stage promises: GET / and GET /api/health."""

from app import __version__


class TestRoot:
    def test_returns_service_information(self, client):
        r = client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "AutoResearch AI"
        assert body["version"] == __version__
        assert body["environment"] == "test"

    def test_points_at_docs_health_and_api_version(self, client):
        body = client.get("/").json()
        assert body["docs_url"] == "/docs"
        assert body["health_url"] == "/api/health"
        assert body["api_version_prefix"] == "/api/v1"

    def test_states_that_research_is_not_implemented(self, client):
        """The stage marker is part of the contract while the project is unfinished."""
        assert "no research functionality" in client.get("/").json()["stage"]


class TestHealth:
    def test_reports_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {
            "status": "ok",
            "version": __version__,
            "environment": "test",
        }

    def test_is_unversioned(self, client):
        """Health must not sit under /v1 -- probes should survive a version bump."""
        assert client.get("/api/v1/health").status_code == 404

    def test_does_no_io(self, client):
        """Succeeds with no database, no network and no API keys configured."""
        assert client.get("/api/health").status_code == 200


class TestSystemRoutes:
    def test_ping(self, client):
        r = client.get("/api/v1/system/ping")
        assert r.status_code == 200
        assert r.json() == {"ping": "pong"}

    def test_capabilities_lists_three_research_modes(self, client):
        body = client.get("/api/v1/system/capabilities").json()
        assert set(body["modes"]) == {"model_only", "hybrid", "search_grounded"}

    def test_capabilities_reports_nothing_implemented_yet(self, client):
        assert client.get("/api/v1/system/capabilities").json()["implemented"] == []

    def test_capabilities_names_the_credentials_still_needed(self, client):
        """So the frontend can tell the user what to put in .env, by name."""
        body = client.get("/api/v1/system/capabilities").json()
        assert isinstance(body["missing_credentials"], list)
        assert set(body["integrations"]) >= {"openai", "tavily", "semantic_scholar", "chromadb"}

    def test_capabilities_makes_no_external_calls(self, client):
        """Safe to hit with no network and no keys -- it only reads configuration."""
        assert client.get("/api/v1/system/capabilities").status_code == 200


class TestOpenAPI:
    def test_schema_builds(self, client):
        """Catches response_model mistakes that would only surface at runtime."""
        r = client.get("/openapi.json")
        assert r.status_code == 200
        assert r.json()["info"]["version"] == __version__

    def test_documents_every_expected_path(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        for expected in ("/", "/api/health", "/api/v1/system/ping", "/api/v1/system/capabilities"):
            assert expected in paths, f"{expected} missing from the OpenAPI schema"

    def test_docs_page_served(self, client):
        assert client.get("/docs").status_code == 200
