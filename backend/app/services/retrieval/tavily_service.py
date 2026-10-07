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

The retry loop, status mapping, `Retry-After` parsing and backoff live in
`http_backend.py`, shared with Semantic Scholar — duplicated retry logic is the kind
of thing where a bug gets fixed in one copy and lives on in the other.
"""

import asyncio
import logging
from collections.abc import Sequence

from app.services.retrieval.exceptions import (
    RetrievalError,
    SearchNotConfigured,
    SearchResponseInvalid,
)
from app.services.retrieval.http_backend import HttpSearchBackend
from app.services.retrieval.models import (
    CandidateSource,
    SearchDepth,
    WebSearchQuery,
    WebSearchResult,
)

logger = logging.getLogger(__name__)

PROVIDER = "tavily"
SEARCH_PATH = "/search"


class TavilySearchService(HttpSearchBackend):
    """Searches the web and returns candidate sources.

    Construct with no arguments for normal use. Pass `client` to supply a prepared
    `httpx.AsyncClient` — that is how the tests inject a `MockTransport` without
    patching anything.
    """

    provider = PROVIDER

    # --- configuration the shared backend asks for ---------------------------

    @property
    def base_url(self) -> str:
        return self.settings.TAVILY_BASE_URL

    @property
    def timeout_seconds(self) -> float:
        return self.settings.TAVILY_TIMEOUT_SECONDS

    @property
    def max_attempts(self) -> int:
        return self.settings.TAVILY_MAX_ATTEMPTS

    @property
    def backoff_base_seconds(self) -> float:
        return self.settings.TAVILY_BACKOFF_BASE_SECONDS

    @property
    def backoff_max_seconds(self) -> float:
        return self.settings.TAVILY_BACKOFF_MAX_SECONDS

    def auth_headers(self) -> dict[str, str]:
        """The key goes in a header, never in the body.

        A request body is far more likely to end up in a log than a header is.
        """
        return {"Authorization": f"Bearer {self._require_key()}"}

    @property
    def credential_setting(self) -> str:
        return "TAVILY_API_KEY"

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

        started = self.now()
        payload, attempts = await self.request_json(
            "POST", SEARCH_PATH, json_body=self._request_body(request)
        )
        elapsed_ms = (self.now() - started) * 1000

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

    # --- the request body ----------------------------------------------------

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
