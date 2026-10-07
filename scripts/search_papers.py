"""
Manual check: run one real Semantic Scholar search and print the candidate papers.

    backend/.venv/Scripts/python.exe scripts/search_papers.py "retrieval augmented generation"

Options:
    --limit N              how many papers to ask for
    --from YEAR            earliest publication year
    --to YEAR              latest publication year
    --min-citations N      drop papers below this citation count
    --json                 print the raw AcademicSearchResult as JSON

A key is optional. The public API works without one; `SEMANTIC_SCHOLAR_API_KEY` only
raises the quota. Either way the client throttles itself to below one request per
second, because that is the documented limit and it is cumulative across all endpoints.

What it prints are **candidate academic sources, not evidence**. Nothing has been
linked to a claim, and **no PDF is downloaded** — where a paper has an open-access PDF
the address is recorded, which is a different thing from fetching it.

Note the `depth` column. `abstract` means the provider gave us the abstract; `metadata`
means it did not, so there is no text for that paper at all and it cannot support a
claim until a later stage fetches one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.config import get_settings  # noqa: E402
from app.services.retrieval import RetrievalError, SemanticScholarService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one Semantic Scholar search and print the candidate papers.",
    )
    parser.add_argument("query", help="Research topic to search for.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--from", dest="year_from", type=int, default=None)
    parser.add_argument("--to", dest="year_to", type=int, default=None)
    parser.add_argument("--min-citations", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead.")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()

    async with SemanticScholarService(settings=settings) as service:
        print(f"Searching papers: {args.query!r}")
        print(
            f"  limit={args.limit or settings.SEMANTIC_SCHOLAR_MAX_RESULTS} "
            f"timeout={settings.SEMANTIC_SCHOLAR_TIMEOUT_SECONDS:g}s "
            f"attempts<={settings.SEMANTIC_SCHOLAR_MAX_ATTEMPTS} "
            f"throttle>={settings.SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS:g}s/request"
        )
        print(
            "  api key: "
            + ("set (higher quota)" if service.has_api_key else "none (shared public quota)")
        )
        print()

        try:
            result = await service.search(
                args.query,
                limit=args.limit,
                year_from=args.year_from,
                year_to=args.year_to,
                min_citation_count=args.min_citations,
            )
        except RetrievalError as exc:
            print(f"  FAILED [{exc.code}] {exc.message}")
            for key, value in (exc.details or {}).items():
                print(f"    {key}: {value}")
            print(f"    retryable: {exc.retryable}")
            return 1

    if args.json:
        print(result.model_dump_json(indent=2))
        return 0

    print(
        f"{len(result.candidates)} candidate paper(s) "
        f"from {result.raw_result_count} result(s)"
        + (f" of {result.total_available} matching" if result.total_available else "")
        + f" in {result.elapsed_ms:.0f}ms"
        + (f" ({result.throttled_ms:.0f}ms throttled)" if result.throttled_ms else "")
        + f", attempt(s)={result.attempts}"
    )
    if result.duplicates_removed:
        print(f"  {result.duplicates_removed} duplicate(s) removed (same DOI or paper id)")
    if result.malformed_results:
        print(f"  {result.malformed_results} record(s) dropped for having no id or title")
    print(
        f"  {len(result.with_abstracts)} with abstracts, "
        f"{result.metadata_only_count} metadata-only, "
        f"{len(result.with_doi)} with DOIs"
    )
    print()

    if result.is_empty:
        # Not an error. "Searched and found nothing" is a real finding.
        print("  No candidates. The search succeeded and matched nothing.")
        return 0

    for candidate in result.candidates:
        citations = (
            f"{candidate.citation_count} citations"
            if candidate.citation_count is not None
            else "citations unknown"
        )
        print(f"[{candidate.rank}] {candidate.citation_label_hint}  ({citations})")
        print(f"    {candidate.title}")
        if candidate.venue:
            print(f"    venue: {candidate.venue}")
        print(f"    authors: {', '.join(candidate.authors) or '(none listed)'}")
        print(f"    paper id: {candidate.paper_id}")
        if candidate.doi:
            print(f"    doi: {candidate.doi}")
        if candidate.arxiv_id:
            print(f"    arxiv: {candidate.arxiv_id}")
        if candidate.url:
            print(f"    url: {candidate.url}")
        if candidate.open_access_pdf_url:
            print(f"    open-access pdf (recorded, NOT downloaded): {candidate.open_access_pdf_url}")
        print(f"    depth: {candidate.evidence_depth.value}  (has text: {candidate.has_text})")
        if candidate.abstract:
            abstract = " ".join(candidate.abstract.split())
            print(f"    abstract: {abstract[:200]}{'...' if len(abstract) > 200 else ''}")
        else:
            print("    abstract: none returned -- no text for this paper yet")
        print()

    print("These are CANDIDATE ACADEMIC SOURCES, not evidence.")
    print("  Nothing has been linked to a claim, and no PDF has been downloaded.")
    print("  A 'metadata' depth means the provider returned no abstract, so there is")
    print("  no text for that paper at all -- it cannot support a claim until the fetch")
    print("  stage retrieves one.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
