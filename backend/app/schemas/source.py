"""
Schemas for sources, documents and chunks.

The create models enforce the invariants that make a source identifiable and a
chunk traceable. Both matter before any external API exists: a source we cannot
identify cannot be deduplicated, and a chunk without a valid span cannot support a
claim.
"""

import hashlib
import uuid
from datetime import datetime

from pydantic import Field, model_validator

from app.core.constants import EvidenceDepth, SourceType
from app.schemas.base import IdentifiedModel, WriteModel


class SourceCreate(WriteModel):
    """A source as first recorded.

    `fingerprint` is derived, not supplied: identity must be computed the same way
    every time, or the same paper retrieved twice becomes two rows and every
    per-source metric is wrong.
    """

    source_type: SourceType
    url: str | None = Field(default=None, max_length=2000)
    canonical_url: str | None = Field(default=None, max_length=2000)
    doi: str | None = Field(default=None, max_length=200)
    external_id: str | None = Field(default=None, max_length=200)

    title: str | None = None
    authors: list[str] | None = None
    venue: str | None = None
    publication_year: int | None = Field(default=None, ge=1400, le=2200)
    citation_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def external_sources_must_be_identifiable(self) -> "SourceCreate":
        """A web page or paper must carry something that identifies it.

        `SourceType.MODEL` is the exception, and the only one: it represents the
        model asserting something with no external source at all. That case needs
        to be representable -- it is how an uncited claim gets recorded instead of
        being quietly dropped -- but it must be explicit, never the result of
        forgetting a URL.
        """
        if self.source_type is SourceType.MODEL:
            if any((self.url, self.doi, self.external_id)):
                raise ValueError(
                    "a model-asserted source has no external identifier; "
                    "use SourceType.WEB or ACADEMIC if it does"
                )
            return self

        # The web-specific rule is checked first so the error names the actual
        # problem: a web page is identified by its URL, and no DOI substitutes.
        if self.source_type is SourceType.WEB and not self.url:
            raise ValueError("web sources require a url")

        if not any((self.url, self.doi, self.external_id)):
            raise ValueError(
                f"{self.source_type.value} sources require at least one of "
                "url, doi or external_id"
            )
        return self

    @property
    def fingerprint(self) -> str | None:
        """Identity hash: DOI first, then canonical URL, then external id.

        DOI wins because it is the most stable identifier a paper has -- the same
        paper reachable at three URLs is still one source.
        """
        basis = self.doi or self.canonical_url or self.url or self.external_id
        if basis is None:
            return None
        normalized = basis.strip().lower().rstrip("/")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class SourceRead(IdentifiedModel):
    source_type: SourceType
    url: str | None
    canonical_url: str | None
    doi: str | None
    external_id: str | None
    fingerprint: str | None
    title: str | None
    authors: list[str] | None
    venue: str | None
    publication_year: int | None
    citation_count: int | None
    first_retrieved_at: datetime | None


class ResearchSourceRead(IdentifiedModel):
    """How one run used one source."""

    source_id: uuid.UUID
    subquestion_id: uuid.UUID | None
    retriever: str | None
    retrieval_query: str | None
    rank: int | None
    evidence_depth: EvidenceDepth
    citation_label: str | None
    source: SourceRead | None = None


class DocumentCreate(WriteModel):
    """One extraction of a source's text."""

    source_id: uuid.UUID
    text: str = Field(min_length=1)
    depth: EvidenceDepth
    extractor: str | None = Field(default=None, max_length=60)
    content_type: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=10)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def char_length(self) -> int:
        return len(self.text)


class DocumentRead(IdentifiedModel):
    source_id: uuid.UUID
    content_hash: str
    char_length: int
    depth: EvidenceDepth
    extractor: str | None
    language: str | None
    fetched_at: datetime
    # The full text is omitted by default: documents run to tens of thousands of
    # characters, and no view that lists documents needs all of them.


class DocumentChunkCreate(WriteModel):
    """A span of a document.

    The offsets are validated here rather than trusted, because they are the
    foundation of every traceability claim this project makes. A chunk whose span
    does not match its text points at the wrong words, and the UI would highlight
    a passage that does not say what the claim says it says.
    """

    document_id: uuid.UUID
    position: int = Field(ge=0)
    text: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    token_count: int | None = Field(default=None, gt=0)
    embedding_model: str | None = Field(default=None, max_length=200)
    embedding_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def span_must_describe_the_text(self) -> "DocumentChunkCreate":
        if self.end_char <= self.start_char:
            raise ValueError("end_char must be greater than start_char")
        span_length = self.end_char - self.start_char
        if span_length != len(self.text):
            raise ValueError(
                f"span is {span_length} characters but text is {len(self.text)}; "
                "the offsets must address exactly this text in the source document"
            )
        return self

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class DocumentChunkRead(IdentifiedModel):
    document_id: uuid.UUID
    position: int
    text: str
    content_hash: str
    start_char: int
    end_char: int
    token_count: int | None
    embedding_model: str | None
    embedding_id: str | None
