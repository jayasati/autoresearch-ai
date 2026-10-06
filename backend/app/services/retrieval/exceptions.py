"""
Retrieval failures.

Every one of these is an `ExternalServiceError`, so they already map to the API's
`external_service_error` / `rate_limited` codes and to a 502 or 429 — and, more
importantly, they stay distinguishable from our own bugs. ARCHITECTURE.md makes that
point for the benchmark: a run that failed because Tavily was down is not the same
result as a run that failed because our logic is wrong, and the two must not be
pooled.

The distinction that actually changes behaviour is **retryable or not**. A 429, a
timeout and a 5xx may succeed on a second attempt. A bad API key never will, and
retrying it only delays a clear error and burns the run's time budget. `retryable`
is a property of the exception class rather than a decision made at the call site,
so the policy cannot drift between callers.
"""

from typing import Any

from app.core.exceptions import ExternalServiceError


class RetrievalError(ExternalServiceError):
    """Base for every retrieval-backend failure."""

    #: Whether another attempt could plausibly succeed.
    retryable: bool = False

    def __init__(self, service: str, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(service, message, **kwargs)


class SearchNotConfigured(RetrievalError):
    """No usable API key.

    A configuration error, not an upstream one, so it is a 500 rather than a 502 and
    is never retried. It is raised at call time rather than at startup, because the
    service must boot and answer /api/health without credentials — see stage 2.
    """

    status_code = 500
    code = "configuration_error"
    message = "The search provider is not configured."
    retryable = False


class SearchAuthenticationFailed(RetrievalError):
    """The provider rejected the key (401/403).

    Never retried: a wrong or revoked key will be just as wrong in two seconds, and
    the attempts would be charged against the run's time budget for nothing.
    """

    status_code = 502
    code = "external_service_error"
    message = "The search provider rejected the API key."
    retryable = False


class SearchRateLimited(RetrievalError):
    """429. Retryable, and the provider may have said when.

    `retry_after` carries the server's own `Retry-After` when present. Obeying it
    beats guessing: backing off less than asked invites another 429, and backing off
    more wastes the run's budget.
    """

    status_code = 429
    code = "rate_limited"
    message = "The search provider rate-limited the request."
    retryable = True

    def __init__(
        self,
        service: str,
        message: str | None = None,
        *,
        retry_after: float | None = None,
        **kwargs: Any,
    ) -> None:
        self.retry_after = retry_after
        details = {"retry_after_seconds": retry_after, **kwargs.pop("details", {})}
        super().__init__(service, message, details=details, **kwargs)


class SearchTimeout(RetrievalError):
    """The request did not complete inside the configured timeout.

    Retryable, but bounded: a provider that is slow now is often slow for the next
    few seconds too, so attempts are capped rather than open-ended.
    """

    status_code = 504
    code = "external_service_error"
    message = "The search provider did not respond in time."
    retryable = True


class SearchProviderUnavailable(RetrievalError):
    """5xx from the provider. Retryable — the fault is on their side and transient."""

    status_code = 502
    code = "external_service_error"
    message = "The search provider is unavailable."
    retryable = True


class SearchRequestInvalid(RetrievalError):
    """4xx that is not auth or rate limiting: we sent something the provider refused.

    Not retryable, because the request will be refused identically next time. This is
    the one in this module that usually means *our* bug, so it says so rather than
    hiding behind a generic upstream error.
    """

    status_code = 502
    code = "external_service_error"
    message = "The search provider rejected the request."
    retryable = False


class SearchResponseInvalid(RetrievalError):
    """The provider answered 200 with something we cannot read.

    Not retryable: a malformed body is not a transient condition, and silently
    returning zero results would be worse — a run would record "no sources found"
    when the truth is "we could not parse the answer", and the benchmark would read
    that as a property of the topic.
    """

    status_code = 502
    code = "external_service_error"
    message = "The search provider returned an unreadable response."
    retryable = False


class SearchUnreachable(RetrievalError):
    """DNS failure, refused connection, TLS error. Retryable."""

    status_code = 502
    code = "external_service_error"
    message = "The search provider could not be reached."
    retryable = True
