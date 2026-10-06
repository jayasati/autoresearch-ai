"""
What a web search returns: **candidate sources**, not evidence.

That distinction is the whole point of this module, so it is worth being explicit
about what it means.

A search result is a page that *might* be relevant, ranked by a provider's relevance
model. Nothing about it has been read, checked, or linked to a claim. It is not
evidence, and this stage must not let anything downstream mistake it for evidence:

- the type is `CandidateSource`, not `Source` and certainly not `Evidence`;
- it carries **no** `relation`, no verdict, no "supports" field — the vocabulary for
  those (`EvidenceRelation`, `VerificationVerdict`) is not imported here at all;
- `relevance_score` is the provider's **relevance** score. It says the page looks
  topical. It says nothing about whether the page is correct, credible, or supports
  any particular claim, and it must never be read as a confidence in a claim;
- `evidence_depth` is `SNIPPET`, because that is literally all we have — a short
  extract the provider chose. Full text arrives in the fetch stage, and recording
  the honest depth now is what stops the groundedness metrics overstating later.

Evidence is created in the evidence layer, by linking a *claim* to a *chunk* of a
fetched document. A `CandidateSource` is two stages away from that.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, computed_field, field_validator, model_validator

from app.core.constants import EvidenceDepth, SourceType
from app.db.base import utcnow
from app.schemas.base import ReadModel, WriteModel
from app.utils.urls import canonicalize_url, source_fingerprint

SearchDepth = Literal["basic", "advanced"]


class WebSearchQuery(WriteModel):
    """One search to run."""

    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=8, ge=1, le=50)
    depth: SearchDepth = "basic"
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)

    @field_validator("query")
    @classmethod
    def query_must_be_substantive(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError("query must not be blank")
        return collapsed

    @model_validator(mode="after")
    def domain_filters_must_not_contradict(self) -> "WebSearchQuery":
        overlap = {d.lower() for d in self.include_domains} & {
            d.lower() for d in self.exclude_domains
        }
        if overlap:
            raise ValueError(
                f"domains appear in both include and exclude: {sorted(overlap)}"
            )
        return self


class CandidateSource(ReadModel):
    """A page a search returned. **A candidate, not evidence.**

    See the module docstring. Nothing here asserts that this page supports anything.
    """

    url: str = Field(description="The URL exactly as the provider returned it.")
    canonical_url: str = Field(
        description="Normalised form used for comparison. See app/utils/urls.py."
    )
    fingerprint: str = Field(description="Identity hash of the canonical URL.")

    title: str | None = None

    snippet: str | None = Field(
        default=None,
        description=(
            "The extract the provider chose. Not the page, and not quotable as "
            "support for a claim -- it is an unverified excerpt selected by a "
            "relevance model."
        ),
    )

    relevance_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "The provider's RELEVANCE score: how topical the page looks. Says nothing "
            "about correctness, credibility, or support for any claim. Never use it as "
            "a confidence in a claim."
        ),
    )

    published_date: str | None = Field(
        default=None,
        description="As reported by the provider, unparsed. Often absent or wrong.",
    )

    rank: int = Field(ge=0, description="Position in the provider's ranking, 0-based.")

    source_type: Literal[SourceType.WEB] = Field(
        default=SourceType.WEB,
        description="Always web. This service only searches the web.",
    )

    evidence_depth: Literal[EvidenceDepth.SNIPPET] = Field(
        default=EvidenceDepth.SNIPPET,
        description=(
            "Always snippet. A search result is an extract; nothing has been fetched. "
            "Recorded honestly now so the groundedness metrics cannot overstate later."
        ),
    )

    retrieved_at: datetime = Field(
        default_factory=utcnow,
        description="When this candidate was retrieved, timezone-aware UTC.",
    )

    provider: str = Field(default="tavily")
    query: str = Field(description="The query that produced this candidate.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def domain(self) -> str:
        """Host of the canonical URL. The unit source diversity is counted in."""
        remainder = self.canonical_url.split("://", 1)[-1]
        return remainder.split("/", 1)[0].split(":", 1)[0]

    @classmethod
    def from_provider_result(
        cls, result: dict[str, Any], *, rank: int, query: str, provider: str = "tavily"
    ) -> "CandidateSource | None":
        """Build a candidate from one provider result.

        Returns `None` for a result with no usable URL rather than raising. One
        malformed entry in a page of ten should cost that entry, not the whole
        search — and the caller counts what it dropped, so the loss is visible
        instead of silent.
        """
        url = (result.get("url") or "").strip()
        if not url:
            return None

        canonical = canonicalize_url(url)
        fingerprint = source_fingerprint(url=url)
        if not canonical or not fingerprint:
            return None

        # Providers have been known to return scores slightly outside [0, 1]. Clamp
        # rather than reject: the ordering is what matters, and losing a result over a
        # rounding artefact would be worse than a hundredth of imprecision.
        raw_score = result.get("score")
        score = (
            min(max(float(raw_score), 0.0), 1.0)
            if isinstance(raw_score, int | float)
            else None
        )

        snippet = result.get("content")
        return cls(
            url=url,
            canonical_url=canonical,
            fingerprint=fingerprint,
            title=(result.get("title") or None),
            snippet=snippet.strip() if isinstance(snippet, str) and snippet.strip() else None,
            relevance_score=score,
            published_date=(result.get("published_date") or None),
            rank=rank,
            query=query,
            provider=provider,
        )

    def to_source_create(self):
        """Convert to the persistence schema for a `source` row.

        Deliberately returns a **`SourceCreate`** and nothing else. It does not build
        an `Evidence` row, a `Citation`, or a `ResearchSource` link, because none of
        those can be justified by a search result: evidence needs a fetched chunk,
        and a citation needs a claim. This is the only bridge this module offers to
        the database, and it carries no claim of support.
        """
        from app.schemas.source import SourceCreate

        return SourceCreate(
            source_type=SourceType.WEB,
            url=self.url,
            canonical_url=self.canonical_url,
            title=self.title,
        )


class WebSearchResult(ReadModel):
    """The outcome of one search: its candidates and what happened on the way.

    The bookkeeping fields are not decoration. `duplicates_removed` and
    `malformed_results` are the difference between "this topic has three sources" and
    "this topic had eight results, five of which were the same page" — and the first
    reading would quietly distort source diversity.
    """

    query: str
    provider: str = "tavily"
    depth: SearchDepth = "basic"
    candidates: list[CandidateSource] = Field(default_factory=list)

    requested_results: int = Field(ge=0)
    raw_result_count: int = Field(default=0, ge=0, description="Before deduplication.")
    duplicates_removed: int = Field(default=0, ge=0)
    malformed_results: int = Field(
        default=0, ge=0, description="Entries dropped for having no usable URL."
    )

    elapsed_ms: float = Field(default=0.0, ge=0.0)
    attempts: int = Field(default=1, ge=1, description="Including the first try.")
    retrieved_at: datetime = Field(default_factory=utcnow)

    @property
    def is_empty(self) -> bool:
        """No candidates.

        An empty result is a legitimate answer, not a failure: some queries genuinely
        match nothing. It is reported as success with zero candidates so that a run
        can record "searched, found nothing" — which is a different and more honest
        finding than an error.
        """
        return not self.candidates

    @property
    def domains(self) -> set[str]:
        return {candidate.domain for candidate in self.candidates}

    @property
    def unique_domain_count(self) -> int:
        return len(self.domains)
