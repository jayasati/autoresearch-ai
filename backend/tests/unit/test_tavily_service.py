"""
Tavily search, against a mocked transport.

Every test drives a **real `httpx.AsyncClient`** over `httpx.MockTransport`. That is
deliberate: the status-code mapping, header parsing and JSON decoding being tested are
the same code that runs in production. Mocking the service's own methods, or a vendor
client, would test the mock instead.

`sleep` is injected so backoff is asserted rather than waited for. A test that really
slept through three exponential retries is a test nobody runs.

Placeholder data uses `example.invalid` — a TLD reserved by RFC 2606 that can never
resolve — so nothing here could be mistaken for a real search result.
"""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.constants import EvidenceDepth, SourceType
from app.services.retrieval import (
    CandidateSource,
    SearchAuthenticationFailed,
    SearchNotConfigured,
    SearchProviderUnavailable,
    SearchRateLimited,
    SearchRequestInvalid,
    SearchResponseInvalid,
    SearchTimeout,
    SearchUnreachable,
    TavilySearchService,
    WebSearchQuery,
)

QUERY = "does retrieval reduce hallucination"


def settings(**overrides) -> Settings:
    base = {
        "TAVILY_API_KEY": "tvly-test-key",
        "TAVILY_MAX_ATTEMPTS": 3,
        "TAVILY_BACKOFF_BASE_SECONDS": 0.01,
        "TAVILY_BACKOFF_MAX_SECONDS": 0.05,
        "TAVILY_TIMEOUT_SECONDS": 5.0,
        "APP_ENV": "test",
    }
    return Settings(**{**base, **overrides})


def result(url: str, *, title="<title>", content="<snippet under test>", score=0.5, **extra):
    return {"url": url, "title": title, "content": content, "score": score, **extra}


def payload(*results) -> dict:
    return {"query": QUERY, "results": list(results), "response_time": 0.4}


class Recorder:
    """A MockTransport handler that records requests and replays queued responses."""

    def __init__(self, *responses: httpx.Response):
        self.queued = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.queued:
            raise AssertionError("more requests than queued responses")
        response = self.queued.pop(0) if len(self.queued) > 1 else self.queued[0]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)


def service(handler, **setting_overrides) -> tuple[TavilySearchService, list[float]]:
    """A service on a mock transport, with sleeps recorded instead of taken."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.tavily.invalid"
    )
    service_under_test = TavilySearchService(
        settings=settings(**setting_overrides), client=client, sleep=fake_sleep
    )
    return service_under_test, slept


def ok(*results) -> Recorder:
    return Recorder(httpx.Response(200, json=payload(*results)))


# --------------------------------------------------------------------------- #
# 1. Successful search
# --------------------------------------------------------------------------- #


class TestSuccessfulSearch:
    @pytest.mark.asyncio
    async def test_returns_candidates_in_provider_order(self):
        handler = ok(
            result("https://a.invalid/one", score=0.9),
            result("https://b.invalid/two", score=0.7),
            result("https://c.invalid/three", score=0.5),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 3
        assert [c.rank for c in found.candidates] == [0, 1, 2]
        assert [c.domain for c in found.candidates] == ["a.invalid", "b.invalid", "c.invalid"]

    @pytest.mark.asyncio
    async def test_every_candidate_is_typed_as_a_web_snippet(self):
        """Honest typing: a search result is a snippet from the web, nothing more."""
        svc, _ = service(ok(result("https://a.invalid/one")))
        found = await svc.search(QUERY)
        candidate = found.candidates[0]

        assert candidate.source_type is SourceType.WEB
        assert candidate.evidence_depth is EvidenceDepth.SNIPPET

    @pytest.mark.asyncio
    async def test_each_candidate_carries_a_retrieval_timestamp(self):
        svc, _ = service(ok(result("https://a.invalid/one")))
        found = await svc.search(QUERY)

        assert found.candidates[0].retrieved_at.tzinfo is not None
        assert found.retrieved_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_the_query_is_recorded_on_the_result_and_each_candidate(self):
        """So a candidate can be traced back to the sub-question that found it."""
        svc, _ = service(ok(result("https://a.invalid/one")))
        found = await svc.search(QUERY)

        assert found.query == QUERY
        assert found.candidates[0].query == QUERY

    @pytest.mark.asyncio
    async def test_timing_and_attempt_count_are_reported(self):
        svc, _ = service(ok(result("https://a.invalid/one")))
        found = await svc.search(QUERY)

        assert found.elapsed_ms >= 0
        assert found.attempts == 1

    @pytest.mark.asyncio
    async def test_a_result_with_no_snippet_or_score_is_still_usable(self):
        handler = Recorder(
            httpx.Response(200, json={"results": [{"url": "https://a.invalid/one"}]})
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 1
        assert found.candidates[0].snippet is None
        assert found.candidates[0].relevance_score is None


class TestTheRequestSent:
    @pytest.mark.asyncio
    async def test_the_api_key_goes_in_the_authorization_header(self):
        handler = ok(result("https://a.invalid/one"))
        svc, _ = service(handler)
        await svc.search(QUERY)

        assert handler.requests[0].headers["Authorization"] == "Bearer tvly-test-key"

    @pytest.mark.asyncio
    async def test_the_api_key_is_not_in_the_body(self):
        """Keys belong in a header. A body is far more likely to be logged."""
        handler = ok(result("https://a.invalid/one"))
        svc, _ = service(handler)
        await svc.search(QUERY)

        assert "api_key" not in handler.body()
        assert "tvly-test-key" not in json.dumps(handler.body())

    @pytest.mark.asyncio
    async def test_generated_answers_are_never_requested(self):
        """The decision that keeps this stage free of generated text.

        Tavily can return an LLM-written summary. That is generated text, not a
        source, and accepting it would smuggle an unattributed, unverified claim into
        the layer that is supposed to be collecting evidence about the world.
        """
        handler = ok(result("https://a.invalid/one"))
        svc, _ = service(handler)
        await svc.search(QUERY)

        assert handler.body()["include_answer"] is False

    @pytest.mark.asyncio
    async def test_raw_page_content_is_not_requested(self):
        """Full text belongs to the fetch stage; asking here would make a
        snippet-depth candidate look better grounded than it is."""
        handler = ok(result("https://a.invalid/one"))
        svc, _ = service(handler)
        await svc.search(QUERY)

        assert handler.body()["include_raw_content"] is False

    @pytest.mark.asyncio
    async def test_depth_and_result_count_are_configurable(self):
        handler = ok(result("https://a.invalid/one"))
        svc, _ = service(handler, TAVILY_SEARCH_DEPTH="advanced", TAVILY_MAX_RESULTS=15)
        found = await svc.search(QUERY)

        body = handler.body()
        assert body["search_depth"] == "advanced"
        assert body["max_results"] == 15
        assert found.depth == "advanced"

    @pytest.mark.asyncio
    async def test_per_call_overrides_beat_the_configured_defaults(self):
        handler = ok(result("https://a.invalid/one"))
        svc, _ = service(handler, TAVILY_SEARCH_DEPTH="basic", TAVILY_MAX_RESULTS=8)
        await svc.search(QUERY, depth="advanced", max_results=3)

        body = handler.body()
        assert body["search_depth"] == "advanced"
        assert body["max_results"] == 3

    @pytest.mark.asyncio
    async def test_domain_filters_are_only_sent_when_set(self):
        handler = ok(result("https://a.invalid/one"))
        svc, _ = service(handler)
        await svc.search(QUERY)
        assert "include_domains" not in handler.body()

        handler2 = ok(result("https://a.invalid/one"))
        svc2, _ = service(handler2)
        await svc2.search(QUERY, include_domains=["arxiv.org"])
        assert handler2.body()["include_domains"] == ["arxiv.org"]


# --------------------------------------------------------------------------- #
# 2. Empty results
# --------------------------------------------------------------------------- #


class TestEmptyResults:
    @pytest.mark.asyncio
    async def test_no_results_is_a_success_not_an_error(self):
        """"Searched and found nothing" is a real finding, and a different one from
        "the search broke". A run must be able to record which happened."""
        svc, _ = service(Recorder(httpx.Response(200, json=payload())))
        found = await svc.search(QUERY)

        assert found.candidates == []
        assert found.is_empty is True
        assert found.raw_result_count == 0

    @pytest.mark.asyncio
    async def test_an_empty_search_still_reports_its_query_and_timing(self):
        svc, _ = service(Recorder(httpx.Response(200, json=payload())))
        found = await svc.search(QUERY)

        assert found.query == QUERY
        assert found.attempts == 1
        assert found.unique_domain_count == 0

    @pytest.mark.asyncio
    async def test_results_with_no_url_are_counted_as_malformed_not_silently_dropped(self):
        """The count is the point: zero candidates from eight results is a very
        different fact from zero results, and the difference must be visible."""
        handler = Recorder(
            httpx.Response(
                200,
                json={"results": [{"title": "no url"}, {"url": ""}, "not even a dict"]},
            )
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert found.candidates == []
        assert found.raw_result_count == 3
        assert found.malformed_results == 3

    @pytest.mark.asyncio
    async def test_a_response_with_no_results_key_is_an_error_not_an_empty_search(self):
        """Reporting "no sources found" for an unreadable response would let the
        benchmark read a parsing failure as a property of the topic."""
        svc, _ = service(Recorder(httpx.Response(200, json={"query": QUERY})))
        with pytest.raises(SearchResponseInvalid):
            await svc.search(QUERY)


# --------------------------------------------------------------------------- #
# 3. Rate limiting
# --------------------------------------------------------------------------- #


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_a_429_is_retried_and_can_succeed(self):
        handler = Recorder(
            httpx.Response(429, json={"detail": "too many requests"}),
            httpx.Response(200, json=payload(result("https://a.invalid/one"))),
        )
        svc, slept = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 1
        assert found.attempts == 2
        assert handler.call_count == 2
        assert len(slept) == 1

    @pytest.mark.asyncio
    async def test_exhausting_the_attempts_raises_rate_limited(self):
        handler = Recorder(httpx.Response(429, json={"detail": "slow down"}))
        svc, slept = service(handler, TAVILY_MAX_ATTEMPTS=3)

        with pytest.raises(SearchRateLimited) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 3
        assert len(slept) == 2  # no sleep after the final attempt
        assert caught.value.status_code == 429
        assert caught.value.code == "rate_limited"

    @pytest.mark.asyncio
    async def test_the_providers_retry_after_is_obeyed_rather_than_guessed(self):
        """It knows when its limit resets and we do not. Backing off less invites
        another 429; backing off more wastes the run's time budget."""
        handler = Recorder(
            httpx.Response(429, headers={"Retry-After": "0.03"}, json={"detail": "wait"}),
            httpx.Response(200, json=payload(result("https://a.invalid/one"))),
        )
        svc, slept = service(handler, TAVILY_BACKOFF_MAX_SECONDS=5.0)
        await svc.search(QUERY)

        assert slept == [0.03]

    @pytest.mark.asyncio
    async def test_retry_after_is_capped_so_one_search_cannot_stall_a_run(self):
        handler = Recorder(
            httpx.Response(429, headers={"Retry-After": "600"}, json={"detail": "wait"}),
            httpx.Response(200, json=payload(result("https://a.invalid/one"))),
        )
        svc, slept = service(handler, TAVILY_BACKOFF_MAX_SECONDS=0.05)
        await svc.search(QUERY)

        assert slept == [0.05]

    @pytest.mark.asyncio
    async def test_a_non_numeric_retry_after_falls_through_to_our_own_backoff(self):
        """An HTTP-date Retry-After needs clock-skew handling to be trustworthy;
        guessing wrong is worse than using our own bounded backoff."""
        handler = Recorder(
            httpx.Response(
                429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, json={}
            ),
            httpx.Response(200, json=payload(result("https://a.invalid/one"))),
        )
        svc, slept = service(handler, TAVILY_BACKOFF_MAX_SECONDS=0.05)
        await svc.search(QUERY)

        assert len(slept) == 1
        assert 0 <= slept[0] <= 0.05

    @pytest.mark.asyncio
    async def test_the_retry_after_value_is_reported_on_the_exception(self):
        handler = Recorder(httpx.Response(429, headers={"Retry-After": "2"}, json={}))
        svc, _ = service(handler, TAVILY_MAX_ATTEMPTS=1)

        with pytest.raises(SearchRateLimited) as caught:
            await svc.search(QUERY)

        assert caught.value.retry_after == 2.0
        assert caught.value.details["retry_after_seconds"] == 2.0

    @pytest.mark.asyncio
    async def test_backoff_is_jittered_so_parallel_searches_do_not_resynchronise(self):
        """Without jitter, several sub-question searches rate-limited together would
        all retry at the same instant and be rate-limited together again."""
        waits = set()
        for _ in range(12):
            handler = Recorder(
                httpx.Response(429, json={}),
                httpx.Response(200, json=payload(result("https://a.invalid/one"))),
            )
            svc, slept = service(
                handler, TAVILY_BACKOFF_BASE_SECONDS=1.0, TAVILY_BACKOFF_MAX_SECONDS=1.0
            )
            await svc.search(QUERY)
            waits.add(slept[0])

        assert len(waits) > 1, "backoff is not jittered"
        assert all(0 <= w <= 1.0 for w in waits)


# --------------------------------------------------------------------------- #
# 4. Timeout
# --------------------------------------------------------------------------- #


class TestTimeout:
    @pytest.mark.asyncio
    async def test_a_timeout_is_retried_and_can_succeed(self):
        handler = Recorder(
            httpx.ReadTimeout("too slow"),
            httpx.Response(200, json=payload(result("https://a.invalid/one"))),
        )
        svc, slept = service(handler)
        found = await svc.search(QUERY)

        assert found.attempts == 2
        assert len(slept) == 1

    @pytest.mark.asyncio
    async def test_exhausting_the_attempts_raises_search_timeout(self):
        handler = Recorder(httpx.ReadTimeout("too slow"))
        svc, _ = service(handler, TAVILY_MAX_ATTEMPTS=2)

        with pytest.raises(SearchTimeout) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 2
        assert caught.value.status_code == 504
        assert caught.value.retryable is True

    @pytest.mark.asyncio
    async def test_the_configured_timeout_is_named_in_the_error(self):
        handler = Recorder(httpx.ConnectTimeout("no connect"))
        svc, _ = service(handler, TAVILY_MAX_ATTEMPTS=1, TAVILY_TIMEOUT_SECONDS=7.0)

        with pytest.raises(SearchTimeout) as caught:
            await svc.search(QUERY)

        assert "7s" in caught.value.message
        assert caught.value.details["timeout_seconds"] == 7.0

    @pytest.mark.asyncio
    async def test_the_timeout_is_actually_applied_to_the_client(self):
        svc = TavilySearchService(settings=settings(TAVILY_TIMEOUT_SECONDS=12.5))
        try:
            assert svc.client.timeout.read == 12.5
            assert svc.client.timeout.connect == 12.5
        finally:
            await svc.aclose()


# --------------------------------------------------------------------------- #
# 5. Server failure
# --------------------------------------------------------------------------- #


class TestServerFailure:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_a_5xx_is_retried_then_reported_as_unavailable(self, status):
        handler = Recorder(httpx.Response(status, text="upstream broke"))
        svc, _ = service(handler, TAVILY_MAX_ATTEMPTS=2)

        with pytest.raises(SearchProviderUnavailable) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 2
        assert caught.value.details["status"] == status
        # 502, not 500: the fault is upstream, and the benchmark must not pool
        # "the provider was down" with "our logic is wrong".
        assert caught.value.status_code == 502
        assert caught.value.service == "tavily"

    @pytest.mark.asyncio
    async def test_a_5xx_that_recovers_is_not_an_error(self):
        handler = Recorder(
            httpx.Response(503, text="try later"),
            httpx.Response(200, json=payload(result("https://a.invalid/one"))),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert found.attempts == 2
        assert len(found.candidates) == 1

    @pytest.mark.asyncio
    async def test_a_connection_failure_is_retried_and_reported_as_unreachable(self):
        handler = Recorder(httpx.ConnectError("dns failure"))
        svc, _ = service(handler, TAVILY_MAX_ATTEMPTS=2)

        with pytest.raises(SearchUnreachable):
            await svc.search(QUERY)
        assert handler.call_count == 2

    @pytest.mark.asyncio
    async def test_a_non_json_body_is_reported_as_an_unreadable_response(self):
        handler = Recorder(httpx.Response(200, text="<html>not json</html>"))
        svc, _ = service(handler)

        with pytest.raises(SearchResponseInvalid):
            await svc.search(QUERY)
        assert handler.call_count == 1  # not retryable

    @pytest.mark.asyncio
    async def test_the_error_detail_never_echoes_the_request(self):
        """A provider error body can quote the request it rejected, and the request
        carries the API key."""
        handler = Recorder(
            httpx.Response(500, json={"detail": "failed on " + "x" * 500})
        )
        svc, _ = service(handler, TAVILY_MAX_ATTEMPTS=1)

        with pytest.raises(SearchProviderUnavailable) as caught:
            await svc.search(QUERY)

        assert "tvly-test-key" not in caught.value.message
        assert len(caught.value.message) < 400


class TestNonRetryableFailures:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_an_auth_failure_is_never_retried(self, status):
        """A wrong key will be just as wrong in two seconds; retrying only delays a
        clear error and spends the run's time budget."""
        handler = Recorder(httpx.Response(status, json={"detail": "bad key"}))
        svc, slept = service(handler, TAVILY_MAX_ATTEMPTS=3)

        with pytest.raises(SearchAuthenticationFailed) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 1
        assert slept == []
        assert caught.value.retryable is False
        assert caught.value.details["setting"] == "TAVILY_API_KEY"

    @pytest.mark.asyncio
    async def test_a_400_is_not_retried(self):
        handler = Recorder(httpx.Response(400, json={"detail": "bad query"}))
        svc, _ = service(handler, TAVILY_MAX_ATTEMPTS=3)

        with pytest.raises(SearchRequestInvalid):
            await svc.search(QUERY)
        assert handler.call_count == 1

    @pytest.mark.asyncio
    async def test_a_missing_api_key_fails_before_any_request(self):
        handler = Recorder(httpx.Response(200, json=payload()))
        svc, _ = service(handler, TAVILY_API_KEY=None)

        with pytest.raises(SearchNotConfigured) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 0
        assert caught.value.code == "configuration_error"

    @pytest.mark.asyncio
    async def test_a_placeholder_api_key_counts_as_unconfigured(self):
        handler = Recorder(httpx.Response(200, json=payload()))
        svc, _ = service(handler, TAVILY_API_KEY="tvly-replace-me")

        with pytest.raises(SearchNotConfigured):
            await svc.search(QUERY)
        assert handler.call_count == 0


# --------------------------------------------------------------------------- #
# URL normalisation and deduplication
# --------------------------------------------------------------------------- #


class TestUrlNormalisationAndDeduplication:
    @pytest.mark.asyncio
    async def test_the_same_page_in_five_spellings_becomes_one_candidate(self):
        handler = ok(
            result("https://example.invalid/article"),
            result("https://www.example.invalid/article"),
            result("https://example.invalid/article/"),
            result("https://example.invalid/article?utm_source=newsletter"),
            result("https://EXAMPLE.invalid/article#section-3"),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 1
        assert found.raw_result_count == 5
        assert found.duplicates_removed == 4

    @pytest.mark.asyncio
    async def test_the_first_occurrence_is_kept_so_ranking_survives(self):
        """If a page appears at positions 0 and 3, position 0 is the one worth
        keeping -- the provider ranked it there."""
        handler = ok(
            result("https://a.invalid/one", score=0.9),
            result("https://b.invalid/two", score=0.8),
            result("https://www.a.invalid/one/", score=0.2),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 2
        assert found.candidates[0].relevance_score == 0.9
        assert found.candidates[0].rank == 0

    @pytest.mark.asyncio
    async def test_the_original_url_is_preserved_alongside_the_canonical_one(self):
        """The canonical form is for comparison; a citation must point at the URL the
        provider actually returned."""
        handler = ok(result("https://WWW.Example.invalid/Article/?utm_source=x"))
        svc, _ = service(handler)
        candidate = (await svc.search(QUERY)).candidates[0]

        assert candidate.url == "https://WWW.Example.invalid/Article/?utm_source=x"
        assert candidate.canonical_url == "https://example.invalid/Article"

    @pytest.mark.asyncio
    async def test_meaningful_query_parameters_are_not_stripped(self):
        """Many sites identify content with them; stripping would merge two pages."""
        handler = ok(
            result("https://example.invalid/view?id=1"),
            result("https://example.invalid/view?id=2"),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 2
        assert found.duplicates_removed == 0

    @pytest.mark.asyncio
    async def test_deduplication_is_counted_so_source_diversity_is_not_overstated(self):
        handler = ok(
            result("https://a.invalid/one"),
            result("https://a.invalid/one?utm_medium=email"),
            result("https://b.invalid/two"),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert found.unique_domain_count == 2
        assert found.domains == {"a.invalid", "b.invalid"}
        assert found.duplicates_removed == 1

    @pytest.mark.asyncio
    async def test_the_fingerprint_matches_what_persistence_would_compute(self):
        """The whole reason canonicalisation is shared: if retrieval and the database
        disagreed, one page found by two runs would become two source rows."""
        handler = ok(result("https://www.example.invalid/article/"))
        svc, _ = service(handler)
        candidate = (await svc.search(QUERY)).candidates[0]

        assert candidate.to_source_create().fingerprint == candidate.fingerprint


# --------------------------------------------------------------------------- #
# Candidates are not evidence
# --------------------------------------------------------------------------- #


class TestCandidatesAreNotEvidence:
    """The constraint this whole stage is written around."""

    def test_the_model_has_no_field_asserting_support(self):
        forbidden = {
            "relation",
            "verdict",
            "supports",
            "is_evidence",
            "evidence",
            "confidence",
            "claim_id",
        }
        assert forbidden & set(CandidateSource.model_fields) == set()

    def test_the_relevance_score_is_documented_as_relevance_only(self):
        """It says the page looks topical. It says nothing about correctness."""
        description = CandidateSource.model_fields["relevance_score"].description or ""
        assert "RELEVANCE" in description
        assert "correctness" in description

    def test_depth_is_snippet_and_cannot_be_set_to_full_text(self):
        """A search result is an extract. Claiming full text would let the
        groundedness metrics overstate the result."""
        with pytest.raises(ValidationError):
            CandidateSource(
                url="https://a.invalid/x",
                canonical_url="https://a.invalid/x",
                fingerprint="f" * 64,
                rank=0,
                query=QUERY,
                evidence_depth=EvidenceDepth.FULL_TEXT,
            )

    def test_the_only_bridge_to_the_database_is_a_source_row(self):
        """Not an Evidence row, not a Citation: evidence needs a fetched chunk and a
        citation needs a claim. A search result justifies neither."""
        from app.schemas.source import SourceCreate

        candidate = CandidateSource(
            url="https://a.invalid/x",
            canonical_url="https://a.invalid/x",
            fingerprint="f" * 64,
            rank=0,
            query=QUERY,
        )
        created = candidate.to_source_create()
        assert isinstance(created, SourceCreate)
        assert created.source_type is SourceType.WEB

    def test_the_module_states_the_distinction(self):
        """Documented where someone extending it will read it."""
        from app.services.retrieval import models

        assert "not evidence" in (models.__doc__ or "").lower()


# --------------------------------------------------------------------------- #
# Query validation and concurrent search
# --------------------------------------------------------------------------- #


class TestQueryValidation:
    def test_a_blank_query_is_rejected(self):
        with pytest.raises(ValidationError):
            WebSearchQuery(query="   ")

    def test_whitespace_is_collapsed(self):
        assert WebSearchQuery(query="  does   retrieval  help ").query == "does retrieval help"

    def test_contradictory_domain_filters_are_rejected(self):
        with pytest.raises(ValidationError, match="both include and exclude"):
            WebSearchQuery(
                query="a query", include_domains=["arxiv.org"], exclude_domains=["ARXIV.ORG"]
            )

    def test_result_count_is_bounded(self):
        with pytest.raises(ValidationError):
            WebSearchQuery(query="a query", max_results=500)


class TestSearchMany:
    @pytest.mark.asyncio
    async def test_several_queries_run_and_return_in_order(self):
        handler = ok(result("https://a.invalid/one"))
        svc, _ = service(handler)
        results = await svc.search_many(["first query", "second query", "third query"])

        assert len(results) == 3
        assert [r.query for r in results] == ["first query", "second query", "third query"]

    @pytest.mark.asyncio
    async def test_one_failing_query_does_not_discard_the_others(self):
        """A dead sub-question must not throw away the ones that worked; the
        orchestrator needs to record partial retrieval as exactly that."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 2:
                return httpx.Response(401, json={"detail": "nope"})
            return httpx.Response(200, json=payload(result("https://a.invalid/one")))

        svc, _ = service(handler)
        results = await svc.search_many(["q one", "q two", "q three"])

        assert len(results) == 3
        failures = [r for r in results if isinstance(r, Exception)]
        successes = [r for r in results if not isinstance(r, Exception)]
        assert len(failures) == 1
        assert len(successes) == 2
        assert isinstance(failures[0], SearchAuthenticationFailed)
