"""
Application exception hierarchy.

Code raises these instead of ``HTTPException``. The benefit is that modules
below the API layer -- retrieval, evidence, evaluation -- can signal failure
without importing FastAPI or deciding on an HTTP status code. The handlers in
``core/errors.py`` do that translation in one place.

Every exception carries a stable machine-readable ``code``. The frontend
switches on that string, never on the human-readable message, so wording can be
improved without breaking the UI.
"""

from typing import Any


class AppError(Exception):
    """Base class for every expected, handled failure in the application."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --- 4xx: the caller can fix it ------------------------------------------------


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"
    message = "The request could not be processed as submitted."


class ValidationError(AppError):
    """Semantic validation that Pydantic cannot express on its own."""

    status_code = 422
    code = "validation_error"
    message = "The request body failed validation."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "The requested resource does not exist."


class ConflictError(AppError):
    """The request is valid but conflicts with current state."""

    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


# --- 5xx / 503: we or an upstream are at fault --------------------------------


class ConfigurationError(AppError):
    """A required setting or credential is missing.

    Raised rather than crashing at import time, so the service still boots and
    can report *which* integration is unusable through /api/v1/system/capabilities.
    """

    status_code = 500
    code = "configuration_error"
    message = "The server is misconfigured."


class ExternalServiceError(AppError):
    """An upstream dependency failed: OpenAI, Tavily, Semantic Scholar, a fetch.

    Distinguishing this from a generic 500 matters for this project: a run that
    failed because a provider was down is not the same result as a run that
    failed because our logic is wrong, and the benchmark must not conflate them.
    """

    status_code = 502
    code = "external_service_error"
    message = "An upstream service failed."

    def __init__(self, service: str, message: str | None = None, **kwargs: Any) -> None:
        self.service = service
        details = {"service": service, **kwargs.pop("details", {})}
        super().__init__(
            message or f"Upstream service '{service}' failed.", details=details, **kwargs
        )


class RateLimitError(ExternalServiceError):
    status_code = 429
    code = "rate_limited"
    message = "An upstream service rate-limited the request."


class BudgetExceededError(AppError):
    """A per-run cost guardrail from ``core.config`` was hit.

    This is a deliberate stop, not a bug -- it keeps a runaway agent loop from
    spending real money.
    """

    status_code = 429
    code = "budget_exceeded"
    message = "The run exceeded its configured budget."
