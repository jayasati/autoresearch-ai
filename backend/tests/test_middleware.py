"""Correlation ids, timing headers, CORS, and logging configuration."""

import logging

from app.core.logging import RequestIdFilter, configure_logging
from app.utils.request_context import get_request_id, reset_request_id, set_request_id


class TestRequestId:
    def test_every_response_carries_one(self, client):
        assert client.get("/api/health").headers["X-Request-ID"]

    def test_each_request_gets_a_distinct_id(self, client):
        first = client.get("/api/health").headers["X-Request-ID"]
        second = client.get("/api/health").headers["X-Request-ID"]
        assert first != second

    def test_an_inbound_id_is_honoured_not_replaced(self, client):
        """So a trace can be followed from the frontend into the backend."""
        r = client.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
        assert r.headers["X-Request-ID"] == "trace-abc-123"

    def test_error_responses_carry_one_too(self, client):
        r = client.get("/unknown-path")
        assert r.headers["X-Request-ID"]
        assert r.json()["error"]["request_id"] == r.headers["X-Request-ID"]


class TestTiming:
    def test_response_time_header_is_present_and_numeric(self, client):
        value = client.get("/api/health").headers["X-Response-Time-ms"]
        assert float(value) >= 0


class TestRequestContextVar:
    def test_defaults_to_a_placeholder_outside_a_request(self):
        assert get_request_id() == "-"

    def test_set_and_reset_round_trip(self):
        token = set_request_id("abc123")
        assert get_request_id() == "abc123"
        reset_request_id(token)
        assert get_request_id() == "-"


class TestLoggingConfiguration:
    def test_configures_a_single_root_handler(self):
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == 1

    def test_is_idempotent(self):
        """Calling it twice must not double every log line."""
        configure_logging("INFO")
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == 1

    def test_level_is_applied(self):
        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING
        configure_logging("INFO")

    def test_filter_adds_request_id_to_every_record(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "-"

    def test_filter_uses_the_active_request_id(self):
        token = set_request_id("req-99")
        try:
            record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
            RequestIdFilter().filter(record)
            assert record.request_id == "req-99"
        finally:
            reset_request_id(token)

    def test_format_string_renders_with_the_injected_field(self):
        """Guards against a KeyError in the formatter, which would break logging
        at exactly the moment it is most needed."""
        from app.core.logging import LOG_FORMAT

        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        RequestIdFilter().filter(record)
        assert "hello" in logging.Formatter(LOG_FORMAT).format(record)


class TestCORS:
    def test_allowed_origin_is_permitted(self, client):
        r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_preflight_is_answered(self, client):
        r = client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 200
        assert "GET" in r.headers["access-control-allow-methods"]

    def test_disallowed_origin_gets_no_allow_header(self, client):
        r = client.get("/api/health", headers={"Origin": "http://evil.test"})
        assert "access-control-allow-origin" not in r.headers

    def test_request_id_header_is_exposed_to_the_browser(self, client):
        """Without this the frontend cannot read the id it needs for bug reports."""
        r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
        assert "X-Request-ID" in r.headers["access-control-expose-headers"]
