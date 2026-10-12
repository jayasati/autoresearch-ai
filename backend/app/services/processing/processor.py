"""
The document processing pipeline: source metadata + text → cleaned document → chunks.

    RawDocument ──clean_text──▶ ──strip_boilerplate──▶ text ──chunk_spans──▶ TextChunk[]
                                                          │
                                                          └──▶ ProcessedDocument

Pure and synchronous: no network, no database, no model. The same input always gives
the same output, ids included — which is what lets a benchmark run be reproduced, and
what lets a re-processed source be recognised as the same document.

**It never produces text the source did not contain.** A paper with no abstract gives
an empty document with no chunks; nothing — not the title, not a placeholder — is put
in its place. See `models.py` for why the title in particular stays out of the text.
"""

from collections.abc import Iterable

from app.core.constants import EvidenceDepth
from app.services.processing.chunking import chunk_spans
from app.services.processing.cleaning import clean_text, strip_boilerplate
from app.services.processing.models import (
    ChunkingConfig,
    EmptyReason,
    ProcessedDocument,
    RawDocument,
    TextChunk,
    chunk_id_for,
    content_hash,
    document_id_for,
)
from app.services.retrieval.models import AcademicCandidateSource, CandidateSource

Candidate = CandidateSource | AcademicCandidateSource


class DocumentProcessor:
    """Turns raw source text into a `ProcessedDocument`. Holds only its configuration."""

    def __init__(self, config: ChunkingConfig | None = None) -> None:
        self.config = config or ChunkingConfig.from_settings()

    def process(self, raw: RawDocument) -> ProcessedDocument:
        text = clean_text(raw.text)
        removed: list[str] = []
        if text and self.config.strip_boilerplate:
            text, removed = strip_boilerplate(text)

        empty_reason: EmptyReason | None = None
        if not text:
            empty_reason = (
                "empty_after_cleaning" if raw.text and raw.text.strip() else "no_text_provided"
            )

        text_hash = content_hash(text)
        document_id = document_id_for(raw.source.fingerprint, text_hash)
        # The depth describes what we hold, not what we were promised.
        depth = raw.depth if text else EvidenceDepth.METADATA

        chunks = [
            TextChunk(
                chunk_id=chunk_id_for(document_id, start, end),
                document_id=document_id,
                position=position,
                text=text[start:end],
                start_char=start,
                end_char=end,
                content_hash=content_hash(text[start:end]),
                source_fingerprint=raw.source.fingerprint,
                source=raw.source,
                depth=depth,
            )
            for position, (start, end) in enumerate(
                chunk_spans(
                    text,
                    chunk_size=self.config.chunk_size,
                    chunk_overlap=self.config.chunk_overlap,
                )
            )
        ]

        return ProcessedDocument(
            document_id=document_id,
            source=raw.source,
            text=text,
            content_hash=text_hash,
            depth=depth,
            extractor=raw.extractor if text else None,
            chunks=chunks,
            chunking=self.config,
            raw_char_length=len(raw.text or ""),
            removed_boilerplate=removed,
            empty_reason=empty_reason,
        )

    def process_candidate(self, candidate: Candidate) -> ProcessedDocument:
        """Process a search result from either retrieval provider."""
        if isinstance(candidate, AcademicCandidateSource):
            return self.process(RawDocument.from_academic_candidate(candidate))
        return self.process(RawDocument.from_web_candidate(candidate))

    def process_many(self, items: Iterable[RawDocument | Candidate]) -> list[ProcessedDocument]:
        """Process several inputs, in order. Empty documents are returned, not dropped:
        the caller decides what a source with no text means for its run."""
        return [
            self.process(item) if isinstance(item, RawDocument) else self.process_candidate(item)
            for item in items
        ]
