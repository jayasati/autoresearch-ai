"""
The vector store: chunks in, semantically similar chunks out.

    TextChunk[] ──embed──▶ ChromaDB (persistent, local) ──query──▶ RetrievedChunk[]

ChromaDB runs embedded — a library writing SQLite and HNSW files under
`CHROMA_PERSIST_DIR` — so there is no server, no credential, and nothing to be
unreachable. The collection uses cosine distance over the embedder's normalised
vectors, and the reported `similarity` is `1 - distance`.

Three rules this module enforces rather than assumes:

- **One collection, one model.** The embedder's name is written into the collection's
  metadata at creation, and opening the collection with a different model is an error.
  Vectors from two models share no meaningful space; querying one with the other would
  return confident nonsense.
- **Chunks are embedded here, never by ChromaDB.** The collection's default embedding
  function is never used: every add and every query passes vectors computed by the
  configured embedder, so what is stored and what is asked are always comparable.
- **A result is rebuilt, then re-validated.** Each hit is reconstructed into a
  `RetrievedChunk`, whose validator re-checks the span arithmetic and the source
  fingerprint — the store's answer is not exempt from the traceability invariant.

What similarity means — and does not — is documented in `models.py` and README.md:
it ranks wording, it does not verify claims.
"""

from collections.abc import Iterable
from typing import Any

from app.core.config import get_settings
from app.services.processing.models import SourceMetadata, TextChunk
from app.services.vectorstore.embedder import Embedder, SentenceTransformerEmbedder
from app.services.vectorstore.models import IndexResult, RetrievedChunk

#: Chroma metadata values must be str/int/float/bool — no None, no nesting. The full
#: source therefore travels as one JSON string, which round-trips losslessly; the flat
#: fields beside it exist so the collection can be filtered without parsing JSON.
_SOURCE_JSON_KEY = "source_json"


class VectorStore:
    """Persistent semantic index over processed chunks. Synchronous and local."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        persist_dir: str | None = None,
        collection_name: str | None = None,
    ) -> None:
        settings = get_settings()
        self.embedder: Embedder = embedder or SentenceTransformerEmbedder()
        self.persist_dir = persist_dir or settings.CHROMA_PERSIST_DIR
        self.collection_name = collection_name or settings.CHROMA_COLLECTION

        # Deferred import: chromadb is a heavy dependency from requirements-ml.txt,
        # and importing this package must not require it until a store is built.
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self._client = chromadb.PersistentClient(
            path=self.persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={
                "hnsw:space": "cosine",
                "embedding_model": self.embedder.model_name,
            },
        )

        # get_or_create keeps an existing collection's metadata, so this reads what
        # the collection was actually created with, not what we just asked for.
        existing_model = (self._collection.metadata or {}).get("embedding_model")
        if existing_model and existing_model != self.embedder.model_name:
            raise ValueError(
                f"collection {self.collection_name!r} was built with "
                f"{existing_model!r}; querying it with {self.embedder.model_name!r} "
                "would compare vectors from unrelated spaces. Use the original model "
                "or a different collection."
            )

    def count(self) -> int:
        return self._collection.count()

    # --- Indexing ----------------------------------------------------------------

    def index_chunks(self, chunks: Iterable[TextChunk]) -> IndexResult:
        """Embed and store chunks. Idempotent: ids are content-derived, so the same
        chunk indexed twice — in one batch or across runs — is stored once, and the
        result reports exactly how the input split between new and already-known."""
        requested = 0
        unique: dict[str, TextChunk] = {}
        duplicates_in_batch = 0
        for chunk in chunks:
            requested += 1
            chunk_id = str(chunk.chunk_id)
            if chunk_id in unique:
                duplicates_in_batch += 1
            else:
                unique[chunk_id] = chunk

        if not unique:
            return IndexResult(
                requested=requested,
                newly_indexed=0,
                already_present=0,
                duplicates_in_batch=duplicates_in_batch,
                collection_count=self.count(),
                embedding_model=self.embedder.model_name,
            )

        ids = list(unique)
        already_present = len(self._collection.get(ids=ids, include=[])["ids"])

        texts = [chunk.text for chunk in unique.values()]
        embeddings = self.embedder.embed_documents(texts)
        self._collection.upsert(
            ids=ids,
            documents=texts,
            embeddings=embeddings,  # type: ignore[arg-type]
            metadatas=[self._chunk_metadata(chunk) for chunk in unique.values()],
        )

        return IndexResult(
            requested=requested,
            newly_indexed=len(ids) - already_present,
            already_present=already_present,
            duplicates_in_batch=duplicates_in_batch,
            collection_count=self.count(),
            embedding_model=self.embedder.model_name,
        )

    def _chunk_metadata(self, chunk: TextChunk) -> dict[str, Any]:
        """Everything needed to rebuild and to filter; None values omitted (Chroma
        rejects them), the full source kept losslessly as JSON."""
        metadata: dict[str, Any] = {
            "document_id": str(chunk.document_id),
            "position": chunk.position,
            "start_char": chunk.start_char,
            "end_char": chunk.end_char,
            "content_hash": chunk.content_hash,
            "source_fingerprint": chunk.source_fingerprint,
            "depth": chunk.depth.value,
            "source_type": chunk.source.source_type.value,
            "embedding_model": self.embedder.model_name,
            _SOURCE_JSON_KEY: chunk.source.model_dump_json(),
        }
        for key, value in (
            ("provider", chunk.source.provider),
            ("title", chunk.source.title),
            ("url", chunk.source.url),
            ("doi", chunk.source.doi),
            ("year", chunk.source.year),
        ):
            if value is not None:
                metadata[key] = value
        return metadata

    # --- Querying ----------------------------------------------------------------

    def search(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
        """The `top_k` chunks whose embeddings sit closest to the query's, best first.

        An empty collection returns an empty list — before any source has been
        indexed, "nothing similar exists" is the correct answer, not an error. Fewer
        than `top_k` results means the collection holds fewer chunks than that.
        """
        if not query or not query.strip():
            raise ValueError("query must contain text")
        k = top_k if top_k is not None else get_settings().TOP_K
        if k < 1:
            raise ValueError(f"top_k must be at least 1, got {k}")

        total = self.count()
        if total == 0:
            return []

        result = self._collection.query(
            query_embeddings=[self.embedder.embed_query(query)],  # type: ignore[arg-type]
            n_results=min(k, total),
            include=["documents", "metadatas", "distances"],
        )
        ids = result["ids"][0]
        documents = (result.get("documents") or [[]])[0] or []
        metadatas = (result.get("metadatas") or [[]])[0] or []
        distances = (result.get("distances") or [[]])[0] or []

        return [
            self._rebuild(
                chunk_id=chunk_id,
                text=text,
                metadata=dict(metadata),
                distance=float(distance),
                query=query,
                rank=rank,
            )
            for rank, (chunk_id, text, metadata, distance) in enumerate(
                zip(ids, documents, metadatas, distances, strict=True)
            )
        ]

    def _rebuild(
        self,
        *,
        chunk_id: str,
        text: str,
        metadata: dict[str, Any],
        distance: float,
        query: str,
        rank: int,
    ) -> RetrievedChunk:
        source = SourceMetadata.model_validate_json(metadata[_SOURCE_JSON_KEY])
        return RetrievedChunk(
            chunk_id=chunk_id,  # type: ignore[arg-type]
            document_id=metadata["document_id"],
            position=metadata["position"],
            text=text,
            start_char=metadata["start_char"],
            end_char=metadata["end_char"],
            content_hash=metadata["content_hash"],
            source_fingerprint=metadata["source_fingerprint"],
            source=source,
            depth=metadata["depth"],
            query=query,
            rank=rank,
            # Cosine distance over normalised vectors is 1 - cos; clamp only the
            # float noise at the boundaries, never a genuinely out-of-range value.
            similarity=min(1.0, max(-1.0, 1.0 - distance)),
            distance=distance,
            embedding_model=metadata["embedding_model"],
        )
