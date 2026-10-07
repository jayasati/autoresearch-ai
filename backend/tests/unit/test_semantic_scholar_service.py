"""
Semantic Scholar search, against a mocked transport.

Same approach as the Tavily tests: a real `httpx.AsyncClient` over
`httpx.MockTransport`, so the status mapping, header handling and JSON decoding under
test are the code that runs in production.

Both `sleep` and the rate limiter are controlled, so the 1-request-per-second throttle
is asserted rather than waited for. Without that, this file would take a minute to run
and nobody would run it.

Placeholder data uses `10.0000/invalid.*` DOIs and `example.invalid` URLs, so nothing
here could be mistaken for a real paper.
"""

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.constants import EvidenceDepth, SourceType
from app.services.retrieval import (
    AcademicCandidateSource,
    AcademicSearchQuery,
    SearchAuthenticationFailed,
    SearchProviderUnavailable,
    SearchRateLimited,
    SearchResponseInvalid,
    SearchTimeout,
    SearchUnreachable,
    SemanticScholarService,
    reset_limiters,
)
from app.services.retrieval.semantic_scholar_service import (
    normalise_authors,
    normalise_count,
    normalise_doi,
    normalise_text,
    normalise_year,
)

QUERY = "retrieval augmented generation hallucination"


@pytest.fixture(autouse=True)
def _isolate_limiter():
    """The limiter is process-wide, so one test's throttle would otherwise leak into
    the next and make results depend on test order."""
    reset_limiters()
    yield
    reset_limiters()


def settings(**overrides) -> Settings:
    base = {
        "SEMANTIC_SCHOLAR_API_KEY": None,
        "SEMANTIC_SCHOLAR_MAX_ATTEMPTS": 3,
        "SEMANTIC_SCHOLAR_BACKOFF_BASE_SECONDS": 0.01,
        "SEMANTIC_SCHOLAR_BACKOFF_MAX_SECONDS": 0.05,
        "SEMANTIC_SCHOLAR_TIMEOUT_SECONDS": 5.0,
        # Zero so the throttle does not slow the tests. Its behaviour is asserted
        # separately in test_rate_limit.py, with a fake clock.
        "SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS": 0.0,
        "APP_ENV": "test",
    }
    return Settings(**{**base, **overrides})


def paper(
    paper_id: str = "p1",
    *,
    title: str = "A Paper Under Test",
    abstract: str | None = "An abstract under test.",
    year: int | None = 2021,
    doi: str | None = "10.0000/invalid.one",
    authors: list | None = None,
    citations: int | None = 12,
    **extra,
) -> dict:
    entry: dict = {
        "paperId": paper_id,
        "title": title,
        "abstract": abstract,
        "year": year,
        "authors": authors if authors is not None else [{"name": "Jane Smith"}, {"name": "Bo Li"}],
        "url": f"https://example.invalid/paper/{paper_id}",
        "citationCount": citations,
        "venue": "A Venue Under Test",
    }
    if doi is not None:
        entry["externalIds"] = {"DOI": doi}
    entry.update(extra)
    return entry


def payload(*papers, total: int | None = None) -> dict:
    return {
        "total": total if total is not None else len(papers),
        "offset": 0,
        "data": list(papers),
    }


class Recorder:
    """A MockTransport handler that records requests and replays queued responses."""

    def __init__(self, *responses):
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

    def params(self, index: int = 0) -> dict:
        return dict(self.requests[index].url.params)


def service(handler, **setting_overrides):
    """A service on a mock transport, with sleeps recorded instead of taken."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.semanticscholar.invalid",
    )
    svc = SemanticScholarService(
        settings=settings(**setting_overrides), client=client, sleep=fake_sleep
    )
    return svc, slept


def ok(*papers, total: int | None = None) -> Recorder:
    return Recorder(httpx.Response(200, json=payload(*papers, total=total)))


# --------------------------------------------------------------------------- #
# 1. Successful search
# --------------------------------------------------------------------------- #


class TestSuccessfulSearch:
    @pytest.mark.asyncio
    async def test_returns_candidates_in_provider_order(self):
        handler = ok(
            paper("p1", doi="10.0000/invalid.one"),
            paper("p2", doi="10.0000/invalid.two"),
            paper("p3", doi="10.0000/invalid.three"),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 3
        assert [c.rank for c in found.candidates] == [0, 1, 2]
        assert [c.paper_id for c in found.candidates] == ["p1", "p2", "p3"]

    @pytest.mark.asyncio
    async def test_every_requested_field_is_captured(self):
        """The brief's list: title, authors, year, abstract, URL, DOI, citation count,
        paper id."""
        handler = ok(paper("p9", title="Grounding Study", year=2023, citations=44))
        svc, _ = service(handler)
        candidate = (await svc.search(QUERY)).candidates[0]

        assert candidate.paper_id == "p9"
        assert candidate.title == "Grounding Study"
        assert candidate.authors == ["Jane Smith", "Bo Li"]
        assert candidate.year == 2023
        assert candidate.abstract == "An abstract under test."
        assert candidate.url == "https://example.invalid/paper/p9"
        assert candidate.doi == "10.0000/invalid.one"
        assert candidate.citation_count == 44

    @pytest.mark.asyncio
    async def test_the_candidate_is_typed_as_an_academic_source(self):
        svc, _ = service(ok(paper()))
        candidate = (await svc.search(QUERY)).candidates[0]
        assert candidate.source_type is SourceType.ACADEMIC

    @pytest.mark.asyncio
    async def test_a_paper_with_an_abstract_has_abstract_depth(self):
        svc, _ = service(ok(paper(abstract="Something to read.")))
        candidate = (await svc.search(QUERY)).candidates[0]
        assert candidate.evidence_depth is EvidenceDepth.ABSTRACT
        assert candidate.has_text is True

    @pytest.mark.asyncio
    async def test_a_paper_with_no_abstract_is_metadata_only_not_abstract(self):
        """The honest depth. Calling it ABSTRACT would be a small lie that the
        groundedness metrics would faithfully repeat later."""
        svc, _ = service(ok(paper(abstract=None)))
        found = await svc.search(QUERY)
        candidate = found.candidates[0]

        assert candidate.evidence_depth is EvidenceDepth.METADATA
        assert candidate.has_text is False
        assert found.metadata_only_count == 1
        assert found.with_abstracts == []

    @pytest.mark.asyncio
    async def test_a_paper_with_no_abstract_is_kept_not_dropped(self):
        """Discarding every paper whose abstract is not indexed would bias retrieval
        toward whatever happens to be well indexed."""
        handler = ok(
            paper("p1", abstract="Has one.", doi="10.0000/invalid.one"),
            paper("p2", abstract=None, doi="10.0000/invalid.two"),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 2
        assert found.metadata_only_count == 1
        assert len(found.with_abstracts) == 1

    @pytest.mark.asyncio
    async def test_candidates_carry_a_retrieval_timestamp(self):
        svc, _ = service(ok(paper()))
        found = await svc.search(QUERY)
        assert found.candidates[0].retrieved_at.tzinfo is not None
        assert found.retrieved_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_timing_attempts_and_total_are_reported(self):
        svc, _ = service(ok(paper(), total=4821))
        found = await svc.search(QUERY)

        assert found.elapsed_ms >= 0
        assert found.attempts == 1
        assert found.total_available == 4821

    @pytest.mark.asyncio
    async def test_the_open_access_pdf_url_is_recorded_but_nothing_is_downloaded(self):
        """Keeping the address costs nothing and saves the fetch stage a lookup.
        Only one request was made, and it was the search."""
        handler = ok(
            paper(openAccessPdf={"url": "https://example.invalid/p.pdf", "status": "GOLD"})
        )
        svc, _ = service(handler)
        candidate = (await svc.search(QUERY)).candidates[0]

        assert candidate.open_access_pdf_url == "https://example.invalid/p.pdf"
        assert handler.call_count == 1
        assert ".pdf" not in str(handler.requests[0].url)

    @pytest.mark.asyncio
    async def test_arxiv_and_other_external_ids_are_kept(self):
        handler = ok(
            paper(externalIds={"DOI": "10.0000/invalid.x", "ArXiv": "2401.00001", "CorpusId": 99})
        )
        svc, _ = service(handler)
        candidate = (await svc.search(QUERY)).candidates[0]

        assert candidate.arxiv_id == "2401.00001"
        assert candidate.external_ids["CorpusId"] == "99"


class TestTheRequestSent:
    @pytest.mark.asyncio
    async def test_the_result_limit_is_configurable(self):
        handler = ok(paper())
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MAX_RESULTS=25)
        await svc.search(QUERY)
        assert handler.params()["limit"] == "25"

    @pytest.mark.asyncio
    async def test_a_per_call_limit_beats_the_configured_default(self):
        handler = ok(paper())
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MAX_RESULTS=25)
        await svc.search(QUERY, limit=3)
        assert handler.params()["limit"] == "3"

    @pytest.mark.asyncio
    async def test_only_the_needed_fields_are_requested(self):
        """Fewer fields is faster and kinder to the provider than asking for everything
        and discarding most of it."""
        handler = ok(paper())
        svc, _ = service(handler)
        await svc.search(QUERY)

        fields = handler.params()["fields"].split(",")
        for required in ("paperId", "title", "authors", "year", "abstract", "url",
                         "externalIds", "citationCount"):
            assert required in fields

    @pytest.mark.asyncio
    async def test_no_api_key_header_is_sent_when_none_is_configured(self):
        """The public API works without one, so an absent key is not an error here."""
        handler = ok(paper())
        svc, _ = service(handler, SEMANTIC_SCHOLAR_API_KEY=None)
        await svc.search(QUERY)

        assert "x-api-key" not in handler.requests[0].headers
        assert svc.has_api_key is False

    @pytest.mark.asyncio
    async def test_the_api_key_is_sent_as_a_header_when_configured(self):
        handler = ok(paper())
        svc, _ = service(handler, SEMANTIC_SCHOLAR_API_KEY="s2-test-key")
        await svc.search(QUERY)

        assert handler.requests[0].headers["x-api-key"] == "s2-test-key"
        assert svc.has_api_key is True

    @pytest.mark.asyncio
    async def test_a_placeholder_key_is_treated_as_absent(self):
        handler = ok(paper())
        svc, _ = service(handler, SEMANTIC_SCHOLAR_API_KEY="s2-replace-me")
        await svc.search(QUERY)
        assert "x-api-key" not in handler.requests[0].headers

    @pytest.mark.asyncio
    async def test_the_year_range_is_sent_in_the_providers_format(self):
        handler = ok(paper())
        svc, _ = service(handler)
        await svc.search(QUERY, year_from=2020, year_to=2024)
        assert handler.params()["year"] == "2020-2024"

    @pytest.mark.asyncio
    async def test_no_year_parameter_is_sent_when_no_range_is_given(self):
        handler = ok(paper())
        svc, _ = service(handler)
        await svc.search(QUERY)
        assert "year" not in handler.params()

    @pytest.mark.asyncio
    async def test_a_minimum_citation_count_is_forwarded(self):
        handler = ok(paper())
        svc, _ = service(handler)
        await svc.search(QUERY, min_citation_count=10)
        assert handler.params()["minCitationCount"] == "10"


# --------------------------------------------------------------------------- #
# 2. No results
# --------------------------------------------------------------------------- #


class TestNoResults:
    @pytest.mark.asyncio
    async def test_an_empty_data_list_is_a_success(self):
        """"Searched and found nothing" is a real finding, and a different one from
        "the search broke"."""
        svc, _ = service(Recorder(httpx.Response(200, json={"total": 0, "data": []})))
        found = await svc.search(QUERY)

        assert found.candidates == []
        assert found.is_empty is True
        assert found.raw_result_count == 0

    @pytest.mark.asyncio
    async def test_a_response_with_total_zero_and_no_data_key_is_also_empty(self):
        """The provider omits `data` entirely for a query that matched nothing. That is
        an empty result, not a malformed response."""
        svc, _ = service(Recorder(httpx.Response(200, json={"total": 0})))
        found = await svc.search(QUERY)

        assert found.is_empty is True
        assert found.total_available == 0

    @pytest.mark.asyncio
    async def test_an_empty_search_still_reports_its_query_and_timing(self):
        svc, _ = service(Recorder(httpx.Response(200, json={"total": 0, "data": []})))
        found = await svc.search(QUERY)

        assert found.query == QUERY
        assert found.attempts == 1
        assert found.metadata_only_count == 0

    @pytest.mark.asyncio
    async def test_records_with_no_id_or_title_are_counted_as_malformed(self):
        """Zero candidates from five records is a different fact from zero records, and
        the difference has to be visible."""
        handler = Recorder(
            httpx.Response(
                200,
                json={
                    "total": 5,
                    "data": [
                        {"title": "No id"},
                        {"paperId": "p1"},
                        {"paperId": "", "title": "Blank id"},
                        "not even a dict",
                        None,
                    ],
                },
            )
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert found.candidates == []
        assert found.raw_result_count == 5
        assert found.malformed_results == 5


# --------------------------------------------------------------------------- #
# 3. HTTP 429
# --------------------------------------------------------------------------- #


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_a_429_is_retried_and_can_succeed(self):
        handler = Recorder(
            httpx.Response(429, json={"message": "Too Many Requests"}),
            httpx.Response(200, json=payload(paper())),
        )
        svc, slept = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 1
        assert found.attempts == 2
        assert handler.call_count == 2
        assert len(slept) == 1

    @pytest.mark.asyncio
    async def test_exhausting_the_attempts_raises_rate_limited(self):
        handler = Recorder(httpx.Response(429, json={"message": "Too Many Requests"}))
        svc, slept = service(handler, SEMANTIC_SCHOLAR_MAX_ATTEMPTS=3)

        with pytest.raises(SearchRateLimited) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 3
        assert len(slept) == 2  # no sleep after the final attempt
        assert caught.value.status_code == 429
        assert caught.value.code == "rate_limited"
        assert caught.value.service == "semantic_scholar"

    @pytest.mark.asyncio
    async def test_the_providers_retry_after_is_obeyed(self):
        handler = Recorder(
            httpx.Response(429, headers={"Retry-After": "0.04"}, json={}),
            httpx.Response(200, json=payload(paper())),
        )
        svc, slept = service(handler, SEMANTIC_SCHOLAR_BACKOFF_MAX_SECONDS=5.0)
        await svc.search(QUERY)
        assert slept == [0.04]

    @pytest.mark.asyncio
    async def test_a_429_penalises_the_shared_limiter_not_just_this_caller(self):
        """Backing off only the caller that was rejected leaves the others queued to
        make the same mistake in turn. The whole process should wait once.

        The penalty is recorded rather than served: by the time the search returns, the
        retry has already waited it out, so checking `seconds_until_next` afterwards
        would find zero and prove nothing. What matters is that the shared limiter was
        told, with the provider's own figure.
        """
        handler = Recorder(
            httpx.Response(429, headers={"Retry-After": "2"}, json={}),
            httpx.Response(200, json=payload(paper())),
        )
        svc, _ = service(
            handler,
            SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS=0.0,
            SEMANTIC_SCHOLAR_BACKOFF_MAX_SECONDS=5.0,
        )

        penalties: list[float] = []
        limiter = svc.rate_limiter
        limiter.penalise = penalties.append  # type: ignore[method-assign]

        await svc.search(QUERY)

        assert penalties == [2.0], "the shared limiter was not told about the 429"

    @pytest.mark.asyncio
    async def test_a_5xx_does_not_penalise_the_limiter(self):
        """A server error is not a rate limit. Slowing every caller in the process for
        it would turn one bad response into a project-wide delay."""
        handler = Recorder(
            httpx.Response(503, text="try later"),
            httpx.Response(200, json=payload(paper())),
        )
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS=0.0)

        penalties: list[float] = []
        svc.rate_limiter.penalise = penalties.append  # type: ignore[method-assign]

        await svc.search(QUERY)
        assert penalties == []

    @pytest.mark.asyncio
    async def test_the_throttle_runs_before_the_request_not_after_a_rejection(self):
        """With a limit of one per second, retrying straight into it is how a tight
        budget gets spent on rejections instead of results."""
        handler = ok(paper())
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS=0.0)
        limiter = svc.rate_limiter
        before = limiter.acquisitions
        await svc.search(QUERY)
        assert limiter.acquisitions == before + 1

    @pytest.mark.asyncio
    async def test_throttled_time_is_reported_separately_from_network_time(self):
        """A search that spent four seconds queued, not on the network, is worth
        distinguishing in a benchmark's timing."""
        svc, _ = service(ok(paper()))
        found = await svc.search(QUERY)
        assert found.throttled_ms >= 0


# --------------------------------------------------------------------------- #
# 4. Timeout
# --------------------------------------------------------------------------- #


class TestTimeout:
    @pytest.mark.asyncio
    async def test_a_timeout_is_retried_and_can_succeed(self):
        handler = Recorder(
            httpx.ReadTimeout("too slow"),
            httpx.Response(200, json=payload(paper())),
        )
        svc, slept = service(handler)
        found = await svc.search(QUERY)

        assert found.attempts == 2
        assert len(slept) == 1

    @pytest.mark.asyncio
    async def test_exhausting_the_attempts_raises_search_timeout(self):
        handler = Recorder(httpx.ReadTimeout("too slow"))
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MAX_ATTEMPTS=2)

        with pytest.raises(SearchTimeout) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 2
        assert caught.value.status_code == 504
        assert caught.value.retryable is True

    @pytest.mark.asyncio
    async def test_the_configured_timeout_is_named_in_the_error(self):
        handler = Recorder(httpx.ConnectTimeout("no connect"))
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MAX_ATTEMPTS=1,
                         SEMANTIC_SCHOLAR_TIMEOUT_SECONDS=9.0)

        with pytest.raises(SearchTimeout) as caught:
            await svc.search(QUERY)

        assert "9s" in caught.value.message
        assert caught.value.details["timeout_seconds"] == 9.0

    @pytest.mark.asyncio
    async def test_the_timeout_is_applied_to_the_client(self):
        svc = SemanticScholarService(settings=settings(SEMANTIC_SCHOLAR_TIMEOUT_SECONDS=17.5))
        try:
            assert svc.client.timeout.read == 17.5
            assert svc.client.timeout.connect == 17.5
        finally:
            await svc.aclose()


# --------------------------------------------------------------------------- #
# 5. Malformed response
# --------------------------------------------------------------------------- #


class TestMalformedResponse:
    @pytest.mark.asyncio
    async def test_a_non_json_body_is_an_error(self):
        svc, _ = service(Recorder(httpx.Response(200, text="<html>not json</html>")))
        with pytest.raises(SearchResponseInvalid):
            await svc.search(QUERY)

    @pytest.mark.asyncio
    async def test_a_json_array_instead_of_an_object_is_an_error(self):
        svc, _ = service(Recorder(httpx.Response(200, json=[1, 2, 3])))
        with pytest.raises(SearchResponseInvalid, match="Expected a JSON object"):
            await svc.search(QUERY)

    @pytest.mark.asyncio
    async def test_a_response_with_no_data_and_no_total_is_an_error(self):
        """Reporting "no papers found" for an unreadable response would let the
        benchmark read a parsing failure as a property of the topic."""
        svc, _ = service(Recorder(httpx.Response(200, json={"offset": 0})))
        with pytest.raises(SearchResponseInvalid, match="no 'data' list"):
            await svc.search(QUERY)

    @pytest.mark.asyncio
    async def test_data_of_the_wrong_type_is_an_error(self):
        svc, _ = service(Recorder(httpx.Response(200, json={"total": 1, "data": "nope"})))
        with pytest.raises(SearchResponseInvalid):
            await svc.search(QUERY)

    @pytest.mark.asyncio
    async def test_a_malformed_response_is_not_retried(self):
        """A malformed body is not a transient condition."""
        handler = Recorder(httpx.Response(200, text="not json"))
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MAX_ATTEMPTS=3)
        with pytest.raises(SearchResponseInvalid):
            await svc.search(QUERY)
        assert handler.call_count == 1

    @pytest.mark.asyncio
    async def test_one_malformed_record_does_not_discard_the_good_ones(self):
        handler = Recorder(
            httpx.Response(
                200,
                json={
                    "total": 3,
                    "data": [
                        paper("p1", doi="10.0000/invalid.one"),
                        {"no": "id or title"},
                        paper("p3", doi="10.0000/invalid.three"),
                    ],
                },
            )
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 2
        assert found.malformed_results == 1


class TestOtherFailures:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_a_5xx_is_retried_then_reported_as_unavailable(self, status):
        handler = Recorder(httpx.Response(status, text="upstream broke"))
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MAX_ATTEMPTS=2)

        with pytest.raises(SearchProviderUnavailable) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 2
        assert caught.value.status_code == 502

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_an_auth_failure_is_never_retried_and_names_the_setting(self, status):
        handler = Recorder(httpx.Response(status, json={"message": "bad key"}))
        svc, slept = service(handler, SEMANTIC_SCHOLAR_API_KEY="s2-wrong",
                             SEMANTIC_SCHOLAR_MAX_ATTEMPTS=3)

        with pytest.raises(SearchAuthenticationFailed) as caught:
            await svc.search(QUERY)

        assert handler.call_count == 1
        assert slept == []
        assert caught.value.details["setting"] == "SEMANTIC_SCHOLAR_API_KEY"

    @pytest.mark.asyncio
    async def test_a_connection_failure_is_retried_then_reported_as_unreachable(self):
        handler = Recorder(httpx.ConnectError("dns failure"))
        svc, _ = service(handler, SEMANTIC_SCHOLAR_MAX_ATTEMPTS=2)
        with pytest.raises(SearchUnreachable):
            await svc.search(QUERY)
        assert handler.call_count == 2

    @pytest.mark.asyncio
    async def test_the_error_detail_never_echoes_the_api_key(self):
        handler = Recorder(httpx.Response(500, json={"message": "failed on " + "x" * 500}))
        svc, _ = service(handler, SEMANTIC_SCHOLAR_API_KEY="s2-secret-key",
                         SEMANTIC_SCHOLAR_MAX_ATTEMPTS=1)

        with pytest.raises(SearchProviderUnavailable) as caught:
            await svc.search(QUERY)

        assert "s2-secret-key" not in caught.value.message
        assert len(caught.value.message) < 400


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_the_same_doi_under_two_paper_ids_is_one_candidate(self):
        """Semantic Scholar indexes preprints and published versions separately. They
        are one paper, and counting them twice would inflate source diversity."""
        handler = ok(
            paper("p1", doi="10.0000/invalid.same"),
            paper("p2", doi="10.0000/invalid.same"),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 1
        assert found.duplicates_removed == 1

    @pytest.mark.asyncio
    async def test_doi_spellings_are_normalised_before_comparison(self):
        handler = ok(
            paper("p1", doi="10.0000/INVALID.same"),
            paper("p2", doi="https://doi.org/10.0000/invalid.same"),
            paper("p3", doi="doi:10.0000/invalid.same"),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 1
        assert found.duplicates_removed == 2

    @pytest.mark.asyncio
    async def test_the_same_paper_id_twice_is_one_candidate(self):
        """Two records can share an id without sharing a DOI; either collision is a
        duplicate."""
        handler = ok(paper("p1", doi=None), paper("p1", doi=None))
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 1
        assert found.duplicates_removed == 1

    @pytest.mark.asyncio
    async def test_the_first_occurrence_wins_so_ranking_survives(self):
        handler = ok(
            paper("p1", doi="10.0000/invalid.same", citations=99),
            paper("p2", doi="10.0000/invalid.same", citations=1),
        )
        svc, _ = service(handler)
        candidate = (await svc.search(QUERY)).candidates[0]

        assert candidate.paper_id == "p1"
        assert candidate.citation_count == 99

    @pytest.mark.asyncio
    async def test_different_papers_are_not_merged(self):
        handler = ok(
            paper("p1", doi="10.0000/invalid.one"),
            paper("p2", doi="10.0000/invalid.two"),
        )
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 2
        assert found.duplicates_removed == 0

    @pytest.mark.asyncio
    async def test_a_paper_with_no_doi_still_gets_a_fingerprint(self):
        """Otherwise every paper without a DOI would collapse into one source row."""
        handler = ok(paper("p1", doi=None), paper("p2", doi=None))
        svc, _ = service(handler)
        found = await svc.search(QUERY)

        assert len(found.candidates) == 2
        assert len({c.fingerprint for c in found.candidates}) == 2

    @pytest.mark.asyncio
    async def test_the_fingerprint_matches_what_persistence_would_compute(self):
        """The shared identity rule: if retrieval and the database disagreed, one paper
        found by two runs would become two source rows."""
        svc, _ = service(ok(paper(doi="10.0000/invalid.match")))
        candidate = (await svc.search(QUERY)).candidates[0]
        assert candidate.to_source_create().fingerprint == candidate.fingerprint


# --------------------------------------------------------------------------- #
# Metadata normalisation
# --------------------------------------------------------------------------- #


class TestMetadataNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            "10.1234/abc",
            "10.1234/ABC",
            "https://doi.org/10.1234/abc",
            "http://doi.org/10.1234/abc",
            "https://dx.doi.org/10.1234/abc",
            "http://dx.doi.org/10.1234/abc",
            "doi:10.1234/abc",
            "DOI:10.1234/ABC",
            "  10.1234/abc/  ",
        ],
    )
    def test_every_doi_spelling_normalises_to_one_value(self, raw):
        assert normalise_doi(raw) == "10.1234/abc"

    def test_a_url_on_a_non_resolver_host_is_left_alone(self):
        """Only doi.org and its aliases are guaranteed to have the DOI as their path."""
        assert normalise_doi("https://example.com/10.1234/abc") == (
            "https://example.com/10.1234/abc"
        )

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_an_absent_doi_is_none(self, raw):
        assert normalise_doi(raw) is None

    def test_abstract_whitespace_is_collapsed(self):
        """So a later character offset into the abstract counts the characters we
        actually stored."""
        assert normalise_text("Line one.\n\n  Line   two.\t") == "Line one. Line two."

    @pytest.mark.parametrize("raw", [None, "", "   ", 42, [], {}])
    def test_non_text_becomes_none(self, raw):
        assert normalise_text(raw) is None

    def test_author_order_is_preserved_and_empties_dropped(self):
        """First authorship is meaningful, and a dropped name would silently change
        what "et al." refers to."""
        authors = normalise_authors(
            [{"name": "Jane  Smith"}, {"name": ""}, {"name": None}, "Bo Li", 42, None]
        )
        assert authors == ["Jane Smith", "Bo Li"]

    @pytest.mark.parametrize("raw", [None, "nope", {}])
    def test_malformed_authors_become_an_empty_list(self, raw):
        assert normalise_authors(raw) == []

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, 0), (12, 12), (12.0, 12), (-5, None), (None, None), ("many", None), (True, None)],
    )
    def test_citation_counts_are_non_negative_integers_or_none(self, raw, expected):
        assert normalise_count(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(2021, 2021), (0, None), (9999, None), (1399, None), (None, None), ("2021", None)],
    )
    def test_implausible_years_become_none(self, raw, expected):
        """A nonsense year would put a paper outside every year filter and make a
        recency-sensitive topic look unanswerable."""
        assert normalise_year(raw) == expected


# --------------------------------------------------------------------------- #
# Candidates are not evidence
# --------------------------------------------------------------------------- #


class TestCandidatesAreNotEvidence:
    def test_the_model_has_no_field_asserting_support(self):
        forbidden = {"relation", "verdict", "supports", "is_evidence", "claim_id", "confidence"}
        assert forbidden & set(AcademicCandidateSource.model_fields) == set()

    def test_full_text_depth_is_rejected_because_nothing_was_downloaded(self):
        with pytest.raises(ValidationError, match="has not been fetched"):
            AcademicCandidateSource(
                paper_id="p1",
                title="T",
                fingerprint="f" * 64,
                rank=0,
                query=QUERY,
                evidence_depth=EvidenceDepth.FULL_TEXT,
            )

    def test_depth_cannot_claim_an_abstract_that_is_not_there(self):
        """The check that keeps the groundedness metrics honest at the source."""
        with pytest.raises(ValidationError, match="must be metadata"):
            AcademicCandidateSource(
                paper_id="p1",
                title="T",
                abstract=None,
                fingerprint="f" * 64,
                rank=0,
                query=QUERY,
                evidence_depth=EvidenceDepth.ABSTRACT,
            )

    def test_depth_cannot_deny_an_abstract_that_is_there(self):
        with pytest.raises(ValidationError, match="must be abstract"):
            AcademicCandidateSource(
                paper_id="p1",
                title="T",
                abstract="Present.",
                fingerprint="f" * 64,
                rank=0,
                query=QUERY,
                evidence_depth=EvidenceDepth.METADATA,
            )

    def test_the_only_bridge_to_the_database_is_a_source_row(self):
        from app.schemas.source import SourceCreate

        candidate = AcademicCandidateSource(
            paper_id="p1",
            title="T",
            doi="10.0000/invalid.one",
            fingerprint="f" * 64,
            rank=0,
            query=QUERY,
            evidence_depth=EvidenceDepth.METADATA,
        )
        created = candidate.to_source_create()
        assert isinstance(created, SourceCreate)
        assert created.source_type is SourceType.ACADEMIC

    def test_the_module_states_the_distinction(self):
        from app.services.retrieval import semantic_scholar_service

        doc = (semantic_scholar_service.__doc__ or "").lower()
        assert "not return evidence" in doc
        assert "does not download pdfs" in doc


# --------------------------------------------------------------------------- #
# Query validation and multiple searches
# --------------------------------------------------------------------------- #


class TestQueryValidation:
    def test_a_blank_query_is_rejected(self):
        with pytest.raises(ValidationError):
            AcademicSearchQuery(query="   ")

    def test_whitespace_is_collapsed(self):
        assert AcademicSearchQuery(query="  does  retrieval help ").query == "does retrieval help"

    def test_an_inverted_year_range_is_rejected(self):
        with pytest.raises(ValidationError, match="is after"):
            AcademicSearchQuery(query="a query", year_from=2024, year_to=2020)

    def test_the_limit_is_bounded(self):
        with pytest.raises(ValidationError):
            AcademicSearchQuery(query="a query", limit=500)

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, None),
            ({"year_from": 2020}, "2020-"),
            ({"year_to": 2024}, "-2024"),
            ({"year_from": 2020, "year_to": 2024}, "2020-2024"),
            ({"year_from": 2021, "year_to": 2021}, "2021"),
        ],
    )
    def test_the_year_filter_is_built_in_the_providers_format(self, kwargs, expected):
        assert AcademicSearchQuery(query="a query", **kwargs).year_filter == expected


class TestSearchMany:
    @pytest.mark.asyncio
    async def test_several_queries_return_in_order(self):
        svc, _ = service(ok(paper()))
        results = await svc.search_many(["first", "second", "third"])

        assert len(results) == 3
        assert [r.query for r in results] == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_one_failing_query_does_not_discard_the_others(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 2:
                return httpx.Response(401, json={"message": "nope"})
            return httpx.Response(200, json=payload(paper()))

        svc, _ = service(handler, SEMANTIC_SCHOLAR_API_KEY="s2-wrong")
        results = await svc.search_many(["q one", "q two", "q three"])

        failures = [r for r in results if isinstance(r, Exception)]
        successes = [r for r in results if not isinstance(r, Exception)]
        assert len(failures) == 1
        assert len(successes) == 2
        assert isinstance(failures[0], SearchAuthenticationFailed)

    @pytest.mark.asyncio
    async def test_every_query_passes_through_the_shared_throttle(self):
        """Even launched together, they are serialised by the limiter rather than by a
        semaphore -- which would be a second, weaker constraint doing nothing."""
        svc, _ = service(ok(paper()))
        limiter = svc.rate_limiter
        before = limiter.acquisitions
        await svc.search_many(["a", "b", "c"])
        assert limiter.acquisitions == before + 3
