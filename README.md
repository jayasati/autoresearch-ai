# AutoResearch AI

An evidence-grounded agentic research system. Give it a research topic; it
produces a research report where **every claim is traceable to a real source**,
and it measures how well it did.

> **Status: Stage 1 — scaffolding only.**
> The project structure, configuration surface and documentation exist. No
> research logic is implemented yet. See [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md)
> for exactly what is and is not done.

---

## What makes this different from "ask a chatbot"

A language model asked for a research report will produce fluent text with
plausible-looking citations, some of which do not exist. This project treats
that as the problem to be measured and reduced. It does so by:

1. Decomposing the topic into sub-questions (planning)
2. Retrieving real sources — web + academic papers
3. Writing the report grounded in retrieved passages
4. **Extracting every atomic claim** from the report
5. **Verifying each claim** against retrieved evidence
6. **Validating each citation** actually exists and supports its claim
7. **Detecting conflicts** between sources
8. Keeping a **traceability chain**: claim → passage → source → URL
9. Scoring the run with quality metrics
10. **Benchmarking** three configurations against each other

### The three configurations under comparison

| Mode | Sources | Purpose |
|---|---|---|
| `model_only` | none (parametric knowledge) | the baseline we expect to hallucinate |
| `hybrid` | + web search (Tavily) | does web grounding help? |
| `search_grounded` | + academic papers + RAG over full text | does deep grounding help more? |

The project's result is not just the report generator — it is the **evidence
that grounding measurably improves factuality**.

---

## Tech stack

We are **not training a model.** Everything below is orchestration we build.

| Concern | Choice |
|---|---|
| Foundation model | OpenAI API |
| Embeddings | Sentence Transformers (local, free) |
| Vector store | ChromaDB (embedded, persistent) |
| Web search | Tavily |
| Academic search | Semantic Scholar API |
| Structured data | PostgreSQL |
| Backend | FastAPI (Python) |
| Frontend | React + Vite |

---

## Repository layout

```
autoresearch-ai/
├── backend/            FastAPI service — orchestration, retrieval, evidence, evaluation
├── frontend/           React + Vite UI
├── docs/               Architecture, ADRs, API notes, evaluation protocol
├── data/               Vector store, benchmark topic sets, exports (mostly gitignored)
├── scripts/            Dev helper scripts
├── notebooks/          Exploratory analysis of benchmark results
├── .env.example        Every environment variable the system reads
├── ARCHITECTURE.md     How the system is designed and why
└── DEVELOPMENT_LOG.md  Stage-by-stage build journal
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full module-by-module breakdown.

---

## Running it (stage 1)

Right now "running it" means: the backend boots and answers `/health`, and the
frontend loads and shows that it reached the backend. Nothing else.

### Prerequisites

- **Python 3.11 or 3.12** — see the note below
- **Node.js 18+** (you have 22)
- PostgreSQL — *not needed until stage 3*
- API keys — *not needed until stage 4*

> **⚠️ Python version:** this machine has Python 3.14. `torch` /
> `sentence-transformers` / `chromadb` do not reliably ship wheels for 3.14 yet,
> and installing them will likely fail or try to build from source. Create the
> virtual environment with **Python 3.12** before stage 4. The stage-1 backend
> (FastAPI only) runs fine on 3.14 if you want to start immediately.

### Backend

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# .venv\Scripts\Activate.ps1       # Windows PowerShell
# source .venv/bin/activate        # macOS / Linux

pip install -r requirements-dev.txt
cp ../.env.example ../.env         # safe to leave the placeholder keys for now

uvicorn app.main:app --reload --port 8000
```

Verify:

| URL | Expected |
|---|---|
| http://localhost:8000/health | `{"status":"ok", ...}` |
| http://localhost:8000/docs | interactive OpenAPI page |
| http://localhost:8000/api/v1/system/capabilities | which integrations are configured |

Run the smoke tests:

```bash
cd backend
pytest
```

### Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. You should see **"Connected — v0.1.0 (development)"**.
If it says not reachable, the backend isn't running on port 8000.

---

## Next stage

Stage 2 is the data model and the run lifecycle. See the
[roadmap in DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md#roadmap).

---

## Academic honesty note

This is a college project. The point of the evidence layer is that the system's
own output can be audited — including by whoever grades it. Benchmark numbers
are reproducible from `data/benchmarks/` plus the committed prompt versions in
`backend/app/prompts/`; re-running a benchmark regenerates the comparison tables
rather than hand-editing them.
