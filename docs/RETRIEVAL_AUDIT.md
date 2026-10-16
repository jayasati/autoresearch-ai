# Evidence-Retrieval Pipeline — Audit and Measured Improvements (stage 26)

Date: 2026-10-16. Scope: Tavily and Semantic Scholar retrieval, URL/source
normalization and deduplication, chunking, embeddings, ChromaDB search, top-k
selection, and evidence assembly. Every change below is driven by a failure case
measured on the stage-19 development runs (committed artifacts + database rows) —
never by assumption. The frozen test set was not touched and selected nothing.

## 1. Measured failure inventory (stage-19 development runs, 8 questions × arms)

**(a) Academic starvation is the dominant failure.** Semantic Scholar returned
**zero candidates in 9 of 14 semantic-arm cells** (4 requested each); across all
semantic cells it produced 14 source rows, 11 of them metadata-only (no
abstract). **Zero academic sources ever received a citation label** — all 42
evidence slots went to web snippets. Root cause, confirmed live (2026-10-16
probe, 7 of 8 dev questions): the raw interrogative question starves the
`/paper/search` endpoint — `total: 0` for 5 of 7 questions — while a keyword form
of the same question matches papers for all of them (coffee question: 0 →
31,143 available, 3 abstracts in the top 4). So "academic abstracts losing to
web snippets because of wording overlap" is NOT what the data shows: they lose
by never arriving.

**(b) Precision is the weak retrieval axis; recall saturates.** Baseline
(relevance-judge-v1 over `expected_evidence`, k=4): P@4 0.45–0.50, R@4 1.0,
nDCG@4 0.78–0.84 across the three retrieving arms. A replay joining the ranked
top-4 against the stored judge labels attributes the irrelevant half across
reputable domains too (PMC, CDC, Harvard Health) — a selection problem, not
merely SEO junk. The measured mechanism: 2–3 near-duplicate chunks per source
(overlapping snippet chunks), and plain cosine top-k happily takes several of
them.

**(c) SEO/low-quality pages do reach evidence** — measured instances include
`dromedairy.com` (a camel-milk vendor, on the camel-milk question),
`newportacademy.com`, `gavinpublishers.com` — but they are a minority of labeled
sources, and the judged-irrelevant half of selections is NOT dominated by them
(see b). No domain-quality list was introduced: that would be a judgment, not a
measurement, and the measured problems lie elsewhere.

**(d) Duplicates:** zero same-title duplicate source rows within any dev run —
the shared canonicalisation/fingerprint design works. The gap is
**cross-channel identity**: `pmc.ncbi.nlm.nih.gov` is the single most-retrieved
web domain (15 rows, 11 labeled), and those PMC articles are the same documents
academic search returns as papers; a PMC URL and the paper's DOI could never
deduplicate.

**(e) Channel vs source type:** the schema distinguishes them
(`research_source.retriever` vs `source.source_type`) but the type is assigned
from the channel, so academic content arriving via Tavily is typed `web` (the
15 PMC rows). Recorded as a limitation; typing by content would change metric
definitions and is not done here.

**(f) Hybrid evidence concentrates:** `provider_rank` selection fills its
4-passage budget from 1–3 distinct sources (one run: a single source).
**Unchanged on purpose** — that selection rule is the hybrid arm's experimental
definition; changing it would redefine the arm, not improve the pipeline.

**(g) Topical-but-unsupporting evidence** is already measured end-to-end by the
verification stage (relevance ≠ support is enforced there); retrieval-side, it
is the P@4 number in (b).

## 2. Changes (each small, each tested)

1. **Academic query keywordization**
   (`semantic_scholar_service.keywordize_academic_query`): question-shaped
   queries (ending in "?" or opening with an interrogative/auxiliary) are
   reduced to their content words, order preserved; keyword queries pass through
   untouched; a rewrite that would empty the query falls back. Both forms are
   recorded (`AcademicSearchResult.effective_query`; candidates carry what
   produced them). Live-measured basis in §1a.
2. **Cross-channel deduplication** (`urls.scholarly_aliases`,
   `identity_keys()` on both candidate models, orchestrator `_retrieve`):
   candidates are duplicates when they share ANY identity key — fingerprint,
   DOI, PMID, PMCID, arXiv id, or a scholarly identifier parsed from a PMC /
   PubMed / arXiv / doi.org URL. On a collision the candidate with **more text**
   wins — content decides, never the channel, so a metadata-only paper cannot
   displace a quotable snippet of itself. `source_fingerprint` is deliberately
   unchanged: it is the stored global identity, and recomputing it would split
   every existing source row's identity.
3. **Publication provenance for web sources** (`CandidateSource.publication_year`):
   a leading plausible four-digit year from the provider's `published_date` now
   persists to `source.publication_year`; anything less trustworthy is dropped
   rather than guessed.
4. **MMR as an optional selection strategy** (`VectorStore.search_mmr`,
   `EVIDENCE_SELECTION` setting, default `similarity` — the behavior is opt-in,
   never a silent redefinition of the arm). The benchmark's retrieval-metric
   replay honors the configured strategy. Measured basis in §3.
5. **Provider-aware interleaving: evaluated and NOT adopted.** On the stage-19
   pools there is nothing to interleave (zero academic chunks — §1a), and the
   premise "academic sources always win" is exactly what the data refuses to
   support. It remains a candidate strategy to re-evaluate once post-fix pools
   contain both channels.

## 3. Measurements

**Selection strategies** (offline replay over stage-19 persisted chunks, 12
cells, relevance-judge-v1, k=4 — `scripts/experiment_selection.py`, artifact in
`data/benchmarks/results/retrieval-experiments/`):

| strategy | P@4 | R@4 | nDCG@4 | distinct sources |
|---|---|---|---|---|
| plain cosine top-k (current) | 0.458 | 1.0 | 0.744 | 2.83 |
| per-source cap (2) | 0.458 | 1.0 | 0.744 | 2.83 |
| **MMR λ=0.7** | **0.583** | **1.0** | **0.819** | **3.5** |

The per-source cap never produced a different selection (the duplicates that
hurt are consecutive overlapping chunks the cap happens to allow). MMR's gain
comes at zero recall cost; n=12 single-judge-pass is modest evidence, which is
why MMR ships as an option, not the default. Source diversity is reported
separately from relevance throughout, as the numbers above show — the MMR gain
is on both axes, not a trade.

**Query keywordization** (live S2 probe, 7 of 8 dev questions): raw question →
0 available papers on 5 of 7; keyword form → candidates for 7 of 7, abstracts
1–4 per question. (The 8th question hit the unkeyed public rate limit; the
probe stands at n=7.)

**End-to-end**: a fresh `search_grounded` development run under the fixed
pipeline vs the stage-19 baseline — same questions, same depth (quick, k=4),
same relevance judge — is recorded in the stage-26 development log entry with
its artifact directory.

## 4. Trade-offs, honestly

- **Keywordization** costs nothing per call (same single request) but rewrites
  the provider query; the original and effective forms are both recorded. Risk:
  a content word dropped as a stopword on an unusual phrasing — bounded by the
  conservative trigger (question-shaped queries only) and the fallback.
- **Cross-channel dedup** trades a candidate slot: when the paper record and its
  PMC page collide, one of them is gone. The more-text rule favors quotable
  evidence over richer bibliographic metadata when the paper is metadata-only;
  the reverse when the abstract is fuller. Either way the run sees one document
  once, which is what source diversity claims to count.
- **MMR** adds one embedding query and an O(window·k) scoring pass (~ms at
  these pool sizes; latency not measurably affected). Its redundancy penalty
  could, on a pool with one genuinely dominant source, select a weaker passage —
  the measured dev pools show the opposite, but that is why it is a recorded
  option rather than the new default.
- **Candidate depth** (more web/academic results per query) was NOT raised:
  depth profiles are experiment constants, and raising them changes cost and
  arm definitions together. The measured bottlenecks (starvation, redundancy)
  were addressable without it.

## 5. Limitations and non-claims

- Relevance labels are LLM-judged against each item's `expected_evidence`
  (relevance-judge-v1, the benchmark's established mechanism, every pair stored
  and auditable). They are not human labels, and similarity never proves truth —
  the verification stage, not retrieval, judges support.
- The selection experiment is n=12 cells, one judge pass; the probe is n=7
  questions. Directionally consistent, not statistically established.
- Evidence is still provider snippets and abstracts — no full-text fetching
  exists in the pipeline. That stage (planned since 8b) remains the single
  biggest retrieval-quality lever and is untouched here.
- The hybrid arm's single-source concentration is documented, not fixed (§1f).
