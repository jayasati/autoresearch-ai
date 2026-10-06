"""
Where evidence comes from: Source, Document, DocumentChunk.

The three are separate on purpose, and the distinction is the whole basis of
traceability:

    Source    -- a thing in the world (a web page, a paper). Identity is its URL
                 or DOI, so it is shared across runs rather than duplicated.
    Document  -- one fetched, extracted *text* of that source, at one point in
                 time. A page can be re-fetched and say something different.
    Chunk     -- a span of that document, with its character offsets kept.

Collapsing these would make "this claim is supported by this passage" unprovable:
without the document you cannot say *which version* of the page was read, and
without offsets you cannot point at the sentence.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import EvidenceDepth, SourceType
from app.db.base import Base, Timestamped, UUIDPrimaryKey, enum_column

if TYPE_CHECKING:
    from app.models.evidence import Citation
    from app.models.research import ResearchRun, SubQuestion


class Source(UUIDPrimaryKey, Timestamped, Base):
    """A web page, a paper, or a model assertion with no external source at all.

    Deliberately **global, not per-run**: two runs that both cite the same paper
    must reference the same row, otherwise "source diversity" and "how often is
    this paper cited" cannot be computed, and the same paper could be judged
    fabricated in one run and valid in another.

    `fingerprint` is what makes that deduplication possible: a hash of the
    canonical URL, the DOI, or the external id, whichever identifies the source.
    """

    __tablename__ = "source"

    source_type: Mapped[SourceType] = mapped_column(enum_column(SourceType), nullable=False)

    # Identity. Which of these is present depends on source_type; the check
    # constraint below enforces that at least one exists for external sources.
    url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(200))  # e.g. S2 paper id

    fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

    # Bibliographic metadata
    title: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[list | None] = mapped_column(JSON)  # ordered list of names
    venue: Mapped[str | None] = mapped_column(Text)
    publication_year: Mapped[int | None] = mapped_column(Integer)
    citation_count: Mapped[int | None] = mapped_column(Integer)

    first_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    documents: Mapped[list["Document"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    research_sources: Mapped[list["ResearchSource"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    citations: Mapped[list["Citation"]] = relationship(back_populates="source")

    __table_args__ = (
        CheckConstraint(
            "source_type = 'model' OR url IS NOT NULL OR doi IS NOT NULL "
            "OR external_id IS NOT NULL",
            name="external_sources_are_identifiable",
        ),
        CheckConstraint(
            "publication_year IS NULL OR (publication_year > 1400 "
            "AND publication_year < 2200)",
            name="publication_year_plausible",
        ),
        CheckConstraint(
            "citation_count IS NULL OR citation_count >= 0",
            name="citation_count_non_negative",
        ),
        Index("ix_source_doi", "doi"),
    )

    def __repr__(self) -> str:
        label = self.title or self.url or self.doi or "unidentified"
        return f"<Source {self.source_type} {str(label)[:45]!r}>"


class ResearchSource(UUIDPrimaryKey, Base):
    """Association: this run retrieved that source, this way.

    A plain many-to-many table would lose everything that matters about the
    *retrieval*. The per-run facts live here rather than on `Source`, because they
    differ between runs: the query that found it, its rank, and -- critically --
    how deeply it was read, which is per-run because one run may fetch full text
    where another only saw an abstract.
    """

    __tablename__ = "research_source"

    research_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_run.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )
    subquestion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subquestion.id", ondelete="SET NULL")
    )

    retriever: Mapped[str | None] = mapped_column(String(60))  # "tavily", "semantic_scholar"
    retrieval_query: Mapped[str | None] = mapped_column(Text)
    rank: Mapped[int | None] = mapped_column(Integer)
    evidence_depth: Mapped[EvidenceDepth] = mapped_column(
        enum_column(EvidenceDepth), nullable=False, default=EvidenceDepth.SNIPPET
    )

    # The label used in the report ("S3"). Per-run, because the same source is
    # numbered differently in different reports.
    citation_label: Mapped[str | None] = mapped_column(String(20))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    research_run: Mapped["ResearchRun"] = relationship(back_populates="research_sources")
    source: Mapped[Source] = relationship(back_populates="research_sources")
    subquestion: Mapped["SubQuestion | None"] = relationship(back_populates="research_sources")

    __table_args__ = (
        # One row per source per run: a source retrieved twice is still one source.
        UniqueConstraint("research_run_id", "source_id", name="uq_research_source_source_per_run"),
        UniqueConstraint(
            "research_run_id", "citation_label", name="uq_research_source_label_per_run"
        ),
        CheckConstraint("rank IS NULL OR rank >= 0", name="rank_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<ResearchSource {self.citation_label or '?'} depth={self.evidence_depth}>"


class Document(UUIDPrimaryKey, Base):
    """The extracted text of a source, as read at one moment.

    Separate from `Source` because a web page is not a fixed object. If a page is
    re-fetched a month later and now says something different, that is a second
    document for the same source -- and a claim verified against the first one must
    keep pointing at the text that actually supported it. `content_hash` makes a
    re-fetch that changed nothing a no-op.
    """

    __tablename__ = "document"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    char_length: Mapped[int] = mapped_column(Integer, nullable=False)

    depth: Mapped[EvidenceDepth] = mapped_column(enum_column(EvidenceDepth), nullable=False)
    extractor: Mapped[str | None] = mapped_column(String(60))  # "trafilatura", "api_abstract"
    content_type: Mapped[str | None] = mapped_column(String(100))
    language: Mapped[str | None] = mapped_column(String(10))

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    source: Mapped[Source] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentChunk.position",
    )

    __table_args__ = (
        # Re-fetching identical content must not create a second document.
        UniqueConstraint("source_id", "content_hash", name="uq_document_content_per_source"),
        CheckConstraint("char_length > 0", name="char_length_positive"),
    )

    def __repr__(self) -> str:
        return f"<Document source={self.source_id} {self.char_length} chars depth={self.depth}>"


class DocumentChunk(UUIDPrimaryKey, Base):
    """A span of a document, with its position in the original text preserved.

    `start_char` and `end_char` are the reason this project can claim
    traceability at all. They are offsets into `Document.text`, so any claim can be
    followed back to the exact characters that support it -- not merely to the page
    it came from. A chunk without offsets would make "grounded" unfalsifiable.

    The embedding itself lives in ChromaDB, not here; `embedding_id` is the handle.
    Vectors in PostgreSQL would duplicate the vector store to no benefit.
    """

    __tablename__ = "document_chunk"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)

    embedding_model: Mapped[str | None] = mapped_column(String(200))
    embedding_id: Mapped[str | None] = mapped_column(String(100))  # ChromaDB id
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    document: Mapped[Document] = relationship(back_populates="chunks")
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="document_chunk", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("document_id", "position", name="uq_document_chunk_position_per_document"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint("start_char >= 0", name="start_char_non_negative"),
        # The offsets must describe a real, non-empty span.
        CheckConstraint("end_char > start_char", name="span_is_non_empty"),
        CheckConstraint("token_count IS NULL OR token_count > 0", name="token_count_positive"),
    )

    @property
    def span(self) -> tuple[int, int]:
        return self.start_char, self.end_char

    def __repr__(self) -> str:
        return f"<DocumentChunk {self.position} [{self.start_char}:{self.end_char}]>"


if TYPE_CHECKING:  # pragma: no cover
    from app.models.evidence import Evidence
