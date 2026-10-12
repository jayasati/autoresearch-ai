"""
Vector retrieval: processed chunks to a persistent semantic index, and back by meaning.

| Module | Contents |
|---|---|
| `embedder.py` | `Embedder` protocol; `SentenceTransformerEmbedder` — local, lazy-loaded |
| `store.py` | `VectorStore` — persistent ChromaDB collection, index and search |
| `models.py` | `IndexResult`, `RetrievedChunk` |

Importing this package stays cheap: torch and chromadb are imported only when a model
is first used or a store is first built, so the FastAPI app can import the services
layer without paying for the ML stack.

**Similarity is not proof that a claim is true.** It ranks passages by closeness of
wording to the query — a claim and its negation score almost identically. See
`models.py` and README.md before reading anything more into the number.
"""

from app.services.vectorstore.embedder import Embedder, SentenceTransformerEmbedder
from app.services.vectorstore.models import IndexResult, RetrievedChunk
from app.services.vectorstore.store import VectorStore

__all__ = [
    "Embedder",
    "IndexResult",
    "RetrievedChunk",
    "SentenceTransformerEmbedder",
    "VectorStore",
]
