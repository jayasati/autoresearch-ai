"""
Semantic Scholar academic search.

**This service returns candidate academic sources. It does not return evidence**, and
it does not download PDFs. See `models.py` for why that distinction is enforced in the
types rather than written in a comment.

Three things make this provider different from Tavily:

**The rate limit is one request per second, cumulative across all endpoints.** That is
tight enough that handling 429 alone is the wrong design: six sub-question searches
fired at once would collect five rejections and spend the run's time budget backing off
from a limit they could simply have respected. So requests are throttled *before*
sending, by a process-wide limiter (`rate_limit.py`) shared across every instance —
because the limit belongs to the account, not to the object. A 429 penalises that shared
limiter, so the whole process waits once instead of each caller discovering the wall
separately.

**A key is optional.** The public API works without one; a key only raises the quota. So
an absent key is not a misconfiguration here, unlike every other credential in this
project, and `is_configured` is not the right question to ask of it.

**An abstract is not guaranteed.** When the provider returns none, we have the title and
the bibliography and no text at all. That is `EvidenceDepth.METADATA`, not `ABSTRACT` —
see `AcademicCandidateSource`. Such papers are kept rather than dropped, because
discarding every paper whose abstract happens not to be indexed would bias retrieval
toward whatever is well indexed.
"""

import asyncio
import logging
from collections.abc import Sequence

from app.core.constants import EvidenceDepth
from app.services.retrieval.exceptions import RetrievalError, SearchResponseInvalid
from app.services.retrieval.http_backend import HttpSearchBackend
from app.services.retrieval.models import (
    AcademicCandidateSource,
    AcademicSearchQuery,
    AcademicSearchResult,
)
from app.services.retrieval.rate_limit import AsyncRateLimiter, get_limiter
from app.utils.urls import source_fingerprint

logger = logging.getLogger(__name__)

PROVIDER = "semantic_scholar"
SEARCH_PATH = "/graph/v1/paper/search"

#: Exactly the fields the brief asks for, plus the three that cost nothing and save a
#: later stage a lookup. Requesting fewer fields is faster and kinder to the provider,
#: so the list is explicit rather than "everything".
PAPER_FIELDS = (
    "paperId",
    "title",
    "authors",
    "year",
    "abstract",
    "url",
    "externalIds",
    "citationCount",
    "influentialCitationCount",
    "venue",
    "publicationTypes",
    "openAccessPdf",
)


#: Hosts that only ever resolve a DOI, so their path *is* the DOI.
DOI_RESOLVER_HOSTS = frozenset({"doi.org", "dx.doi.org", "www.doi.org"})


def normalise_doi(raw: str | None) -> str | None:
    """Reduce a DOI to its bare, lower-cased form.

    Providers hand out DOIs in several shapes: bare, `doi:`-prefixed, and as a URL on
    any of the resolver hosts under either scheme. They all identify the same paper, so
    they must all normalise to one value — otherwise the same paper found twice becomes
    two sources and every per-source metric is computed over a split identity.

    DOIs are case-insensitive by specification, so lower-casing is safe.
    """
    if not raw or not raw.strip():
        return None
    value = raw.strip()

    # Resolve by host rather than by matching a list of prefixes. The prefix approach
    # missed `http://dx.doi.org/...` because the first matching entry won and the
    # scheme/host combinations multiply: {http, https} x {doi.org, dx.doi.org} is four
    # spellings before anyone adds a fifth resolver.
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        remainder = value.split("://", 1)[1]
        host, _, path = remainder.partition("/")
        if host.lower() in DOI_RESOLVER_HOSTS and path:
            value = path

    if lowered.startswith("doi:"):
        value = value[4:]

    value = value.strip().strip("/")
    return value.lower() or None


def normalise_text(raw: object) -> str | None:
    """Collapse whitespace; return None for anything empty or not a string.

    Abstracts arrive with newlines and runs of spaces from PDF extraction. Collapsing
    them now means a later character offset into the abstract counts the characters we
    actually stored.
    """
    if not isinstance(raw, str):
        return None
    collapsed = " ".join(raw.split())
    return collapsed or None


def normalise_authors(raw: object) -> list[str]:
    """Author names in order, dropping entries with no usable name.

    Order is preserved because first authorship is meaningful, and a dropped name
    would silently change what "et al." refers to.
    """
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = normalise_text(entry.get("name"))
        else:
            name = normalise_text(entry)
        if name:
            names.append(name)
    return names


def normalise_count(raw: object) -> int | None:
    """A non-negative integer, or None.

    A negative citation count is not a small error to pass along: it would make the
    `source_diversity` and citation metrics produce impossible values downstream.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = int(raw)
    return value if value >= 0 else None


def normalise_year(raw: object) -> int | None:
    """A plausible publication year, or None.

    Semantic Scholar occasionally returns 0 or a nonsense year for a malformed record.
    Carrying that forward would put a paper outside any year filter and make a
    recency-sensitive topic look unanswerable.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    year = int(raw)
    return year if 1400 <= year <= 2200 else None


class SemanticScholarService(HttpSearchBackend):
    """Searches papers and returns candidate academic sources."""

    provider = PROVIDER

    # --- configuration the shared backend asks for ---------------------------

    @property
    def base_url(self) -> str:
        return self.settings.SEMANTIC_SCHOLAR_BASE_URL

    @property
    def timeout_seconds(self) -> float:
        return self.settings.SEMANTIC_SCHOLAR_TIMEOUT_SECONDS

    @property
    def max_attempts(self) -> int:
        return self.settings.SEMANTIC_SCHOLAR_MAX_ATTEMPTS

    @property
    def backoff_base_seconds(self) -> float:
        return self.settings.SEMANTIC_SCHOLAR_BACKOFF_BASE_SECONDS

    @property
    def backoff_max_seconds(self) -> float:
        return self.settings.SEMANTIC_SCHOLAR_BACKOFF_MAX_SECONDS

    @property
    def credential_setting(self) -> str:
        return "SEMANTIC_SCHOLAR_API_KEY"

    def auth_headers(self) -> dict[str, str]:
        """`x-api-key` when a key is configured, nothing when it is not.

        No `SearchNotConfigured` here, unlike Tavily: the public API is usable without a
        key, so refusing to search would be refusing to do something that works.
        """
        key = self.settings.SEMANTIC_SCHOLAR_API_KEY
        if key and not key.strip().lower().endswith("replace-me"):
            return {"x-api-key": key.strip()}
        return {}

    @property
    def has_api_key(self) -> bool:
        return bool(self.auth_headers())

    @property
    def rate_limiter(self) -> AsyncRateLimiter:
        """The process-wide throttle for this provider.

        Shared across instances because the provider's limit is cumulative across all
        endpoints for the account. Two services in one process are still one account.
        """
        return get_limiter(PROVIDER, self.settings.SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS)

    # --- the public API ------------------------------------------------------

    async def search(
        self,
        query: str | AcademicSearchQuery,
        *,
        limit: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_citation_count: int | None = None,
    ) -> AcademicSearchResult:
        """Search papers by topic and return deduplicated candidates.

        Raises a `RetrievalError` subclass on failure. **Zero results is not a
        failure** — it returns a successful result with no candidates, because
        "searched and found nothing" is a real and different finding from "the search
        broke", and a run must be able to record which happened.
        """
        request = (
            query
            if isinstance(query, AcademicSearchQuery)
            else AcademicSearchQuery(
                query=query,
                limit=limit or self.settings.SEMANTIC_SCHOLAR_MAX_RESULTS,
                year_from=year_from,
                year_to=year_to,
                min_citation_count=min_citation_count,
            )
        )

        limiter = self.rate_limiter
        waited_before = limiter.total_waited

        started = self.now()
        payload, attempts = await self.request_json(
            "GET", SEARCH_PATH, params=self._request_params(request)
        )
        elapsed_ms = (self.now() - started) * 1000
        throttled_ms = (limiter.total_waited - waited_before) * 1000

        raw_results = payload.get("data")
        if raw_results is None and "total" in payload:
            # The provider returns `total: 0` with no `data` key for a query that
            # matched nothing. That is an empty result, not a malformed response.
            raw_results = []
        if not isinstance(raw_results, list):
            raise SearchResponseInvalid(
                PROVIDER,
                "The response had no 'data' list.",
                details={"keys": sorted(payload)[:10]},
            )

        candidates, duplicates, malformed = self._to_candidates(raw_results, request)

        result = AcademicSearchResult(
            query=request.query,
            provider=PROVIDER,
            candidates=candidates,
            requested_limit=request.limit,
            total_available=normalise_count(payload.get("total")),
            raw_result_count=len(raw_results),
            duplicates_removed=duplicates,
            malformed_results=malformed,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
            throttled_ms=throttled_ms,
        )

        logger.info(
            "semantic scholar %r: %d candidates from %d results "
            "(%d duplicates, %d malformed), %d with abstracts, %d metadata-only, "
            "%d with DOIs, in %.0fms (%.0fms throttled), attempt(s)=%d%s",
            request.query,
            len(candidates),
            len(raw_results),
            duplicates,
            malformed,
            len(result.with_abstracts),
            result.metadata_only_count,
            len(result.with_doi),
            elapsed_ms,
            throttled_ms,
            attempts,
            "" if self.has_api_key else " [no API key: shared quota]",
        )
        return result

    async def search_many(
        self, queries: Sequence[str | AcademicSearchQuery]
    ) -> list[AcademicSearchResult | RetrievalError]:
        """Run several searches, returning failures rather than raising them.

        Deliberately **not** concurrency-bounded by a semaphore like the web service:
        the rate limiter already serialises these to one per second, so a semaphore
        would be a second, weaker constraint doing nothing. The searches are still
        launched together so that each one's turn comes up as soon as the limiter
        allows, rather than waiting for the previous `await` to return.

        A failure is returned in place, because one dead sub-question must not discard
        the ones that worked.
        """

        async def run_one(q):
            try:
                return await self.search(q)
            except RetrievalError as exc:
                text = q.query if isinstance(q, AcademicSearchQuery) else q
                logger.warning("semantic scholar %r failed: %s", text, exc.message)
                return exc

        return list(await asyncio.gather(*(run_one(q) for q in queries)))

    # --- the request ---------------------------------------------------------

    def _request_params(self, request: AcademicSearchQuery) -> dict:
        params: dict = {
            "query": request.query,
            "limit": request.limit,
            # Explicit field list: fewer fields is faster and kinder to the provider
            # than asking for everything and discarding most of it.
            "fields": ",".join(PAPER_FIELDS),
        }
        year = request.year_filter
        if year:
            params["year"] = year
        if request.min_citation_count is not None:
            params["minCitationCount"] = request.min_citation_count
        return params

    # --- shaping the results -------------------------------------------------

    @classmethod
    def _to_candidates(
        cls, raw_results: list, request: AcademicSearchQuery
    ) -> tuple[list[AcademicCandidateSource], int, int]:
        """Normalise, deduplicate, and count what was dropped.

        Deduplication is by `source_fingerprint`, which prefers the DOI — so the same
        paper indexed twice under different Semantic Scholar ids collapses to one
        candidate. The first occurrence wins, keeping the provider's ranking.

        The paper id is also tracked separately: two records can share an id without
        sharing a DOI, and either collision is a duplicate.
        """
        candidates: list[AcademicCandidateSource] = []
        seen_fingerprints: set[str] = set()
        seen_paper_ids: set[str] = set()
        duplicates = 0
        malformed = 0

        for index, entry in enumerate(raw_results):
            if not isinstance(entry, dict):
                malformed += 1
                continue
            candidate = cls._to_candidate(entry, rank=index, query=request.query)
            if candidate is None:
                malformed += 1
                continue
            if (
                candidate.fingerprint in seen_fingerprints
                or candidate.paper_id in seen_paper_ids
            ):
                duplicates += 1
                continue
            seen_fingerprints.add(candidate.fingerprint)
            seen_paper_ids.add(candidate.paper_id)
            candidates.append(candidate)

        return candidates, duplicates, malformed

    @staticmethod
    def _to_candidate(
        entry: dict, *, rank: int, query: str
    ) -> AcademicCandidateSource | None:
        """Build one candidate, or `None` if the record is unusable.

        A record with no paper id or no title is skipped rather than raising: one bad
        entry in a page of ten should cost that entry, not the whole search. The caller
        counts what it dropped, so the loss is visible instead of silent.
        """
        paper_id = normalise_text(entry.get("paperId"))
        title = normalise_text(entry.get("title"))
        if not paper_id or not title:
            return None

        external_ids_raw = entry.get("externalIds")
        external_ids: dict[str, str] = {}
        if isinstance(external_ids_raw, dict):
            for key, value in external_ids_raw.items():
                text = normalise_text(str(value)) if value is not None else None
                if text:
                    external_ids[str(key)] = text

        doi = normalise_doi(external_ids.get("DOI"))
        arxiv_id = external_ids.get("ArXiv")
        url = normalise_text(entry.get("url"))

        open_access = entry.get("openAccessPdf")
        pdf_url = (
            normalise_text(open_access.get("url")) if isinstance(open_access, dict) else None
        )

        abstract = normalise_text(entry.get("abstract"))

        # The identity rule, shared with persistence and the web service: DOI, then
        # URL, then the provider's own id. Falling back to the paper id guarantees a
        # fingerprint exists -- a paper with neither DOI nor URL is still a source, and
        # leaving it unfingerprinted would make every such paper collapse into one row.
        fingerprint = source_fingerprint(doi=doi, url=url, external_id=paper_id)
        if fingerprint is None:  # pragma: no cover - paper_id is non-empty here
            return None

        publication_types = entry.get("publicationTypes")
        types = (
            [t for t in (normalise_text(x) for x in publication_types) if t]
            if isinstance(publication_types, list)
            else []
        )

        return AcademicCandidateSource(
            paper_id=paper_id,
            title=title,
            authors=normalise_authors(entry.get("authors")),
            year=normalise_year(entry.get("year")),
            abstract=abstract,
            url=url,
            doi=doi,
            arxiv_id=arxiv_id,
            external_ids=external_ids,
            venue=normalise_text(entry.get("venue")),
            citation_count=normalise_count(entry.get("citationCount")),
            influential_citation_count=normalise_count(
                entry.get("influentialCitationCount")
            ),
            publication_types=types,
            open_access_pdf_url=pdf_url,
            fingerprint=fingerprint,
            rank=rank,
            # The honest depth: an abstract if we got one, otherwise nothing but
            # metadata. Never full text -- no PDF has been downloaded.
            evidence_depth=(
                EvidenceDepth.ABSTRACT if abstract else EvidenceDepth.METADATA
            ),
            query=query,
        )
