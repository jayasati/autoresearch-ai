"""
Manual check: run one real Tavily search and print the candidate sources.

    backend/.venv/Scripts/python.exe scripts/search_web.py "does retrieval reduce hallucination"

Options:
    --max-results N        how many results to ask for
    --depth basic|advanced search depth (advanced costs more Tavily credits)
    --include DOMAIN       restrict to a domain (repeatable)
    --exclude DOMAIN       exclude a domain (repeatable)
    --json                 print the raw WebSearchResult as JSON

Needs a real `TAVILY_API_KEY` in `.env`. **This call costs Tavily credits**, which is
why it is a script you run deliberately and not part of the test suite — the suite is
fully mocked and makes no network requests.

What it prints are **candidate sources, not evidence**: pages a relevance model thinks
are topical. Nothing has been fetched, read, or checked against any claim.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.config import get_settings  # noqa: E402
from app.services.retrieval import (  # noqa: E402
    RetrievalError,
    SearchNotConfigured,
    TavilySearchService,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one Tavily search and print the candidate sources.",
    )
    parser.add_argument("query", help="What to search for.")
    parser.add_argument("--max-results", type=int, default=None)
    parser.add_argument("--depth", choices=("basic", "advanced"), default=None)
    parser.add_argument("--include", action="append", default=[], metavar="DOMAIN")
    parser.add_argument("--exclude", action="append", default=[], metavar="DOMAIN")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead.")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()

    async with TavilySearchService(settings=settings) as service:
        if not service.is_configured:
            print("\n  TAVILY_API_KEY is not set (or still holds a placeholder).")
            print("    - Get a key at https://app.tavily.com (free tier available).")
            print("    - Put it in .env:  TAVILY_API_KEY=tvly-...")
            return 1

        print(f"Searching: {args.query!r}")
        print(f"  depth={args.depth or settings.TAVILY_SEARCH_DEPTH} "
              f"max_results={args.max_results or settings.TAVILY_MAX_RESULTS} "
              f"timeout={settings.TAVILY_TIMEOUT_SECONDS:g}s "
              f"attempts<={settings.TAVILY_MAX_ATTEMPTS}")
        print()

        try:
            result = await service.search(
                args.query,
                max_results=args.max_results,
                depth=args.depth,
                include_domains=args.include,
                exclude_domains=args.exclude,
            )
        except SearchNotConfigured as exc:
            print(f"  NOT CONFIGURED: {exc.message}")
            return 1
        except RetrievalError as exc:
            print(f"  FAILED [{exc.code}] {exc.message}")
            if exc.details:
                for key, value in exc.details.items():
                    print(f"    {key}: {value}")
            print(f"    retryable: {exc.retryable}")
            return 1

    if args.json:
        print(result.model_dump_json(indent=2))
        return 0

    print(
        f"{len(result.candidates)} candidate source(s) "
        f"from {result.raw_result_count} result(s) "
        f"across {result.unique_domain_count} domain(s) "
        f"in {result.elapsed_ms:.0f}ms, attempt(s)={result.attempts}"
    )
    if result.duplicates_removed:
        print(f"  {result.duplicates_removed} duplicate(s) removed after URL normalisation")
    if result.malformed_results:
        print(f"  {result.malformed_results} result(s) dropped for having no usable URL")
    print()

    if result.is_empty:
        # Not an error. "Searched and found nothing" is a real finding, and a
        # different one from "the search broke".
        print("  No candidates. The search succeeded and matched nothing.")
        return 0

    for candidate in result.candidates:
        score = (
            f"{candidate.relevance_score:.3f}"
            if candidate.relevance_score is not None
            else "  -  "
        )
        print(f"[{candidate.rank}] relevance={score}  {candidate.domain}")
        print(f"    {candidate.title or '(no title)'}")
        print(f"    {candidate.url}")
        if candidate.canonical_url != candidate.url:
            print(f"    canonical: {candidate.canonical_url}")
        print(f"    fingerprint: {candidate.fingerprint[:16]}...  depth={candidate.evidence_depth}")
        if candidate.snippet:
            snippet = " ".join(candidate.snippet.split())
            print(f"    snippet: {snippet[:160]}{'...' if len(snippet) > 160 else ''}")
        print()

    print("These are CANDIDATE SOURCES, not evidence.")
    print("  'relevance' is how topical the page looks to Tavily's ranking model.")
    print("  It is not a judgement about correctness, credibility, or support for any")
    print("  claim. Nothing here has been fetched, read, or linked to a claim --")
    print("  that happens in the fetch and evidence stages.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
