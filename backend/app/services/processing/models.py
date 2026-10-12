"""
What the processing stage takes in and gives back.

    RawDocument        source metadata + whatever text the provider gave us (maybe none)
        ↓ clean, strip boilerplate, chunk
    ProcessedDocument  the cleaned text, its metadata, and its chunks
        └── TextChunk  a span of that text, carrying a reference to its source

Three properties are enforced by validators here rather than promised in comments,
because every later stage depends on them:

- **A chunk's text is exactly `document.text[start:end]`.** Not approximately, not
  "after stripping" — exactly. That is what makes a citation point at real characters.
- **Every chunk names its source.** A chunk carries the source fingerprint and the
  full source metadata, so a chunk that reaches the vector store alone can still be
  traced back without a database join.
- **Depth describes the text actually held.** A document whose text is empty is
  `METADATA`, whatever the input claimed. An abstract that cleaned to nothing is not an
  abstract we can quote.

IDs are UUIDv5 — derived, not random — so processing the same input twice gives the
same ids, and those ids can be used directly as the `document` / `document_chunk`
primary keys.
"""

import hashlib
import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, model_validator

from app.core.config import get_settings
from app.core.constants import EvidenceDepth, SourceType
from app.schemas.base import ReadModel, WriteModel
from app.services.retrieval.models import AcademicCandidateSource, CandidateSource
from app.utils.urls import source_fingerprint

#: Namespace for every id this stage derives. Fixed forever: changing it would change
#: every document and chunk id, and break every reference stored against the old ones.
PROCESSING_NAMESPACE = uuid.UUID("6f1d4c2e-8b7a-5e3f-9c1d-2a4b6c8e0f13")

EmptyReason = Literal["no_text_provided", "empty_after_cleaning"]


def content_hash(text: str) -> str:
    """SHA-256 of the text. The same definition `DocumentCreate` and the chunk
    repository use, so a hash computed here matches the one stored."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_id_for(fingerprint: str, text_hash: str) -> uuid.UUID:
    """Stable id for one text of one source.

    Built from the source and the *content*, not from when it was fetched: re-fetching
    unchanged text gives the same id (matching the `uq_document_content_per_source`
    constraint), and changed text gives a new one, so a claim verified against the old
    version keeps pointing at what it was verified against.
    """
    return uuid.uuid5(PROCESSING_NAMESPACE, f"document:{fingerprint}:{text_hash}")


def chunk_id_for(document_id: uuid.UUID, start: int, end: int) -> uuid.UUID:
    """Stable id for one span of one document.

    The span, not the position, identifies a chunk: the same characters of the same
    document are the same chunk, whichever chunk size produced them. Two configurations
    that produce different spans produce different ids, which is correct — they are
    different passages.
    """
    return uuid.uuid5(PROCESSING_NAMESPACE, f"chunk:{document_id}:{start}:{end}")


class ChunkingConfig(WriteModel):
    """Chunk size and overlap, in characters.

    Defaults come from `CHUNK_SIZE` / `CHUNK_OVERLAP`. The overlap must be smaller
    than the size — the same rule `research_settings` enforces with
    `overlap_smaller_than_chunk`, so a configuration this accepts can be stored.
    """

    chunk_size: int = Field(ge=20, le=20_000)
    chunk_overlap: int = Field(ge=0)
    strip_boilerplate: bool = True

    @model_validator(mode="after")
    def overlap_must_be_smaller_than_size(self) -> "ChunkingConfig":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"chunk_overlap ({self.chunk_overlap}) must be smaller than "
                f"chunk_size ({self.chunk_size})"
            )
        return self

    @classmethod
    def from_settings(cls, **overrides: object) -> "ChunkingConfig":
        settings = get_settings()
        values: dict[str, object] = {
            "chunk_size": settings.CHUNK_SIZE,
            "chunk_overlap": settings.CHUNK_OVERLAP,
        }
        values.update(overrides)
        return cls.model_validate(values)


class SourceMetadata(ReadModel):
    """Everything known about where a text came from. Immutable.

    Carried unchanged from retrieval to every chunk. `fingerprint` is required — derived
    from DOI, URL or external id when not given — because a chunk whose source cannot
    be identified cannot be cited, and should not exist.
    """

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    source_type: Literal[SourceType.WEB, SourceType.ACADEMIC]
    fingerprint: str = Field(min_length=1)
    provider: str | None = None

    title: str | None = None
    url: str | None = None
    canonical_url: str | None = None
    doi: str | None = None
    external_id: str | None = Field(default=None, description="e.g. Semantic Scholar paper id")
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str | None = None
    published_date: str | None = None
    citation_count: int | None = None

    query: str | None = Field(default=None, description="The query that retrieved it.")
    rank: int | None = None
    relevance_score: float | None = Field(
        default=None,
        description=(
            "The search provider's RELEVANCE score, kept as retrieval provenance. "
            "Not a confidence in anything this source says."
        ),
    )
    retrieved_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def derive_fingerprint(cls, data: object) -> object:
        if isinstance(data, dict) and not data.get("fingerprint"):
            derived = source_fingerprint(
                doi=data.get("doi"),
                url=data.get("canonical_url") or data.get("url"),
                external_id=data.get("external_id"),
            )
            if derived is None:
                raise ValueError(
                    "a source needs a DOI, URL or external id; a chunk whose source "
                    "cannot be identified could never be cited"
                )
            data = {**data, "fingerprint": derived}
        return data

    @classmethod
    def from_web_candidate(cls, candidate: CandidateSource) -> "SourceMetadata":
        return cls(
            source_type=SourceType.WEB,
            fingerprint=candidate.fingerprint,
            provider=candidate.provider,
            title=candidate.title,
            url=candidate.url,
            canonical_url=candidate.canonical_url,
            published_date=candidate.published_date,
            query=candidate.query,
            rank=candidate.rank,
            relevance_score=candidate.relevance_score,
            retrieved_at=candidate.retrieved_at,
        )

    @classmethod
    def from_academic_candidate(cls, candidate: AcademicCandidateSource) -> "SourceMetadata":
        return cls(
            source_type=SourceType.ACADEMIC,
            fingerprint=candidate.fingerprint,
            provider=candidate.provider,
            title=candidate.title,
            url=candidate.url,
            doi=candidate.doi,
            external_id=candidate.paper_id,
            authors=tuple(candidate.authors),
            year=candidate.year,
            venue=candidate.venue,
            citation_count=candidate.citation_count,
            query=candidate.query,
            rank=candidate.rank,
            retrieved_at=candidate.retrieved_at,
        )

    def to_source_create(self):
        """The persistence schema for this source's `source` row."""
        from app.schemas.source import SourceCreate

        return SourceCreate(
            source_type=self.source_type,
            url=self.url,
            canonical_url=self.canonical_url,
            doi=self.doi,
            external_id=self.external_id,
            title=self.title,
            authors=list(self.authors) or None,
            venue=self.venue,
            publication_year=self.year,
            citation_count=self.citation_count,
        )


class RawDocument(WriteModel):
    """The input: a source, and the text a provider returned for it, if any.

    `depth` says what *kind* of text `text` is when present — a snippet, an abstract.
    It is not trusted as a description of the output: if the text turns out to be
    empty, the processed document is `METADATA` regardless.
    """

    # Text is not stripped on the way in: cleaning is the processor's job, and
    # `raw_char_length` should report what the provider actually sent.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    source: SourceMetadata
    text: str | None = None
    depth: EvidenceDepth
    extractor: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def metadata_depth_cannot_carry_text(self) -> "RawDocument":
        if self.depth is EvidenceDepth.METADATA and self.text and self.text.strip():
            raise ValueError(
                "depth is metadata but text was supplied; say what kind of text it is"
            )
        return self

    @classmethod
    def from_web_candidate(cls, candidate: CandidateSource) -> "RawDocument":
        """A web search result. Its text is the provider's snippet — nothing more."""
        return cls(
            source=SourceMetadata.from_web_candidate(candidate),
            text=candidate.snippet,
            depth=candidate.evidence_depth,
            extractor=f"{candidate.provider}_snippet",
        )

    @classmethod
    def from_academic_candidate(cls, candidate: AcademicCandidateSource) -> "RawDocument":
        """A paper. Its text is the abstract — and **only** the abstract.

        The title is not prepended to the text, and nothing is substituted when the
        abstract is missing. The title lives in the metadata; putting it in the text
        would let a claim be "supported" by a paper's title, which says what the paper
        is about, not what it found.
        """
        return cls(
            source=SourceMetadata.from_academic_candidate(candidate),
            text=candidate.abstract,
            depth=candidate.evidence_depth,
            extractor=f"{candidate.provider}_abstract" if candidate.abstract else None,
        )


class TextChunk(ReadModel):
    """A span of a processed document, with a reference to its source."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    position: int = Field(ge=0)

    text: str = Field(min_length=1)
    start_char: int = Field(ge=0, description="Offset into ProcessedDocument.text.")
    end_char: int = Field(gt=0, description="Exclusive.")
    content_hash: str

    source_fingerprint: str = Field(min_length=1)
    source: SourceMetadata
    depth: EvidenceDepth

    @model_validator(mode="after")
    def span_and_source_must_agree(self) -> "TextChunk":
        if self.end_char - self.start_char != len(self.text):
            raise ValueError(
                f"span [{self.start_char}:{self.end_char}] is "
                f"{self.end_char - self.start_char} characters but text is {len(self.text)}"
            )
        if self.source_fingerprint != self.source.fingerprint:
            raise ValueError("source_fingerprint does not match the source metadata")
        return self

    @property
    def span(self) -> tuple[int, int]:
        return self.start_char, self.end_char


class ProcessedDocument(ReadModel):
    """The output: one source's cleaned text, divided into chunks.

    An empty document is a valid result, not an error: a paper with no abstract is
    still a source worth recording. It has `text == ""`, depth `METADATA`, no chunks,
    and an `empty_reason` saying why — so "we had nothing to read" stays distinguishable
    from "we read it and it said nothing relevant".
    """

    document_id: uuid.UUID
    source: SourceMetadata
    text: str
    content_hash: str
    depth: EvidenceDepth
    extractor: str | None = None
    chunks: list[TextChunk] = Field(default_factory=list)

    chunking: ChunkingConfig
    raw_char_length: int = Field(ge=0, description="Length of the text as received.")
    removed_boilerplate: list[str] = Field(
        default_factory=list,
        description="Every fragment boilerplate removal took out, for auditing.",
    )
    empty_reason: EmptyReason | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def char_length(self) -> int:
        return len(self.text)

    @property
    def is_empty(self) -> bool:
        return not self.text

    @property
    def spans(self) -> list[tuple[int, int, str]]:
        """`(start, end, text)` triples, the shape `DocumentChunkRepository.add_chunks`
        takes."""
        return [(c.start_char, c.end_char, c.text) for c in self.chunks]

    @model_validator(mode="after")
    def chunks_must_address_this_text(self) -> "ProcessedDocument":
        """The traceability invariant, checked rather than assumed."""
        if self.content_hash != content_hash(self.text):
            raise ValueError("content_hash does not match the text")
        if self.is_empty:
            if self.chunks:
                raise ValueError("an empty document cannot have chunks")
            if self.depth is not EvidenceDepth.METADATA:
                raise ValueError("an empty document has no text, so its depth is metadata")
            if self.empty_reason is None:
                raise ValueError("an empty document must say why it is empty")
        elif self.empty_reason is not None:
            raise ValueError("a document with text cannot have an empty_reason")

        for expected_position, chunk in enumerate(self.chunks):
            if chunk.position != expected_position:
                raise ValueError(f"chunk positions must run 0..n-1; found {chunk.position}")
            if chunk.document_id != self.document_id:
                raise ValueError(f"chunk {chunk.position} belongs to another document")
            if chunk.source != self.source:
                raise ValueError(f"chunk {chunk.position} references a different source")
            if self.text[chunk.start_char : chunk.end_char] != chunk.text:
                raise ValueError(
                    f"chunk {chunk.position} text is not document.text"
                    f"[{chunk.start_char}:{chunk.end_char}]"
                )
        return self

    def to_document_create(self, source_id: uuid.UUID):
        """The persistence schema for the `document` row.

        Refuses an empty document: the `document` table requires text, and inventing
        some to satisfy it is exactly what this stage must not do. Record the source
        alone with `source.to_source_create()`.
        """
        from app.schemas.source import DocumentCreate

        if self.is_empty:
            raise ValueError(
                f"document has no text ({self.empty_reason}); record the source only"
            )
        return DocumentCreate(
            source_id=source_id,
            text=self.text,
            depth=self.depth,
            extractor=self.extractor,
        )
