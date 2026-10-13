# Experiment metrics — exact definitions (metrics-v1)

The implementations live in `backend/app/services/benchmark/metrics.py`; each
function's docstring carries the same formula line written here, and a test asserts
the two files agree. **There is no overall score anywhere**: each metric answers one
question and is reported with its numerator and denominator, because 0.8 over five
claims and 0.8 over five hundred are different findings. A metric with an empty
denominator is reported as undefined (None), never as zero.

## The arms

| Arm | Pipeline | What counts as product cost |
|---|---|---|
| A `model_only` | topic → LLM → report | the one synthesis call |
| B `hybrid` | topic → web search → LLM | retrieval + synthesis |
| C `search_grounded` | topic → web + academic → RAG → LLM | retrieval + indexing + synthesis |
| D `full_autoresearch_verified` | C + claim extraction + verification + final report | everything, including verification |

Every arm is **evaluated identically**: claims are extracted from its report and
verified against the run's own retrieved evidence (A has none — its claims verify to
`not_enough_evidence`, which is the measured property of an ungrounded baseline, not
an error). For arms A–C that evaluation work is overhead billed separately
(`evaluation_tokens`, `evaluation_latency_seconds`); for D it is the product.

## Claim and citation metrics (from stored verification rows)

- **claim_support_rate** = (supported + partially_supported) / total_claims.
  `partially_supported` counts as support and is also reported on its own; a
  fractional weight would be an arbitrary constant.
- **unsupported_claim_rate** = (unsupported + contradicted) / total_claims.
- **citation_precision** = valid / (valid + misattributed + fabricated + broken).
  Unvalidated citations (from claims skipped in verification) are excluded: they
  were never judged.
- **citation_coverage** = claims_with_at_least_one_citation / claims_requiring_citation.
- **conflict_detection_rate** = |items with expected conflicts where ≥1 conflict was
  stored| / |items with expected conflicts|. Per-arm; items whose ground truth
  expects no conflict contribute to neither term.

## Retrieval metrics (only where ground truth exists)

Computed **only for items with non-empty `expected_evidence`** and only for arms
that retrieve. Relevance labels come from a fixed LLM judge
(`relevance-judge-v1`) that marks which retrieved passages establish which expected
statements; it is identical for every arm, answers over indices so it can invent
nothing, and its raw pairs are stored in `results.json` for audit. K is the depth
profile's `top_k`. The ranking is the run's own evidence ranking (replayed vector
search for C/D; provider rank order for B).

- **precision@K** = |relevant chunks among the first K retrieved| / min(K, retrieved).
- **recall@K** = |expected evidence statements covered by the first K chunks| /
  |expected statements|. The classical denominator (all relevant documents in the
  corpus) is unknowable against the live web, so recall is defined over the item's
  expected statements — the ground truth the dataset actually has.
- **nDCG@K** = DCG@K / IDCG@K with binary gains:
  DCG@K = Σᵢ relᵢ / log₂(i+1) in retrieval order; IDCG@K is the same sum with the
  relevant items moved to the top. Undefined when nothing retrieved is relevant.

## Cost and performance

- **latency** — `product_latency_seconds`: wall-clock of the arm's product pipeline
  (D includes extraction, verification and finalization). Evaluation overhead for
  A–C is reported separately as `evaluation_latency_seconds`.
- **token usage** — prompt/completion/total, split the same way
  (`product_tokens` / `evaluation_tokens`).
- **estimated_cost_usd** = prompt_tokens × input_price + completion_tokens ×
  output_price, at the provider's published per-token prices
  (gpt-4o-mini: $0.15 / $0.60 per million). "Estimated" because published prices
  change and cached-input discounts are ignored; at these rates it is an upper
  bound. A model with no listed price yields None, never $0.00.

## Aggregation (audit CD-3)

Per-arm summaries are **macro-averages**: the per-item metric is computed first and
items are averaged with equal weight, regardless of how many claims or citations
each item produced. A micro-average (pooling numerators and denominators across
items) can differ — in the first frozen run, D's support rate was 0.671 macro vs
0.667 micro — and is not currently reported. Means are taken over items where the
metric is defined, with n stated; failed cells are excluded from means and counted
in the failure rate.

## Structural values (audit CD-2)

Some baseline values are fixed by the arm's construction, not observed:
`model_only` retrieves no evidence (its claim support rate can only be 0.0), its
prompt forbids citations (citation coverage can only be 0.0), and claims citing
nothing cannot be contradicted by their citations (its unsupported-claim rate's 0.0
is not a quality signal). The generated report marks these cells with † and a
footnote. They demonstrate the definition of the metric; the empirical content of
the experiment lives in the differences among the retrieval arms.

## The final comparison (baseline vs AutoResearch)

- **failure_rate** = items_failed / items_attempted, per arm. A failed cell is a
  recorded row with its error; it is never retried into silence or dropped.
- **absolute_difference** = autoresearch − baseline, per metric
  (baseline = `model_only`, AutoResearch = `full_autoresearch_verified`).
- **relative_difference** = (autoresearch − baseline) / |baseline|, only where
  meaningful: omitted when either value is undefined or the baseline is 0, because
  a ratio over zero asserts an infinity nobody measured.
- **direction** classifies each difference as improved / worse / unchanged against
  the metric's better direction (declared per metric in code), mechanically — so
  no prose can soften a regression.
- **matched-items comparison** (audit CD-1): when the compared arms completed
  different item subsets (cells can fail), the report additionally shows the same
  comparison restricted to the items both arms completed, and names each arm's
  missing items. The headline table remains the all-completed-items view.

## Provenance

Raw results are stored three ways, from one payload: `results.json` (full rows, run
ids, judge pairs), `results.csv` (flat table), `evaluation_summary.md` (per-arm
means with n). Per-run rows also land in the `evaluation_result` table under
`metrics-v1` where a `MetricKey` exists for the metric. Results are never edited by
hand; a wrong number is fixed by fixing the code and re-running, which produces a
new, dated output directory.
