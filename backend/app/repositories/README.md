# repositories/ — Queries, one entity type each

The rule that defines this layer: **repositories never commit.**

They stage inserts and call `flush()` where a database check must happen before the
next statement, but the transaction belongs to whoever opened the unit of work.

Why it matters: a research run produces a connected graph — a report, its claims,
each claim's evidence, citations and verdict. If repositories committed, a failure
partway through would leave the earlier claims written, producing a run whose claim
set is silently incomplete. Every metric computed over it would then be wrong in a
way that looks like a finding rather than a bug.

A `flush()` is not a commit. It sends the INSERT so constraints are checked and
relationships resolve, while the transaction stays rollback-able.

| Module | Repositories |
|---|---|
| `base.py` | `Repository` — get, get_or_raise, list (always paginated), count, add, flush, delete |
| `research.py` | configuration, source, run, benchmark |
| `evidence.py` | report, document, chunk, claim, evidence, citation, verification, conflict |
| `evaluation.py` | metric results, LLM call log |

Two methods earn their place most clearly: `get_or_create` on configurations and
sources. Both implement deduplication by content fingerprint, which several metrics
depend on and which is easy to get subtly wrong when written inline in a handler.

`ClaimRepository.get_trace` and `get_verification_trace` materialise two of the
traceability chains in a fixed number of round trips. Reconstructed ad hoc those
chains are both slow (an N+1 per claim) and easy to get subtly wrong — and a wrong
provenance chain is worse than none, because it still looks authoritative.
