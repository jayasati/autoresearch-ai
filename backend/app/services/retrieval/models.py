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


# --------------------------------------------------------------------------- #
# Academic search
# --------------------------------------------------------------------------- #


class AcademicSearchQuery(WriteModel):
    """One paper search to run."""

    query: str = Field(min_length=1, max_length=400)
    limit: int = Field(default=10, ge=1, le=100)
    year_from: int | None = Field(default=None, ge=1400, le=2200)
    year_to: int | None = Field(default=None, ge=1400, le=2200)
    min_citation_count: int | None = Field(default=None, ge=0)

    @field_validator("query")
    @classmethod
    def query_must_be_substantive(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError("query must not be blank")
        return collapsed

    @model_validator(mode="after")
    def year_range_must_make_sense(self) -> "AcademicSearchQuery":
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError(f"year_from ({self.year_from}) is after year_to ({self.year_to})")
        return self

    @property
    def year_filter(self) -> str | None:
        """Semantic Scholar's `year` parameter: 2020, 2020-, -2020, or 2016-2020."""
        if self.year_from is None and self.year_to is None:
            return None
        if self.year_from is not None and self.year_to is not None:
            if self.year_from == self.year_to:
                return str(self.year_from)
            return f"{self.year_from}-{self.year_to}"
        if self.year_from is not None:
            return f"{self.year_from}-"
        return f"-{self.year_to}"


class AcademicCandidateSource(ReadModel):
    """A paper a search returned. **A candidate, not evidence.**

    Same rule as `CandidateSource`, and for the same reasons: nothing has been read
    beyond what the provider volunteered, nothing is linked to a claim, and this
    carries no `relation`, no verdict and no "supports" field.

    One difference matters. A web result always comes with a snippet; a paper does
    **not** always come with an abstract. When Semantic Scholar returns no abstract we
    have the title and the bibliography and no text at all, so `evidence_depth` is
    `METADATA` rather than `ABSTRACT`. Calling it `ABSTRACT` would be a small lie that
    the groundedness metrics would faithfully repeat later.

    Such papers are still returned, not dropped: discarding every paper whose abstract
    the provider failed to index would bias retrieval toward whatever happens to be
    well indexed, which is a worse distortion than carrying a source we cannot yet
    quote. The fetch stage is what turns it into text.
    """

    paper_id: str = Field(description="Semantic Scholar's own identifier for the paper.")

    title: str
    authors: list[str] = Field(
        default_factory=list, description="Author names in the order given."
    )
    year: int | None = Field(default=None, ge=1400, le=2200)

    abstract: str | None = Field(
        default=None,
        description=(
            "The abstract as published, when the provider has one. Not quotable as "
            "support for a claim yet -- it has not been linked to one. Often absent."
        ),
    )

    url: str | None = Field(default=None, description="Landing page for the paper.")
    doi: str | None = Field(default=None, description="Normalised: lower-cased, bare.")
    arxiv_id: str | None = None
    external_ids: dict[str, str] = Field(
        default_factory=dict, description="Every identifier the provider returned."
    )

    venue: str | None = None
    citation_count: int | None = Field(default=None, ge=0)
    influential_citation_count: int | None = Field(default=None, ge=0)
    publication_types: list[str] = Field(default_factory=list)

    open_access_pdf_url: str | None = Field(
        default=None,
        description=(
            "Where an open-access PDF would be, when the provider knows of one. "
            "**Recorded, never downloaded** -- fetching belongs to a later stage. "
            "Keeping the address costs nothing and saves that stage a lookup."
        ),
    )

    fingerprint: str = Field(
        description="Identity hash: DOI when there is one, else the URL, else the paper id."
    )
    rank: int = Field(ge=0, description="Position in the provider's ranking, 0-based.")

    source_type: Literal[SourceType.ACADEMIC] = Field(
        default=SourceType.ACADEMIC,
        description="Always academic. This service only searches papers.",
    )

    evidence_depth: EvidenceDepth = Field(
        description=(
            "ABSTRACT when the provider returned one, METADATA when it did not. "
            "Never FULL_TEXT: nothing has been fetched."
        ),
    )

    retrieved_at: datetime = Field(default_factory=utcnow)
    provider: str = Field(default="semantic_scholar")
    query: str = Field(description="The query that produced this candidate.")

    @field_validator("evidence_depth")
    @classmethod
    def depth_cannot_claim_full_text(cls, value: EvidenceDepth) -> EvidenceDepth:
        """Nothing has been fetched, so full text is not representable here.

        A plain `Literal` cannot express "one of two", so this is a validator -- but it
        is the same rule `CandidateSource` enforces with a `Literal`.
        """
        if value not in (EvidenceDepth.METADATA, EvidenceDepth.ABSTRACT):
            raise ValueError(
                "an academic search result has at most an abstract; "
                f"{value.value!r} would claim text that has not been fetched"
            )
        return value

    @model_validator(mode="after")
    def depth_must_match_whether_there_is_an_abstract(self) -> "AcademicCandidateSource":
        """The depth has to describe what is actually here.

        This is the check that keeps the groundedness metrics honest at the source. If
        depth could say ABSTRACT while `abstract` is None, every later measurement built
        on depth would inherit the error -- and it would read as a property of the
        sources rather than a bug.
        """
        if self.abstract and self.evidence_depth is not EvidenceDepth.ABSTRACT:
            raise ValueError("an abstract is present, so evidence_depth must be abstract")
        if not self.abstract and self.evidence_depth is not EvidenceDepth.METADATA:
            raise ValueError("there is no abstract, so evidence_depth must be metadata")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_text(self) -> bool:
        """Whether there is anything to quote. False for a metadata-only paper."""
        return self.evidence_depth.has_text

    @property
    def citation_label_hint(self) -> str:
        """A human-readable label for a source list. Not a citation.

        Produces forms like "Smith et al. (2021)". It exists so a person reading the
        retrieved sources can recognise them; nothing in the pipeline cites by it,
        because a citation has to resolve to an identifier.
        """
        if not self.authors:
            stem = "Unknown"
        elif len(self.authors) == 1:
            parts = self.authors[0].split()
            stem = parts[-1] if parts else self.authors[0]
        else:
            parts = self.authors[0].split()
            stem = f"{parts[-1] if parts else self.authors[0]} et al."
        return f"{stem} ({self.year})" if self.year else stem

    def to_source_create(self):
        """Convert to the persistence schema for a `source` row.

        Returns a **`SourceCreate`** and nothing else, for the same reason as the web
        candidate: evidence needs a fetched chunk and a citation needs a claim, and a
        search result justifies neither.
        """
        from app.schemas.source import SourceCreate

        return SourceCreate(
            source_type=SourceType.ACADEMIC,
            url=self.url,
            canonical_url=canonicalize_url(self.url) if self.url else None,
            doi=self.doi,
            external_id=self.paper_id,
            title=self.title,
            authors=self.authors or None,
            venue=self.venue,
            publication_year=self.year,
            citation_count=self.citation_count,
        )


class AcademicSearchResult(ReadModel):
    """The outcome of one paper search.

    `metadata_only_count` is reported separately from the total because "eight papers
    found" and "eight papers found, five of which we have no text for" support very
    different conclusions, and only the second one is true.
    """

    query: str
    provider: str = "semantic_scholar"
    candidates: list[AcademicCandidateSource] = Field(default_factory=list)

    requested_limit: int = Field(ge=0)
    total_available: int | None = Field(
        default=None,
        ge=0,
        description="How many the provider says match, usually far more than were returned.",
    )
    raw_result_count: int = Field(default=0, ge=0, description="Before deduplication.")
    duplicates_removed: int = Field(default=0, ge=0)
    malformed_results: int = Field(default=0, ge=0)

    elapsed_ms: float = Field(default=0.0, ge=0.0)
    attempts: int = Field(default=1, ge=1)
    throttled_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Time spent waiting on the client-side rate limit, not on the network.",
    )
    retrieved_at: datetime = Field(default_factory=utcnow)

    @property
    def is_empty(self) -> bool:
        """No candidates. A legitimate answer, not a failure."""
        return not self.candidates

    @property
    def with_abstracts(self) -> list[AcademicCandidateSource]:
        return [c for c in self.candidates if c.evidence_depth is EvidenceDepth.ABSTRACT]

    @property
    def metadata_only(self) -> list[AcademicCandidateSource]:
        """Papers we found but have no text for. They cannot support a claim yet."""
        return [c for c in self.candidates if c.evidence_depth is EvidenceDepth.METADATA]

    @property
    def metadata_only_count(self) -> int:
        return len(self.metadata_only)

    @property
    def with_doi(self) -> list[AcademicCandidateSource]:
        """Papers carrying a DOI -- the identifier a citation can be validated against."""
        return [c for c in self.candidates if c.doi]
