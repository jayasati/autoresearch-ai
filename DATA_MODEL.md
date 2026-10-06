# Data Model

Seventeen tables, one Pydantic schema layer, three traceability chains.

This document explains what each entity is **for** and why it exists separately
from its neighbours. The tables are in `backend/app/models/`, the schemas in
`backend/app/schemas/`.

> **Status:** the schema is complete and tested. No external APIs are implemented,
> no migrations are generated yet, and **no rows ship with the project** — see
> [No seed data](#no-seed-data).

---

## 1. Identity: every object has a stable ID

Every table has a single-column primary key named `id`, holding a **UUIDv4
generated in Python at object construction** — not at INSERT, and not by a
database sequence.

Three reasons, in order of importance:

1. **The id exists before the row does.** A verification pass builds a claim, its
   evidence, its citations and its verdict as one in-memory graph and persists them
   in a single transaction. With a sequence, none of those objects could reference
   each other until each round-tripped through the database.
2. **It never changes and never collides.** An id quoted in a log line, an exported
   report, or a benchmark table means the same row forever, across every
   environment. An autoincrementing integer satisfies neither.
3. **It carries no information.** Sequential ids would leak how many runs exist.

SQLAlchemy's `default=uuid.uuid4` is an *insert-time* default, so it does not give
property 1 on its own. `app/db/base.py` adds an `init` event listener that assigns
the id at construction, keeping the column default as a safety net for rows created
by a bulk insert or a migration.

### Content fingerprints: identity vs. equality

Three tables also carry a **fingerprint** — a SHA-256 hash of the content that
defines sameness. The UUID answers "which row is this"; the fingerprint answers
"is this the same thing as that".

| Table | Fingerprint of | Why it matters |
|---|---|---|
| `run_configuration` | every setting that shapes a run, including prompt versions | two runs are comparable **iff** their fingerprints match |
| `source` | the DOI, else canonical URL, else external id | the same paper retrieved by two runs must be one row |
| `document` / `document_chunk` | the extracted text | a re-fetch that changed nothing is a no-op |

---

## 2. The entities

### Run and configuration

#### `run_configuration`
An **immutable, content-addressed snapshot** of everything that shapes a run: the
model, temperature, embedding model, chunk size, overlap, top-k, budgets, and the
version of every prompt.

A separate table rather than columns on the run, because the benchmark's entire
validity rests on knowing that two runs differed *only* in the thing under test.
With a fingerprint, "same configuration" is a fact you can join on rather than a
claim someone makes in a results table.

**Prompt versions are part of the fingerprint.** Changing a prompt changes the
output, so it must make the two runs non-comparable. Tests assert that changing any
one field — including a prompt version — changes the hash.

Retrieval settings must be `NULL` for `model_only` and present for the other two
modes. A chunk size on a no-retrieval run is not a harmless extra: it implies
retrieval that never happened, and would make the fingerprint depend on settings
that never applied.

#### `benchmark_run`
One execution of the topic set across every configuration under comparison. Groups
the `research_run` rows of a single experiment, so a comparison table cannot mix
runs from different experiments.

#### `research_run`
One research request, and the root of the traceability graph — every claim, source,
chunk, verdict and metric reaches this row by following foreign keys.

`normalized_topic` (lower-cased, whitespace-collapsed) is the grouping key for
comparison: `"RAG and  Hallucination"` and `"rag and hallucination"` are one topic.

A `failed` run **must** record an `error_code` (database check constraint). A
silently failed run would be counted as a run that produced nothing, corrupting
every metric computed over the set.

#### `subquestion`
One decomposed question from planning, with the search query actually issued.

Persisted because **coverage** is "how many planned sub-questions did the report
address" — unanswerable unless the plan is stored next to the result.

---

### Sources: three tables, not one

This is the distinction the whole traceability claim rests on.

```
Source      a thing in the world (a page, a paper). Global, deduplicated.
  │
Document    one fetched, extracted text of it, at one moment in time.
  │
Chunk       a span of that document, with its character offsets kept.
```

Collapsing them would make "this claim is supported by this passage" unprovable:
without the document you cannot say *which version* of a page was read, and without
offsets you cannot point at the sentence.

#### `source`
Deliberately **global, not per-run**. Two runs citing the same paper reference the
same row — otherwise source diversity cannot be computed, and the same paper could
be judged fabricated in one run and valid in another.

`source_type` is `web`, `academic`, or `model`. The last represents the model
asserting something with no external source at all. That case has to be
representable — it is how an uncited claim gets recorded instead of silently
dropped — so the check constraint requires an identifier for `web`/`academic` and
forbids one for `model`.

#### `document`
A web page is not a fixed object. Re-fetched a month later it may say something
different, and that is a **second document for the same source** — while a claim
verified against the first must keep pointing at the text that actually supported
it. `UNIQUE(source_id, content_hash)` makes an unchanged re-fetch a no-op.

#### `document_chunk`
`start_char` and `end_char` are offsets into `document.text`. They are the reason
this project can claim traceability at all: any claim can be followed to the exact
characters supporting it, not merely to the page it came from. A chunk without
offsets would make "grounded" unfalsifiable.

A check constraint enforces `end_char > start_char`, and the Pydantic schema goes
further — it rejects a span whose length does not equal the length of its text,
because such a chunk points at the wrong words.

Embeddings live in ChromaDB, not here; `embedding_id` is the handle. Vectors in
PostgreSQL would duplicate the vector store for no benefit.

#### `research_source`
The association between a run and a source — and not a plain many-to-many table,
because everything interesting about the *retrieval* is per-run: the query that
found it, its rank, its `citation_label` (`"S3"`), and **`evidence_depth`**.

Depth is per-run because one run may fetch full text where another only saw an
abstract. A claim grounded in full text and a claim grounded in an abstract are not
equally well supported, and without this column the groundedness metrics would
overstate the result.

---

### Report

#### `report`
At most one per run (`UNIQUE(research_run_id)`): a run either produced a report or
failed before doing so.

`markdown` is authoritative — claim offsets point into it. **Treat it as immutable
once claims are extracted**; editing it would silently invalidate every claim's
position.

#### `report_section`
A section with its span in the report markdown, so a claim can be attributed to a
section without duplicating text.

---

### The evidence layer

Four tables where a naive design would use one, because four different questions
must be answerable independently:

| Table | Answers |
|---|---|
| `claim` | what the report asserts |
| `citation` | what the report **said** its source was |
| `evidence` | what passages we **actually found** |
| `verification_result` | what we concluded, and on the strength of which passage |

**Keeping `citation` separate from `evidence` is the central decision.** A
fabricated citation is a citation with no resolvable source and no evidence behind
it. If citations and evidence were one table, that case could not be represented —
and it is precisely the case this project exists to count.

#### `claim`
One **atomic, individually checkable** assertion. Atomicity is what makes
verification meaningful: a sentence asserting three things cannot receive one
verdict honestly.

`requires_citation` is not decoration. Citation **recall** divides by the claims
where it is true, so getting it wrong would distort the result in the project's
favour. Definitions and framing sentences legitimately need no citation.

#### `evidence`
The join between the claim graph and the source graph:

```
Claim → Evidence → DocumentChunk → Document → Source
```

`relation` (`supports` / `contradicts` / `neutral`) is **per passage, not per
claim**. A claim can have supporting *and* contradicting evidence at once, and that
is exactly the input conflict detection needs. A single "is it supported" boolean on
the claim would erase it.

A passage asserted to support or contradict must carry `quoted_text` — otherwise
the relation is an opinion with nothing behind it.

#### `citation`
`source_id` is **nullable**, and that is the key modelling decision. A fabricated
citation points at a source that does not exist, so there is no row to reference. A
check constraint enforces the correspondence:

```sql
(status = 'fabricated' AND source_id IS NULL)
OR (status <> 'fabricated' AND source_id IS NOT NULL)
```

That makes "fabricated" a **structural fact** rather than a label someone remembered
to set. Two further constraints: a `valid` citation must name the passage that
supports it, and every non-valid status must give a `failure_reason`.

The four statuses are counted separately on purpose:

| Status | Meaning |
|---|---|
| `valid` | resolves, and a passage supports the claim |
| `broken` | the URL or DOI does not resolve |
| `misattributed` | **real source**, but it does not support this claim |
| `fabricated` | **no such source exists** |

`fabricated` and `misattributed` are different defects with different causes.
Collapsing them into one "bad citation" flag would hide the most interesting result
this project can produce.

#### `verification_result`
One verdict per claim, with the evidence it rests on.

ARCHITECTURE.md §7 lists "the verifier itself hallucinates" as a known risk, with
the mitigation: *require it to quote the supporting span; a verdict without a quote
is `not_enough_evidence`*. That mitigation is **enforced in the schema**:

```sql
verdict IN ('unsupported', 'not_enough_evidence')
OR supporting_evidence_id IS NOT NULL
```

The Pydantic layer implements it as a **downgrade, not a rejection**. The verifier's
output is data *about the verifier*: refusing it would discard the observation,
while downgrading it and setting `downgraded = true` keeps it — so "how often did
the verifier overclaim" becomes a measurable number rather than a lost error.

`downgraded` results do **not** count as hallucinations. "We could not tell" is a
different finding from "this is wrong".

#### `conflict`
Recorded between two `evidence` rows, not between two sources. "These two papers
disagree" is not actionable; "these two passages disagree about this claim" names
the claim, both passages, and through them both sources — so the UI can *show* the
disagreement rather than assert it.

---

### Evaluation and cost

#### `evaluation_result`
Stored **long** (one row per metric) rather than wide (one column per metric).
Adding a metric then needs no migration, and the comparison table the UI renders is
metric × configuration — which is this shape, pivoted.

`numerator` and `denominator` are kept alongside `value` because **"claim support
rate: 0.80" is not a reportable finding on its own.** Over five claims it is weak
evidence; over five hundred it is strong. A ratio without its terms cannot be
defended, pooled across topics, or checked for an arithmetic mistake. The Pydantic
validator rejects a value that does not equal `numerator / denominator`.

A `denominator` of zero is rejected rather than stored. A run that produced no
claims has an **undefined** support rate; recording `0.0` would make a run that
generated nothing look maximally unreliable.

`metrics_version` is part of the uniqueness key. If a definition changes, the old
numbers are not wrong — they measured something else. Overwriting them would quietly
make an old benchmark incomparable with a new one.

#### `llm_call_log`
One row per model call: stage, model, prompt version, tokens, cost, latency,
outcome, and the HTTP `request_id` that triggered it.

Not optional. Without it neither a benchmark result nor an API bill can be explained
afterwards — and "this configuration is better" is only a finding if you can also
say what it cost. It is also the only place a **failed provider call** is recorded,
which keeps "the run failed because Tavily was down" distinguishable from "the run
failed because our logic is wrong".

---

## 3. The three traceability chains

Each chain is materialised as an explicit Pydantic response model in
`app/schemas/trace.py`, so "the data is traceable" is checkable rather than
something you reconstruct by writing the right joins. Each has validators that make
a **broken** chain impossible to serialise.

### Chain 1 — Evidence
```
research_run → report → claim → citation → source → document → document_chunk
                                                                 └─ (start_char, end_char)
```
`EvidenceTrace.is_fully_traceable` is **computed, not asserted**: it requires at
least one citation that is `valid` *and* resolves to both a source and a passage. A
claim whose citations all failed validation looks cited but is not traceable — which
is exactly why this must be derived.

### Chain 2 — Verification
```
claim → evidence (0..n passages, each with a stance) → verification_result
                                                         └─ supporting_evidence_id
```
`VerificationTrace` lists **all** evidence considered, not just the passage that
won, and names the decisive one. Showing only the supporting passage would hide
contradicting evidence that was weighed and rejected — precisely what a reader needs
in order to disagree. A validator rejects a `decisive_evidence_id` that is not among
the listed evidence.

### Chain 3 — Evaluation
```
research_run → run_configuration (fingerprint) → evaluation_result (metric, value, terms)
```
`EvaluationTrace` embeds the configuration in full rather than referencing it: a
metric without the settings that produced it is not interpretable. A validator
rejects a trace mixing two `metrics_version`s, because those are not measurements of
the same thing.

`BenchmarkComparison` computes a `comparable` flag and refuses to present an
unbalanced comparison silently: if `model_only` ran ten topics and `hybrid` ran
seven, the difference between their numbers includes a difference in what they were
asked.

---

## 4. Entity relationship summary

```
benchmark_run ──┐
                ├──< research_run >── run_configuration
                │         │
                │         ├──< subquestion
                │         ├──< research_source >── source ──< document ──< document_chunk
                │         ├──< llm_call_log                                      ▲
                │         ├──< evaluation_result                                 │
                │         ├──< conflict >─┬─ evidence ───────────────────────────┤
                │         │               └─ evidence                            │
                │         └─── report ──< report_section                         │
                │                   │         ▲                                  │
                │                   └──< claim ┘                                 │
                │                         ├──< evidence ────────────────────────-┘
                │                         ├──< citation >── source
                │                         │        └── supporting_chunk ── document_chunk
                │                         └─── verification_result
                │                                   └── supporting_evidence ── evidence
```

`>──` = many-to-one · `──<` = one-to-many

### Delete behaviour

| Action | Effect | Why |
|---|---|---|
| delete a `research_run` | cascades to its plan, report, claims, evidence, citations, verdicts, conflicts, metrics and call log | all of it is *about* that run |
| delete a `research_run` | **does not** delete `source` rows | sources are shared; another run may still cite the paper |
| delete a `run_configuration` | **RESTRICT** — refused while runs reference it | losing it would make every metric computed under it uninterpretable |
| delete a `source` | cascades to its documents and chunks | they are extractions *of* it |

Every foreign key declares an explicit `ON DELETE` rule; a test enforces that none
is left unspecified, since the default (`NO ACTION`) strands orphan rows.

---

## 5. Validation: enforced in two places on purpose

Pydantic validates what arrives over HTTP. Database constraints hold for anything
that reaches the database — a script, a migration, a `psql` session. **The
invariants that matter are enforced in both**, because a rule that lives only in the
API layer is a rule the next data-loading script will break.

| Invariant | Pydantic | Constraint |
|---|---|---|
| chunk span is non-empty and matches its text | ✓ (both) | ✓ (non-empty) |
| fabricated citation has no source | ✓ | ✓ |
| valid citation names a passage | ✓ | ✓ |
| non-valid citation gives a reason | ✓ | ✓ |
| evidence-bearing verdict cites evidence | ✓ (downgrade) | ✓ (reject) |
| conflict needs two different passages | ✓ | ✓ |
| ratio in `[0, 1]`, value matches its terms | ✓ | ✓ (bounds) |
| denominator > 0 | ✓ | ✓ |
| failed run records an error code | — | ✓ |
| a source is linked to a run once | — | ✓ |
| retrieval settings match the mode | ✓ | partial |

Request schemas use `extra="forbid"`: `topik` instead of `topic` is rejected, not
silently defaulted. A typo in a benchmark script would otherwise run the wrong
configuration and nobody would notice.

### Enum storage

SQLAlchemy's `Enum` stores the member **name** (`MODEL_ONLY`) by default, while the
API, the frontend and the benchmark tables all speak in **values** (`model_only`).
`enum_column()` in `app/db/base.py` sets `values_callable` so the database stores the
value — otherwise raw SQL and every export would disagree with the API about what a
mode is called. A test asserts the stored string directly.

`native_enum=False` renders `VARCHAR` + `CHECK` rather than a PostgreSQL `ENUM`
type, which makes adding a member a data migration instead of a DDL one, and lets the
same model run against SQLite in tests.

---

## No seed data

**Nothing in this project inserts rows.** No fixtures, no demo records, no sample
report, no example claims or metrics. A data model with sample rows baked in would
put fabricated research output into every environment that ran it — exactly what this
project must not do. A test asserts that creating the schema leaves every table
empty.

Tests build throwaway rows in an **in-memory** database and discard them. Their
values are deliberately non-plausible: topics read `<topic under test>`, claims read
`<claim 0 under test>`, and URLs use `example.invalid` — a TLD reserved by RFC 2606
that can never resolve. If any of it ever escaped into a screenshot it would be
unmistakable.

---

## Portability

Written for PostgreSQL, tested against SQLite in memory so the suite needs no
running server. That gap is a real risk — a model can pass every SQLite test and
fail to deploy — so it is closed two ways:

1. SQLite foreign-key enforcement is switched **on** (`PRAGMA foreign_keys=ON`).
   SQLite ignores foreign keys by default; without the pragma the tests would pass
   against constraints PostgreSQL would reject, and the schema would look correct
   and be wrong.
2. `tests/integration/test_schema_portability.py` compiles the same metadata for the
   **PostgreSQL dialect** and asserts the parts that differ: native `UUID`,
   `TIMESTAMP WITH TIME ZONE`, `VARCHAR`-backed enums, and that every constraint has
   a name Alembic can reference.

PostgreSQL was running on the development machine while this was written, but its
credentials were not available, so a live round-trip could not be performed. DDL
compilation is the strongest check possible without them.

---

## What is not here yet

- **Migrations.** No Alembic. The schema is created from metadata in tests. The
  first migration comes with the run-lifecycle endpoints.
- **Repositories / services.** No queries beyond what the tests traverse.
- **API endpoints.** No route exposes any of this. `app/schemas/` defines the
  contract; nothing serves it.
- **External APIs.** No OpenAI, Tavily, Semantic Scholar, ChromaDB or embedding
  code — out of scope for this stage by instruction.
