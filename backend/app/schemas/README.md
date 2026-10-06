# schemas/ — Pydantic request/response models

**Implemented.** The API contract, kept separate from the ORM models so the database
schema can change without breaking clients.

| Module | Contents |
|---|---|
| `base.py` | `ReadModel`, `WriteModel`, `IdentifiedModel`, `TimestampedModel` |
| `common.py` | error envelope, health, service info |
| `research.py` | `ResearchRequest`, `ResearchResponse`, run and configuration reads |
| `source.py` | source, document and chunk create/read |
| `evidence.py` | claim, evidence, citation, verification, conflict |
| `evaluation.py` | metric create/read, `BenchmarkComparison` |
| `trace.py` | the three traceability chains as response models |

Two different defaults, on purpose: `WriteModel` sets `extra="forbid"` so a
misspelled field is rejected rather than silently defaulted, while `ReadModel` is
built from ORM rows.

The validators are not formalities — several encode decisions recorded in
ARCHITECTURE.md and ADR 0001. See DATA_MODEL.md §5 for which invariants are enforced
here, which are enforced by database constraints, and which are enforced in both.
