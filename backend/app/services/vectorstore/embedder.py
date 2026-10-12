"""
Text to vectors, locally.

`SentenceTransformerEmbedder` wraps a Sentence Transformers model named by
`EMBEDDING_MODEL`. The model is loaded on first use, not at import or construction:
importing this module must stay cheap (torch takes seconds to import and the FastAPI
app imports the services package at startup), and constructing the embedder happens
in tests that never embed anything.

Embeddings are L2-normalised, so cosine similarity and dot product agree and the
distance the vector store reports is `1 - cosine_similarity`.

`Embedder` is the protocol the vector store depends on. Tests supply a deterministic
fake; nothing in the store's behaviour requires a neural model.
"""

from collections.abc import Sequence
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from app.core.config import get_settings


@runtime_checkable
class Embedder(Protocol):
    """What the vector store needs: a name, and texts to vectors.

    The name is stored with the collection and with every chunk, because vectors
    from two different models share a space in no meaningful sense — a collection
    must be written and queried by the same model.
    """

    @property
    def model_name(self) -> str: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class SentenceTransformerEmbedder:
    """Local embeddings via Sentence Transformers. No API, no key, no network
    after the first model download (cached under ~/.cache/huggingface)."""

    def __init__(self, model_name: str | None = None, batch_size: int | None = None) -> None:
        settings = get_settings()
        self._model_name = model_name or settings.EMBEDDING_MODEL
        self.batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE
        self._model: Any = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self) -> Any:
        if self._model is None:
            # Deferred: pulls torch, takes seconds, and downloads the model on
            # the very first run. Nothing that merely imports or constructs this
            # class should pay that.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._load().encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


@lru_cache
def get_default_embedder() -> SentenceTransformerEmbedder:
    """One default embedder per process.

    The model takes seconds to load and holds no per-run state; every pipeline
    stage that needs the configured embedder shares this instance rather than
    paying the load again.
    """
    return SentenceTransformerEmbedder()
