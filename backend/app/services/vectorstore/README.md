# vectorstore/ — Embeddings and semantic search

Processed chunks in, a persistent semantic index out, and back again by meaning:

```
TextChunk[] ──SentenceTransformerEmbedder──▶ vectors ──VectorStore──▶ ChromaDB (on disk)
query text  ──the same embedder───────────▶ vector  ──VectorStore──▶ RetrievedChunk[]
```

```python
from app.services.vectorstore import VectorStore

store = VectorStore()                       # EMBEDDING_MODEL, CHROMA_PERSIST_DIR, CHROMA_COLLECTION
store.index_chunks(document.chunks)         # idempotent; returns an IndexResult saying what was new
hits = store.search("does X cause Y", top_k=6)   # top_k defaults to TOP_K
```

Everything is local and synchronous. ChromaDB runs embedded — SQLite plus HNSW files
under `CHROMA_PERSIST_DIR` — and the embedding model runs on this machine. The only
network access in this package is Hugging Face's one-time model download, cached under
`~/.cache/huggingface` forever after.

## Similarity is NOT proof that a claim is true

This is the one fact about this package that must not be forgotten downstream, so it
is stated here, in `models.py`, on the `similarity` field itself, and pinned by a test
(`test_contradiction_scores_as_high_as_agreement`).

Cosine similarity measures how close two texts' *wording* is in embedding space.
"Coffee improves memory" and "coffee does not improve memory" differ by one word and
score almost identically against a query about coffee and memory — the index will
happily rank a claim's refutation as its best match. That is not a bug; it is what
makes the index useful for retrieval: *both* passages are exactly what a verifier
should read.

So the contract is:

- **Similarity decides what is worth reading.** It selects and orders candidate
  passages for a sub-question.
- **Reading decides what is true.** Support or contradiction is judged by the
  verification stage from the passage's content, never from this number.
- A verdict's confidence must not be derived from, scaled by, or tie-broken with
  similarity. A 0.9-similarity contradiction is still a contradiction.

## What the store guarantees

- **Idempotent indexing.** Chunk ids are UUIDv5 of document and span (stage 8), so
  the same passage indexed twice — within a batch or across runs — is stored once.
  `IndexResult` reports `newly_indexed` / `already_present` / `duplicates_in_batch`
  honestly rather than counting re-sends as work.
- **Full traceability.** Every chunk is stored with its document id, span offsets,
  content hash, source fingerprint, and the complete `SourceMetadata` (as one JSON
  field, losslessly; common fields also flat for filtering). A `RetrievedChunk` is
  rebuilt from that and **re-validated** — span arithmetic and fingerprint checks run
  again on what the store returned.
- **One collection, one model.** The embedding model's name is written into the
  collection metadata at creation; opening the collection with a different model
  raises immediately. Vectors from different models share no meaningful space, and a
  silent mix would return confident nonsense.
- **Chroma never embeds.** Its default embedding function is unused; every add and
  query passes vectors from the configured embedder.
- **An empty collection answers, it does not fail.** `search` on an empty collection
  returns `[]`, and `top_k` larger than the collection returns what exists.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | any Sentence Transformers model name |
| `EMBEDDING_BATCH_SIZE` | 32 | encode batch size |
| `CHROMA_PERSIST_DIR` | `../data/vectorstore` | on-disk location (gitignored) |
| `CHROMA_COLLECTION` | `autoresearch_evidence` | collection name |
| `TOP_K` | 6 | default result count for `search` |

Distances use cosine space over L2-normalised vectors; reported
`similarity = 1 - distance`, in `[-1, 1]`.

## Limitations, stated plainly

- **Characters of the chunks are what gets embedded** — all-MiniLM-L6-v2 truncates
  input past 256 word-pieces, so with the default 900-character chunks nothing is
  lost, but a much larger `CHUNK_SIZE` would silently embed only each chunk's head.
- **No deletion or re-ranking API yet.** Nothing needs them; they arrive with the
  stage that does.
- **The unit tests use a deterministic fake embedder** (bag-of-words). They prove the
  store's behaviour — indexing, dedup, ranking order, metadata round-trip — not the
  neural model's quality. The real model is exercised manually:

```bash
cd backend
./.venv/Scripts/python.exe -c "
from app.services.vectorstore import SentenceTransformerEmbedder
e = SentenceTransformerEmbedder()
v = e.embed_documents(['coffee improves memory', 'glaciers are retreating'])
q = e.embed_query('does coffee affect memory')
dot = lambda a, b: sum(x*y for x, y in zip(a, b))
print(len(v[0]), 'dims;', 'coffee:', round(dot(q, v[0]), 3), 'glaciers:', round(dot(q, v[1]), 3))
"
```

Expected: `384 dims`, and the coffee sentence scoring well above the glaciers one.
