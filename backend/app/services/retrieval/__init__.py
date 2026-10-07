"""
Retrieval services: getting candidate sources from the outside world.

**Everything here returns candidates, not evidence.** A search result is a page a
relevance model thinks is topical. Nothing has been fetched, read, or linked to a
claim, so nothing here may be treated as support for anything. See `models.py`.

| Module | Contents |
|---|---|
| `tavily_service.py` | `TavilySearchService` — web search |
| `semantic_scholar_service.py` | `SemanticScholarService` — academic search |
| `http_backend.py` | shared retry loop, status mapping, `Retry-After`, backoff |
| `rate_limit.py` | process-wide throttle, for a provider that needs one |
| `models.py` | queries, candidates and results for both providers |
| `exceptions.py` | `RetrievalError` and subclasses, each marked retryable or not |

Neither provider downloads anything. Tavily returns snippets; Semantic Scholar returns
abstracts when it has them and bibliographic metadata when it does not. Fetching a page
or a PDF belongs to a later stage, and `evidence_depth` records which of those three
situations a candidate is actually in.

Planned: `fetcher.py` (URL to clean text), `chunker.py`, `embedder.py`,
`vector_store.py`.
"""

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
from app.services.retrieval.http_backend import HttpSearchBackend
from app.services.retrieval.models import (
    AcademicCandidateSource,
    AcademicSearchQuery,
    AcademicSearchResult,
    CandidateSource,
    SearchDepth,
    WebSearchQuery,
    WebSearchResult,
)
from app.services.retrieval.rate_limit import AsyncRateLimiter, get_limiter, reset_limiters
from app.services.retrieval.semantic_scholar_service import SemanticScholarService
from app.services.retrieval.tavily_service import TavilySearchService

__all__ = [
    "AcademicCandidateSource",
    "AcademicSearchQuery",
    "AcademicSearchResult",
    "AsyncRateLimiter",
    "CandidateSource",
    "HttpSearchBackend",
    "RetrievalError",
    "SearchAuthenticationFailed",
    "SearchDepth",
    "SemanticScholarService",
    "SearchNotConfigured",
    "SearchProviderUnavailable",
    "SearchRateLimited",
    "SearchRequestInvalid",
    "SearchResponseInvalid",
    "SearchTimeout",
    "SearchUnreachable",
    "TavilySearchService",
    "WebSearchQuery",
    "WebSearchResult",
    "get_limiter",
    "reset_limiters",
]
