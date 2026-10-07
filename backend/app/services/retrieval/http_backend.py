"""
Shared HTTP behaviour for retrieval backends.

Extracted when the second provider arrived. The retry loop, the status-to-exception
mapping, `Retry-After` parsing and the backoff calculation were about to be duplicated
between Tavily and Semantic Scholar — and duplicated retry logic is the kind of thing
where a bug gets fixed in one copy and lives on in the other.

Providers differ in ways the subclass supplies:

- **authentication**: `Bearer` for Tavily, `x-api-key` for Semantic Scholar;
- **method and shape**: Tavily POSTs a JSON body, Semantic Scholar GETs query params;
- **rate limiting**: Semantic Scholar needs a client-side throttle, Tavily does not.

Everything else is here, once.
"""

import logging
import random
import time
from abc import ABC, abstractmethod

import httpx

from app.core.config import Settings, get_settings
from app.services.retrieval.exceptions import (
    RetrievalError,
    SearchAuthenticationFailed,
    SearchProviderUnavailable,
    SearchRateLimited,
    SearchRequestInvalid,
    SearchResponseInvalid,
    SearchTimeout,
    SearchUnreachable,
)
from app.services.retrieval.rate_limit import AsyncRateLimiter

logger = logging.getLogger(__name__)


class HttpSearchBackend(ABC):
    """A retrieval backend that talks HTTP, with retries that only retry what can win."""

    #: Name used in exceptions, logs and the limiter registry.
    provider: str = "unknown"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        sleep=None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        # Injectable so retry tests do not actually wait. Backoff is real behaviour
        # worth asserting on, and a test that sleeps through it is a test nobody runs.
        if sleep is None:
            import asyncio

            sleep = asyncio.sleep
        self._sleep = sleep

    # --- what a subclass must provide ----------------------------------------

    @property
    @abstractmethod
    def base_url(self) -> str: ...

    @property
    @abstractmethod
    def timeout_seconds(self) -> float: ...

    @property
    @abstractmethod
    def max_attempts(self) -> int: ...

    @property
    @abstractmethod
    def backoff_base_seconds(self) -> float: ...

    @property
    @abstractmethod
    def backoff_max_seconds(self) -> float: ...

    @abstractmethod
    def auth_headers(self) -> dict[str, str]:
        """Headers that authenticate the request.

        Returns an empty dict for a provider usable without a key — Semantic
        Scholar's public API works unauthenticated, just more slowly.
        """

    @property
    def credential_setting(self) -> str | None:
        """Which setting holds this provider's credential.

        Named in an authentication error, so the message says what to fix rather than
        only that something is wrong. `None` for a provider usable without a key.
        """
        return None

    @property
    def rate_limiter(self) -> AsyncRateLimiter | None:
        """A client-side throttle, when the provider's limit needs one."""
        return None

    # --- lifecycle -----------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds),
                headers={"Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        """Close the client, but only if this backend created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.aclose()

    # --- the request loop ----------------------------------------------------

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> tuple[dict, int]:
        """Make the request, retrying only what could plausibly succeed.

        Returns `(payload, attempts)`. The attempt count is reported upward because a
        search that needed three tries is a cost worth seeing in the logs and in the
        benchmark's timing.
        """
        headers = self.auth_headers()
        attempts_allowed = max(1, self.max_attempts)
        limiter = self.rate_limiter
        last_error: RetrievalError | None = None

        for attempt in range(1, attempts_allowed + 1):
            # Throttle *before* sending, including before a retry. Retrying straight
            # into a rate limit is how a tight budget gets spent on rejections.
            if limiter is not None:
                await limiter.acquire()

            try:
                response = await self.client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = SearchTimeout(
                    self.provider,
                    f"Request timed out after {self.timeout_seconds:g}s.",
                    details={"attempt": attempt, "timeout_seconds": self.timeout_seconds},
                )
                logger.warning(
                    "%s attempt %d/%d timed out: %s", self.provider, attempt, attempts_allowed, exc
                )
            except httpx.HTTPError as exc:
                last_error = SearchUnreachable(
                    self.provider,
                    f"Could not reach the provider: {type(exc).__name__}.",
                    details={"attempt": attempt},
                )
                logger.warning(
                    "%s attempt %d/%d unreachable: %s",
                    self.provider,
                    attempt,
                    attempts_allowed,
                    exc,
                )
            else:
                error = self.error_for(response, attempt)
                if error is None:
                    return self.decode(response), attempt
                last_error = error
                logger.warning(
                    "%s attempt %d/%d failed: %s (%s)",
                    self.provider,
                    attempt,
                    attempts_allowed,
                    error.code,
                    error.message,
                )

            wait = self.backoff_seconds(attempt, last_error)

            # A rate limit is the provider's whole account, not this one request, so
            # make every caller in the process wait rather than only this one.
            if limiter is not None and isinstance(last_error, SearchRateLimited):
                limiter.penalise(wait)

            if not last_error.retryable or attempt == attempts_allowed:
                break
            await self._sleep(wait)

        assert last_error is not None  # the loop cannot exit without one
        last_error.details["attempts"] = attempts_allowed if last_error.retryable else 1
        raise last_error

    # --- status mapping ------------------------------------------------------

    def error_for(self, response: httpx.Response, attempt: int) -> RetrievalError | None:
        """Map a status code to an exception, or `None` when the response is usable."""
        status = response.status_code
        if 200 <= status < 300:
            return None

        detail = self.error_detail(response)

        if status == 429:
            return SearchRateLimited(
                self.provider,
                f"Rate limited by the provider: {detail}",
                retry_after=self.retry_after_seconds(response),
                details={"status": status, "attempt": attempt},
            )
        if status in (401, 403):
            details: dict = {"status": status}
            if self.credential_setting:
                details["setting"] = self.credential_setting
            return SearchAuthenticationFailed(
                self.provider,
                f"The provider rejected the credentials: {detail}",
                details=details,
            )
        if 500 <= status < 600:
            return SearchProviderUnavailable(
                self.provider,
                f"The provider returned {status}: {detail}",
                details={"status": status, "attempt": attempt},
            )
        return SearchRequestInvalid(
            self.provider,
            f"The provider returned {status}: {detail}",
            details={"status": status},
        )

    @staticmethod
    def error_detail(response: httpx.Response) -> str:
        """A short, safe description of a failure body.

        Truncated, and never echoed in full: a provider error body can quote the
        request it rejected, and the request carries the API key.
        """
        try:
            payload = response.json()
        except (ValueError, httpx.DecodingError):
            return (response.text or "").strip()[:200] or "no body"
        if isinstance(payload, dict):
            for key in ("detail", "error", "message"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:200]
        return str(payload)[:200]

    @staticmethod
    def retry_after_seconds(response: httpx.Response) -> float | None:
        """Parse `Retry-After`, which may be seconds or an HTTP date.

        Only the numeric form is honoured. A date form would need clock-skew handling
        to be trustworthy, and guessing wrong about when to come back is worse than
        falling through to our own bounded backoff.
        """
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            seconds = float(raw.strip())
        except ValueError:
            return None
        return seconds if seconds >= 0 else None

    def backoff_seconds(self, attempt: int, error: RetrievalError) -> float:
        """How long to wait before the next attempt.

        The provider's own `Retry-After` wins when it sent one — it knows when its
        limit resets and we do not. Otherwise exponential backoff with full jitter.

        The jitter is not decoration: without it, several sub-question searches that
        were rate-limited together would all retry at the same instant and be
        rate-limited together again. Everything is capped, so one unlucky request
        cannot sit still for a minute while the run's budget drains.
        """
        retry_after = getattr(error, "retry_after", None)
        if retry_after is not None:
            return min(float(retry_after), self.backoff_max_seconds)

        exponential = self.backoff_base_seconds * (2 ** (attempt - 1))
        capped = min(exponential, self.backoff_max_seconds)
        return random.uniform(0, capped)  # noqa: S311 - jitter, not cryptography

    def decode(self, response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise SearchResponseInvalid(
                self.provider, "The response body was not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise SearchResponseInvalid(
                self.provider, f"Expected a JSON object, got {type(payload).__name__}."
            )
        return payload

    @staticmethod
    def now() -> float:
        return time.perf_counter()
