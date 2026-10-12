"""
What the vector store takes in and gives back.

In: the `TextChunk`s the processing stage produced. Out: `RetrievedChunk`s — the same
chunks, found by meaning, each carrying its similarity to the query and the full
source metadata it was indexed with.

**Similarity is NOT proof that a claim is true.** It measures how close a chunk's
wording is to the query's wording in embedding space — nothing more. "Coffee improves
memory" and "coffee does not improve memory" are nearly identical sentences and will
score nearly identically against a query about coffee and memory. High similarity
means "this passage is about the thing you asked about", and that is all the
verification stage may read into it: similarity selects which passages are worth
reading, and the truth of a claim is judged by reading them, never by this number.
`relevance_score` on `SourceMetadata` carries the same warning for the same reason.
"""

import uuid

from pydantic import Field, model_validator

from app.core.constants import EvidenceDepth
from app.schemas.base import ReadModel
from app.services.processing.models import SourceMetadata


class IndexResult(ReadModel):
    """What one `index_chunks` call did, exactly.

    Chunk ids are content-derived (UUIDv5 of document, start, end), so "duplicate"
    is well defined: the same id is the same passage of the same text. Re-indexing
    it is a no-op by upsert, and this result says how often that happened rather
    than hiding it — a run that indexed nothing new should know it indexed nothing new.
    """

    requested: int = Field(ge=0, description="Chunks passed in, duplicates included.")
    newly_indexed: int = Field(ge=0, description="Chunks the collection had never seen.")
    already_present: int = Field(
        ge=0, description="Chunks already in the collection; re-upserted, unchanged."
    )
    duplicates_in_batch: int = Field(
        ge=0, description="Repeats within this call's own input, indexed once."
    )
    collection_count: int = Field(ge=0, description="Collection size after the call.")
    embedding_model: str

    @model_validator(mode="after")
    def counts_must_add_up(self) -> "IndexResult":
        if self.newly_indexed + self.already_present + self.duplicates_in_batch != self.requested:
            raise ValueError("newly_indexed + already_present + duplicates_in_batch != requested")
        return self


class RetrievedChunk(ReadModel):
    """A chunk that matched a query, with everything needed to cite it.

    Carries the same identity and source fields as the `TextChunk` it was indexed
    from, so a retrieved chunk can be traced to its document row, its exact span of
    `document.text`, and its source — without a database join and without trusting
    the vector store's text blindly (`content_hash` lets a caller re-verify it).
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    position: int = Field(ge=0)

    text: str = Field(min_length=1)
    start_char: int = Field(ge=0, description="Offset into the document's cleaned text.")
    end_char: int = Field(gt=0, description="Exclusive.")
    content_hash: str

    source_fingerprint: str = Field(min_length=1)
    source: SourceMetadata
    depth: EvidenceDepth

    query: str = Field(description="The query this chunk was retrieved for.")
    rank: int = Field(ge=0, description="Position among this query's results, 0 = closest.")
    similarity: float = Field(
        ge=-1.0,
        le=1.0,
        description=(
            "Cosine similarity between the query and this chunk's embedding. "
            "A measure of closeness of WORDING, never of truth: a sentence and its "
            "negation score almost identically. Use it to decide what to read, "
            "not what to believe."
        ),
    )
    distance: float = Field(
        ge=0.0, description="Cosine distance as the store reported it; 1 - similarity."
    )
    embedding_model: str = Field(description="The model that produced both embeddings.")

    @model_validator(mode="after")
    def must_stay_traceable(self) -> "RetrievedChunk":
        """The same checks `TextChunk` enforces, re-applied to what the store returned."""
        if self.end_char - self.start_char != len(self.text):
            raise ValueError(
                f"span [{self.start_char}:{self.end_char}] is "
                f"{self.end_char - self.start_char} characters but text is {len(self.text)}"
            )
        if self.source_fingerprint != self.source.fingerprint:
            raise ValueError("source_fingerprint does not match the source metadata")
        return self
