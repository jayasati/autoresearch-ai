"""
Error handling.

The promise being tested is uniformity: *every* failure, whatever its cause,
comes back as {"error": {code, message, details, request_id}}. A frontend that
can rely on that needs one error path instead of one per endpoint.
"""

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.core.errors import register_exception_handlers
from app.core.exceptions import (
    AppError,
    BudgetExceededError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from app.core.middleware import RequestContextMiddleware
from app.utils.request_context import reset_request_id, set_request_id


def assert_error_envelope(response, *, status: int, code: str) -> dict:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}, f"error response not wrapped: {body}"
    err = body["error"]
    assert set(err) >= {"code", "message", "details", "request_id"}
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["details"], dict)
    return err


class TestExceptionHierarchy:
    """The exception classes themselves, independent of HTTP."""

    @pytest.mark.parametrize(
        ("exc", "status", "code"),
        [
            (NotFoundError(), 404, "not_found"),
            (ValidationError(), 422, "validation_error"),
            (ConflictError(), 409, "conflict"),
            (BudgetExceededError(), 429, "budget_exceeded"),
            (ExternalServiceError("tavily"), 502, "external_service_error"),
            (RateLimitError("openai"), 429, "rate_limited"),
        ],
    )
    def test_each_carries_its_status_and_stable_code(self, exc, status, code):
        assert exc.status_code == status
        assert exc.code == code

    def test_message_and_details_can_be_overridden(self):
        exc = NotFoundError("No run with id 7.", details={"run_id": 7})
        assert exc.message == "No run with id 7."
        assert exc.details == {"run_id": 7}

    def test_external_service_error_records_which_service(self):
        exc = ExternalServiceError("semantic_scholar")
        assert exc.service == "semantic_scholar"
        assert exc.details["service"] == "semantic_scholar"

    def test_rate_limit_is_an_external_service_error(self):
        """So a caller can catch upstream trouble as one category."""
        assert isinstance(RateLimitError("openai"), ExternalServiceError)

    def test_everything_derives_from_app_error(self):
        for exc in (NotFoundError(), ExternalServiceError("x"), BudgetExceededError()):
            assert isinstance(exc, AppError)


class TestRequestIdResolution:
    """`_resolve_request_id` prefers request.state, then the context variable."""

    def _request(self, state: dict | None = None):
        from starlette.requests import Request

        scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
        if state is not None:
            scope["state"] = state
        return Request(scope)

    def test_prefers_request_state(self):
        from app.core.errors import _resolve_request_id

        assert _resolve_request_id(self._request({"request_id": "from-state"})) == "from-state"

    def test_falls_back_to_the_context_variable(self):
        from app.core.errors import _resolve_request_id

        token = set_request_id("from-contextvar")
        try:
            assert _resolve_request_id(self._request({})) == "from-contextvar"
        finally:
            reset_request_id(token)

    def test_yields_the_placeholder_when_neither_is_set(self):
        from app.core.errors import _resolve_request_id

        assert _resolve_request_id(self._request({})) == "-"


class TestHandlersOnRealApp:
    def test_unknown_path_returns_the_envelope(self, client):
        assert_error_envelope(client.get("/does-not-exist"), status=404, code="not_found")

    def test_wrong_method_returns_the_envelope(self, client):
        assert_error_envelope(
            client.post("/api/health"), status=405, code="method_not_allowed"
        )

    def test_error_response_carries_a_request_id(self, client):
        err = assert_error_envelope(client.get("/nope"), status=404, code="not_found")
        assert err["request_id"] != "-"


@pytest.fixture
def failing_app() -> FastAPI:
    """A throwaway app whose only job is to raise, one way per route.

    Built here rather than adding debug routes to the real application -- an
    endpoint that exists only to crash does not belong in production code.
    """
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    class Payload(BaseModel):
        topic: str
        depth: int

    @app.get("/app-error")
    async def app_error():
        raise NotFoundError("No such research run.", details={"run_id": 42})

    @app.get("/upstream")
    async def upstream():
        raise ExternalServiceError("tavily", "Search provider timed out.")

    @app.get("/budget")
    async def budget():
        raise BudgetExceededError(details={"limit": 40, "used": 41})

    @app.get("/boom")
    async def boom():
        raise RuntimeError("something nobody anticipated")

    @app.post("/validated")
    async def validated(payload: Payload):
        return payload

    return app


@pytest.fixture
def failing_client(failing_app):
    from fastapi.testclient import TestClient

    with TestClient(failing_app, raise_server_exceptions=False) as c:
        yield c


class TestRaisedErrors:
    def test_app_error_maps_to_its_status_and_keeps_details(self, failing_client):
        err = assert_error_envelope(
            failing_client.get("/app-error"), status=404, code="not_found"
        )
        assert err["message"] == "No such research run."
        assert err["details"] == {"run_id": 42}

    def test_upstream_failure_is_502_and_names_the_service(self, failing_client):
        err = assert_error_envelope(
            failing_client.get("/upstream"), status=502, code="external_service_error"
        )
        assert err["details"]["service"] == "tavily"

    def test_budget_guardrail_is_reported_as_429_with_the_numbers(self, failing_client):
        err = assert_error_envelope(
            failing_client.get("/budget"), status=429, code="budget_exceeded"
        )
        assert err["details"] == {"limit": 40, "used": 41}

    def test_unexpected_exception_becomes_a_clean_500(self, failing_client):
        """The traceback must go to the log, not to the client."""
        err = assert_error_envelope(failing_client.get("/boom"), status=500, code="internal_error")
        assert "something nobody anticipated" not in err["message"]
        assert "Traceback" not in err["message"]

    def test_unhandled_exception_still_reports_a_request_id(self, failing_client):
        """Regression guard.

        Starlette's ServerErrorMiddleware runs *outside* our own middleware, so by
        the time the catch-all handler executes, the context variable holding the
        id has already been reset. The handler therefore falls back to
        request.state -- without which precisely the errors that most need
        correlating would arrive with no id at all.
        """
        response = failing_client.get("/boom")
        err = response.json()["error"]
        assert err["request_id"] != "-"
        assert response.headers["X-Request-ID"] == err["request_id"]

    def test_a_supplied_request_id_survives_an_unhandled_exception(self, failing_client):
        response = failing_client.get("/boom", headers={"X-Request-ID": "trace-xyz"})
        assert response.json()["error"]["request_id"] == "trace-xyz"

    def test_validation_failure_is_reported_per_field(self, failing_client):
        err = assert_error_envelope(
            failing_client.post("/validated", json={"depth": "deep"}),
            status=422,
            code="validation_error",
        )
        fields = {f["field"] for f in err["details"]["fields"]}
        assert fields == {"topic", "depth"}

    def test_valid_body_is_not_treated_as_an_error(self, failing_client):
        r = failing_client.post("/validated", json={"topic": "RAG", "depth": 2})
        assert r.status_code == 200


class TestProductionDoesNotLeakInternals:
    def test_exception_type_withheld_when_debug_is_off(self, monkeypatch):
        """In development the exception name is echoed back because it saves real
        debugging time. With DEBUG off it must be withheld."""
        from fastapi.testclient import TestClient

        from app.core.config import Settings, get_settings

        prod = Settings(APP_ENV="production", DEBUG=False, LOG_LEVEL="CRITICAL")
        monkeypatch.setattr("app.core.errors.get_settings", lambda: prod)
        get_settings.cache_clear()

        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        register_exception_handlers(app)

        @app.get("/boom")
        async def boom():
            raise RuntimeError("secret internal detail")

        with TestClient(app, raise_server_exceptions=False) as c:
            err = assert_error_envelope(c.get("/boom"), status=500, code="internal_error")

        assert err["details"] == {}
        assert "secret internal detail" not in str(err)
        assert "RuntimeError" not in str(err)
