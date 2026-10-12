"""
Document processing: provider text to a cleaned document and traceable chunks.

| Module | Contents |
|---|---|
| `processor.py` | `DocumentProcessor` — the pipeline |
| `cleaning.py` | `clean_text`, `strip_boilerplate` — remove and normalise, never add |
| `chunking.py` | `chunk_spans` — overlapping character spans with exact offsets |
| `models.py` | `RawDocument` in; `ProcessedDocument` and `TextChunk` out |

v1 handles the text the retrieval stage already returns: web snippets and paper
abstracts. No fetching, no PDF extraction, no embeddings. See README.md for the
chunking strategy and its limitations.
"""

from app.services.processing.chunking import chunk_spans
from app.services.processing.cleaning import clean_text, strip_boilerplate
from app.services.processing.models import (
    ChunkingConfig,
    ProcessedDocument,
    RawDocument,
    SourceMetadata,
    TextChunk,
    chunk_id_for,
    content_hash,
    document_id_for,
)
from app.services.processing.processor import DocumentProcessor

__all__ = [
    "ChunkingConfig",
    "DocumentProcessor",
    "ProcessedDocument",
    "RawDocument",
    "SourceMetadata",
    "TextChunk",
    "chunk_id_for",
    "chunk_spans",
    "clean_text",
    "content_hash",
    "document_id_for",
    "strip_boilerplate",
]
