# pipelines/ — The three research configurations

Each pipeline is a thin composition over `agents/` + `retrieval/`. They exist as
separate entrypoints so the benchmark can hold everything else constant.

- `model_only.py` — LLM parametric knowledge only (the baseline). No retrieval.
- `hybrid.py` — model + Tavily web search.
- `search_grounded.py` — model + web + Semantic Scholar + RAG over fetched
  full text. The full evidence pipeline runs on this one.
