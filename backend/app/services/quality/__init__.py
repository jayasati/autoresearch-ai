"""
Report-quality pipeline v1 (docs/REPORT_QUALITY_PIPELINE.md) — stage S1.

Deterministic, independently testable foundations only. No module here calls a
model, a search provider or the database, and nothing in the legacy pipeline
imports this package; the pipeline cannot run until later stages wire it behind
`REPORT_QUALITY_ENABLED` (off by default).

| Module | Contents |
|---|---|
| `models.py` | typed shapes: model-filled report structures, alias table, statuses, findings |
| `quantities.py` | `quant-audit-v1`: quantity extraction, six-rule pair classifier (audit-only) |
| `alignment.py` | deterministic claim → draft-sentence alignment (decision D2) |
| `assessment.py` | claim status (design §3), subquestion assessment, conflict context |
| `validation.py` | final-report checks V1–V7 and the complete / degraded / failed outcome |
| `rendering.py` | structured report → markdown; markers from the alias table only |

Every threshold these modules take is a provisional setting
(`REPORT_QUALITY_*`), to be tuned on development data only.
"""
