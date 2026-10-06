# retrieval/ — Getting external knowledge in

Three independent backends behind one interface so research modes can be
swapped without touching the orchestrator.

Planned modules:
- `base.py` — `Retriever` protocol: `search(query) -> list[RetrievedDocument]`
- `web_tavily.py` — Tavily web search
- `academic_semantic_scholar.py` — Semantic Scholar paper + abstract search
- `fetcher.py` — URL → clean text (politeness, timeouts, robots respect)
- `chunker.py` — text → overlapping chunks with char offsets preserved
- `embedder.py` — Sentence Transformers wrapper (batched, cached)
- `vector_store.py` — ChromaDB upsert/query
- `rag.py` — chunk → embed → store → top-k retrieve pipeline
- `reranker.py` — optional cross-encoder rerank over top-k

Offsets matter: every chunk keeps its character span in the source document so
a claim can be traced back to an exact passage (requirement 8).
