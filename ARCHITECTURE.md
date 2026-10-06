# AutoResearch AI — Architecture

> Living document. Sections marked *implemented* reflect code that exists;
> *planned* ones describe design intent for a stage not yet built.

---

## 1. Design goals

| Goal | Consequence for the design |
|---|---|
| Every claim must be traceable to a source passage | character offsets are preserved from fetch → chunk → embed → retrieve |
| Three configurations must be comparable | retrieval is swappable behind one interface; everything else is held constant |
| Hallucination must be measurable, not assumed | claim extraction and verification are separate stages *after* generation |
| A student budget must survive the project | hard caps on LLM calls/sources per run; embeddings run locally and free |
| Results must be reproducible for grading | prompts are versioned files; benchmark inputs are committed |

### A note on the central design decision

Verification runs **after** generation, as an independent pass over the finished
report — not as a constraint during writing. This is deliberate. If the writer
also judged its own groundedness, we could not measure hallucination, because the
measurement and the behaviour would share a failure mode. Separating them means
the verifier can disagree with the writer, and that disagreement is the signal
the whole project reports on.

---

## 2. Layers

```
+--------------------------------------------------------------+
|  frontend/            React + Vite                           |
|  topic input - run timeline - report + claim inspector -      |
|  evidence drill-down - benchmark dashboard                    |
+---------------------------+----------------------------------+
                            | JSON over HTTP
+---------------------------v----------------------------------+
|  app/api/v1/            FastAPI routes - thin, no logic       |
+--------------------------------------------------------------+
|  app/services/          use cases, ONE transaction each       |
+--------------------------------------------------------------+
|  app/repositories/      queries; never commit                 |
+--------------------------------------------------------------+
|  app/pipelines/         model_only | hybrid | search_grounded |
+--------------------------------------------------------------+
|  app/agents/            planner -> retriever -> synthesizer   |
|                         -> critic, driven by orchestrator     |
+--------------------+------------------+----------------------+
| app/retrieval/     | app/evidence/    | app/evaluation/      |
| web - academic -   | claims -         | metrics -            |
| fetch - chunk -    | verification -   | benchmark            |
| embed - vector -   | citations -      |                      |
| rag                | conflicts        |                      |
+--------------------+------------------+----------------------+
|  app/models/ + app/db/   SQLAlchemy over PostgreSQL          |
+--------------------------------------------------------------+

External: OpenAI - Tavily - Semantic Scholar - ChromaDB (local)
```

The dependency rule: **arrows point downward only.** `agents/` may call
`retrieval/`; `retrieval/` never imports `agents/`. `agents/` and `retrieval/`
never import `models/` — persistence is the service layer's job. This is what
makes the benchmark possible: a pipeline can be run in-process with no database.

---

## 3. The research run, end to end

```
topic
  |
  +- 1. PLAN         planner -> sub-questions -> search queries
  |
  +- 2. RETRIEVE     per sub-question, depending on mode:
  |                    web_tavily        -> results + snippets
  |                    semantic_scholar  -> papers + abstracts
  |                    fetcher           -> full text
  |                    chunker -> embedder -> vector_store (Chroma)
  |
  +- 3. DRAFT        synthesizer: top-k passages -> report section
  |                  with inline citation markers [S3]
  |
  +- 4. CRITIQUE     critic flags statements with no visible support
  |
  +- 5. EXTRACT      claim_extractor: report -> atomic claims
  |
  +- 6. VERIFY       per claim: evidence_linker -> verifier
  |                    -> supported | partially_supported | unsupported
  |                       | contradicted | not_enough_evidence
  |
  +- 7. VALIDATE     citation_validator: does the cited source exist,
  |                  resolve, and support this specific claim?
  |                    -> valid | broken | misattributed | fabricated
  |
  +- 8. CONFLICTS    conflict_detector: pairwise source disagreement
  |
  +- 9. SCORE        metrics over the whole run
  |
  +- 10. PERSIST     run + report + claims + evidence + metrics
```

Status transitions (`RunStatus` in `app/core/constants.py`) are emitted at each
boundary so the frontend can show a live timeline instead of a spinner.

---

## 4. Module responsibilities

### `app/core/` *(exists)*

`config.py` — the single source of truth for every tunable. `constants.py` — the
shared vocabulary (`ResearchMode`, `RunStatus`, `VerificationVerdict`,
`CitationStatus`, `ConflictType`). Defining these enums first means the database,
the API and the React UI cannot drift apart.

### `app/repositories/` + `app/services/` *(implemented)*

Repositories own queries, one entity type each, and **never commit**. Services own
the transaction: each public method is one atomic operation that either completes and
commits or leaves the database untouched.

The split exists because a research run produces a connected graph — report, claims,
evidence, citations, verdicts. Committing it in pieces means a mid-way failure leaves
a run whose claim set is silently incomplete, and every metric over it is then wrong
in a way that looks like a finding. See DATA_MODEL.md and `app/repositories/README.md`.

### `app/agents/` *(planned)*

The orchestration loop. Single-responsibility steps; the orchestrator sequences
them and enforces `MAX_LLM_CALLS_PER_RUN`. Run state is a plain object, not an
ORM row, so a pipeline can execute without a database.

### `app/retrieval/` *(planned)*

One `Retriever` protocol, three implementations. The chunker preserves
`(start_char, end_char)` per chunk — without that, requirement 8 (traceability)
is impossible to honour and "grounded" becomes unverifiable.

### `app/evidence/` *(planned)*

The project's core contribution:

- **claim extraction** — report prose → atomic, individually checkable claims
- **verification** — each claim judged against its linked passages
- **citation validation** — distinguishes four distinct failure modes. A
  *fabricated* citation (no such paper) and a *misattributed* one (real paper,
  wrong claim) are different defects and are counted separately
- **conflict detection** — sources that disagree numerically, directionally,
  or across time
- **traceability** — the claim → passage → source → URL chain, serialized for
  the UI's drill-down

### `app/evaluation/` *(planned)*

Metrics (groundedness, citation precision, claim support rate, hallucination
rate, coverage, source diversity) and the benchmark harness that holds the topic
set and prompts constant while varying only `ResearchMode`.

### `app/pipelines/` *(planned)*

Three thin compositions. Separate files rather than one branching function,
because the benchmark's validity depends on the differences between modes being
*readable* in the diff.

---

## 5. Data model *(implemented)*

Seventeen tables. **[DATA_MODEL.md](DATA_MODEL.md) is the authoritative reference** —
it explains every entity and the reasoning behind each separation. The sketch below
is the shape only.

```
benchmark_run ──< research_run >── run_configuration   (content-addressed settings)
                       |
                       +──< subquestion                (the plan; coverage needs it)
                       +──< research_source >── source ──< document ──< document_chunk
                       +──< llm_call_log                                      ^
                       +──< evaluation_result                                 |
                       +──< conflict >─ evidence, evidence                    |
                       +─── report ──< report_section                         |
                                 |          ^                                 |
                                 +──< claim ─+                                |
                                       +──< evidence ─────────────────────────+
                                       +──< citation >── source
                                       +─── verification_result
```

Four points that differ from the original sketch, each for a reason recorded in
DATA_MODEL.md:

- **`document` was added** between source and chunk. A source is a thing in the
  world; a document is one extraction of its text at one moment. Without that, a
  re-fetched page that now says something different cannot be represented, and a
  claim verified against the old text loses its ground.
- **`run_configuration` was added** as its own content-addressed table. The
  benchmark's validity rests on "these two runs differed only in the thing under
  test", and a fingerprint makes that a fact you join on rather than a claim.
- **`research_source` was added** as an association table, because `source` is
  global (deduplicated across runs) while retrieval facts — the query, the rank, the
  evidence depth — are per-run.
- **`run_metric` became `evaluation_result`**, stored long with the numerator and
  denominator behind every ratio.

`llm_call_log` remains as promised: model, prompt version, tokens and cost per call,
which is what makes a benchmark result defensible and a bill explainable.

---

## 6. Technology choices and trade-offs

| Decision | Why | Cost we accept |
|---|---|---|
| OpenAI API, not a local LLM | quality and reliability matter more than independence here; we are not training | per-call cost; vendor dependency |
| Sentence Transformers locally | embeddings are called thousands of times; local is free and fast | a ~90 MB model download; needs Python <= 3.13 |
| ChromaDB, not pgvector | zero-setup, embedded, persistent; good enough at project scale | a second store to reason about |
| PostgreSQL for structured data | real relational integrity for the claim/evidence graph | needs a running server |
| Tavily, not raw scraping | returns clean, LLM-ready results; respects the sources | API quota |
| Semantic Scholar | free, broad, no auth needed to start | abstracts are often all we get; full text is inconsistent |
| Post-hoc verification | makes hallucination measurable (see §1) | extra LLM calls per run |

Decisions that turn out to be wrong get an ADR in `docs/adr/` explaining the
reversal, rather than a silent edit to this file.

---

## 7. Known risks

| Risk | Mitigation |
|---|---|
| Python 3.14 breaks the ML dependency install | pin `>=3.11,<3.14`; use a 3.12 venv |
| Verification cost explodes on long reports | cap claims per run; batch verification prompts |
| The verifier itself hallucinates | require it to quote the supporting span; a verdict without a quote is `not_enough_evidence` |
| Paywalled papers yield abstract-only evidence | record `evidence_depth` per source so metrics do not overstate grounding |
| Benchmark results not reproducible | version prompts; log every LLM call; commit topic sets |
