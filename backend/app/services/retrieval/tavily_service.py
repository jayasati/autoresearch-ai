"""
Tavily web search.

**This service returns candidate sources. It does not return evidence.** See
`models.py` for what that distinction means and why it is enforced in the types.

Two implementation choices worth stating:

**`httpx` directly rather than the `tavily-python` client.** The requirements here
are timeout, retry, 429 and 5xx handling — all of which means owning the HTTP
behaviour rather than inheriting whatever a vendor wrapper does. It also makes the
tests honest: they drive a real `httpx.AsyncClient` over a `MockTransport`, so the
status handling, header parsing and JSON decoding under test are the same code that
runs in production. Mocking a vendor client would test the mock.

**`include_answer=False`, deliberately.** Tavily can return an LLM-written summary of
the results. That is generated text, not a source, and this stage must produce no
generated text at all — accepting it would smuggle an unattributed, unverified claim
into the pipeline at exactly the layer that is supposed to be collecting evidence
about the world. `include_raw_content` is off for the same kind of reason: full page
text belongs to the fetch stage, and claiming it here would make every candidate look
better-grounded than it is.
"""

import asyncio
import logging
import random
import time
from collections.abc import Sequence

import httpx

from app.core.config import Settings, get_settings
from app.services.retrieval.exceptions import (
    RetrievalError,
    SearchAuthenticationFailed,
    SearchNotConfigured,
    SearchProviderUnavailable,
    SearchRateLimited,
    SearchRequestInvalid,
    SearchResponseInvalid,
    SearchTimeout,
    SearchUnreachable,
)
from app.services.retrieval.models import (
    CandidateSource,
    SearchDepth,
    WebSearchQuery,
    WebSearchResult,
)

logger = logging.getLogger(__name__)

PROVIDER = "tavily"
SEARCH_PATH = "/search"


class TavilySearchService:
    """Searches the web and returns candidate sources.

    Construct with no arguments for normal use. Pass `client` to supply a prepared
    `httpx.AsyncClient` — that is how the tests inject a `MockTransport` without
    patching anything.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        # Injectable so retry tests do not actually wait. Backoff is real behaviour
        # worth asserting on, and a test that sleeps for it is a test nobody runs.
        self._sleep = sleep

    # --- lifecycle -----------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.TAVILY_BASE_URL,
                timeout=httpx.Timeout(self.settings.TAVILY_TIMEOUT_SECONDS),
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        """Close the client, but only if this service created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "TavilySearchService":
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.aclose()

    # --- configuration -------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return self.settings.integration_status["tavily"]

    def _require_key(self) -> str:
        """The API key, or a clear configuration error.

        Checked here rather than at startup on purpose: the service must boot and
        answer /api/health with no credentials at all, which is what makes the
        liveness check meaningful. A missing key becomes an error when something
        actually tries to search.
        """
        if not self.is_configured:
            raise SearchNotConfigured(
                PROVIDER,
                "TAVILY_API_KEY is not set (or still holds a placeholder). "
                "Set it in .env to enable web search.",
                details={"setting": "TAVILY_API_KEY"},
            )
        assert self.settings.TAVILY_API_KEY is not None  # narrowed by is_configured
        return self.settings.TAVILY_API_KEY

    # --- the public API ------------------------------------------------------

    async def search(
        self,
        query: str | WebSearchQuery,
        *,
        max_results: int | None = None,
        depth: SearchDepth | None = None,
        include_domains: Sequence[str] | None = None,
        exclude_domains: Sequence[str] | None = None,
    ) -> WebSearchResult:
        """Run one search and return deduplicated candidate sources.

        Raises a `RetrievalError` subclass on failure. **Zero results is not a
        failure** — it returns a successful `WebSearchResult` with no candidates,
        because "searched and found nothing" is a real and different finding from
        "the search broke", and a run must be able to record which happened.
        """
        request = (
            query
            if isinstance(query, WebSearchQuery)
            else WebSearchQuery(
                query=query,
                max_results=max_results or self.settings.TAVILY_MAX_RESULTS,
                depth=depth or self.settings.TAVILY_SEARCH_DEPTH,
                include_domains=list(include_domains or []),
                exclude_domains=list(exclude_domains or []),
            )
        )

        started = time.perf_counter()
        payload, attempts = await self._post_with_retries(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise SearchResponseInvalid(
                PROVIDER,
                "The response had no 'results' list.",
                details={"keys": sorted(payload)[:10]},
            )

        candidates, duplicates, malformed = self._to_candidates(raw_results, request.query)

        result = WebSearchResult(
            query=request.query,
            provider=PROVIDER,
            depth=request.depth,
            candidates=candidates,
            requested_results=request.max_results,
            raw_result_count=len(raw_results),
            duplicates_removed=duplicates,
            malformed_results=malformed,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
        )

        logger.info(
            "tavily search %r: %d candidates from %d results "
            "(%d duplicates, %d malformed) across %d domain(s) in %.0fms, attempt(s)=%d",
            request.query,
            len(candidates),
            len(raw_results),
            duplicates,
            malformed,
            result.unique_domain_count,
            elapsed_ms,
            attempts,
        )
        return result

    async def search_many(
        self, queries: Sequence[str | WebSearchQuery], *, max_concurrency: int = 4
    ) -> list[WebSearchResult | RetrievalError]:
        """Run several searches concurrently.

        Returns a list positionally matching `queries`, holding either a result or the
        exception that query failed with. Failures are **returned, not raised**: one
        dead query must not discard the sub-questions that succeeded, and the
        orchestrator needs to record partial retrieval as exactly that rather than as
        a failed run.

        Concurrency is bounded, because a provider that is rate-limiting is not helped
        by twelve simultaneous requests.
        """
        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_one(q):
            async with semaphore:
                try:
                    return await self.search(q)
                except RetrievalError as exc:
                    text = q.query if isinstance(q, WebSearchQuery) else q
                    logger.warning("tavily search %r failed: %s", text, exc.message)
                    return exc

        return list(await asyncio.gather(*(run_one(q) for q in queries)))

    # --- HTTP ----------------------------------------------------------------

    def _request_body(self, request: WebSearchQuery) -> dict:
        body: dict = {
            "query": request.query,
            "search_depth": request.depth,
            "max_results": request.max_results,
            # Both deliberately off -- see the module docstring. An LLM-written answer
            # is generated text, not a source; raw page content belongs to the fetch
            # stage and would make a snippet-depth candidate look better grounded.
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if request.include_domains:
            body["include_domains"] = list(request.include_domains)
        if request.exclude_domains:
            body["exclude_domains"] = list(request.exclude_domains)
        return body

    async def _post_with_retries(self, request: WebSearchQuery) -> tuple[dict, int]:
        """POST the search, retrying only what could plausibly succeed.

        Returns `(payload, attempts)`. The attempt count is reported in the result
        because a search that needed three tries is a cost worth seeing in the logs
        and in the benchmark's timing.
        """
        api_key = self._require_key()
        body = self._request_body(request)
        headers = {"Authorization": f"Bearer {api_key}"}
        max_attempts = max(1, self.settings.TAVILY_MAX_ATTEMPTS)

        last_error: RetrievalError | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self.client.post(SEARCH_PATH, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = SearchTimeout(
                    PROVIDER,
                    f"Search timed out after {self.settings.TAVILY_TIMEOUT_SECONDS:g}s.",
                    details={
                        "attempt": attempt,
                        "timeout_seconds": self.settings.TAVILY_TIMEOUT_SECONDS,
                    },
                )
                logger.warning("tavily attempt %d/%d timed out: %s", attempt, max_attempts, exc)
            except httpx.HTTPError as exc:
                last_error = SearchUnreachable(
                    PROVIDER,
                    f"Could not reach the search provider: {type(exc).__name__}.",
                    details={"attempt": attempt},
                )
                logger.warning("tavily attempt %d/%d unreachable: %s", attempt, max_attempts, exc)
            else:
                error = self._error_for(response, attempt)
                if error is None:
                    return self._decode(response), attempt
                last_error = error
                logger.warning(
                    "tavily attempt %d/%d failed: %s (%s)",
                    attempt,
                    max_attempts,
                    error.code,
                    error.message,
                )

            if not last_error.retryable or attempt == max_attempts:
                break

            await self._sleep(self._backoff_seconds(attempt, last_error))

        assert last_error is not None  # the loop cannot exit without one
        last_error.details["attempts"] = max_attempts if last_error.retryable else 1
        raise last_error

    def _error_for(self, response: httpx.Response, attempt: int) -> RetrievalError | None:
        """Map a status code to an exception, or `None` when the response is usable."""
        status = response.status_code
        if 200 <= status < 300:
            return None

        detail = self._error_detail(response)

        if status == 429:
            return SearchRateLimited(
                PROVIDER,
                f"Rate limited by the search provider: {detail}",
                retry_after=self._retry_after_seconds(response),
                details={"status": status, "attempt": attempt},
            )
        if status in (401, 403):
            return SearchAuthenticationFailed(
                PROVIDER,
                f"The search provider rejected the API key: {detail}",
                details={"status": status, "setting": "TAVILY_API_KEY"},
            )
        if 500 <= status < 600:
            return SearchProviderUnavailable(
                PROVIDER,
                f"The search provider returned {status}: {detail}",
                details={"status": status, "attempt": attempt},
            )
        return SearchRequestInvalid(
            PROVIDER,
            f"The search provider returned {status}: {detail}",
            details={"status": status},
        )

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """A short, safe description of a failure body.

        Truncated, and never echoed in full: a provider error body can contain the
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
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """Parse `Retry-After`, which may be seconds or an HTTP date.

        Only the numeric form is honoured. A date form would need clock-skew handling
        to be trustworthy, and guessing wrong about when to come back is worse than
        falling through to our own backoff.
        """
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            seconds = float(raw.strip())
        except ValueError:
            return None
        return seconds if seconds >= 0 else None

    def _backoff_seconds(self, attempt: int, error: RetrievalError) -> float:
        """How long to wait before the next attempt.

        The provider's own `Retry-After` wins when it sent one — it knows when its
        limit resets and we do not. Otherwise exponential backoff with full jitter.

        The jitter is not decoration: without it, several sub-question searches that
        were rate-limited together would all retry at the same instant and be
        rate-limited together again. Everything is capped, so one unlucky search
        cannot sit still for a minute while the run's budget drains.
        """
        retry_after = getattr(error, "retry_after", None)
        if retry_after is not None:
            return min(float(retry_after), self.settings.TAVILY_BACKOFF_MAX_SECONDS)

        exponential = self.settings.TAVILY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        capped = min(exponential, self.settings.TAVILY_BACKOFF_MAX_SECONDS)
        return random.uniform(0, capped)  # noqa: S311 - jitter, not cryptography

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise SearchResponseInvalid(
                PROVIDER, "The response body was not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise SearchResponseInvalid(
                PROVIDER,
                f"Expected a JSON object, got {type(payload).__name__}.",
            )
        return payload

    # --- shaping the results -------------------------------------------------

    @staticmethod
    def _to_candidates(
        raw_results: list, query: str
    ) -> tuple[list[CandidateSource], int, int]:
        """Convert provider results to candidates, deduplicated by identity.

        The **first** occurrence wins, which keeps the provider's ranking: if the same
        page appears at positions 2 and 7, position 2 is the one worth keeping.

        Deduplication happens here rather than being left to the database because
        source diversity is counted over what a search returned. Eight results that
        are five copies of one page is three sources, and reporting eight would
        overstate the breadth of the retrieval.
        """
        candidates: list[CandidateSource] = []
        seen: set[str] = set()
        duplicates = 0
        malformed = 0

        for index, entry in enumerate(raw_results):
            if not isinstance(entry, dict):
                malformed += 1
                continue
            candidate = CandidateSource.from_provider_result(
                entry, rank=index, query=query, provider=PROVIDER
            )
            if candidate is None:
                malformed += 1
                continue
            if candidate.fingerprint in seen:
                duplicates += 1
                continue
            seen.add(candidate.fingerprint)
            candidates.append(candidate)

        return candidates, duplicates, malformed
