"""
Retrieval services: getting candidate sources from the outside world.

**Everything here returns candidates, not evidence.** A search result is a page a
relevance model thinks is topical. Nothing has been fetched, read, or linked to a
claim, so nothing here may be treated as support for anything. See `models.py`.

| Module | Contents |
|---|---|
| `tavily_service.py` | `TavilySearchService` — web search over httpx, with timeout,
  bounded retry, 429 and 5xx handling |
| `models.py` | `WebSearchQuery`, `CandidateSource`, `WebSearchResult` |
| `exceptions.py` | `RetrievalError` and its subclasses, each marked retryable or not |

Planned: `semantic_scholar_service.py` (academic search), `fetcher.py` (URL to clean
text), `chunker.py`, `embedder.py`, `vector_store.py`.
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
from app.services.retrieval.models import (
    CandidateSource,
    SearchDepth,
    WebSearchQuery,
    WebSearchResult,
)
from app.services.retrieval.tavily_service import TavilySearchService

__all__ = [
    "CandidateSource",
    "RetrievalError",
    "SearchAuthenticationFailed",
    "SearchDepth",
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
]
