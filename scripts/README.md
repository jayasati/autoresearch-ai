# scripts/

Developer convenience only. Nothing here is imported by the application.

- `dev_backend.sh` / `dev_backend.ps1` — start the API with reload
- `dev_frontend.sh` — start Vite
- `run_quality_pipeline.py` — run report-quality-v1 once on one question in an
  isolated temporary SQLite database and vector store. Preflight only (no provider
  calls) unless `--execute` is passed; `--execute` makes paid calls under the
  configured per-run budget and writes an artifact to `data/exports/quality-runs/`.
- `evaluate_quality_pair.py` — paired evaluation of one such artifact: the draft
  (verification off) vs the final report (verification on), blinded, against the
  frozen development pool. Dry run by default; `--export-annotations` /
  `--annotations` for human labels; `--execute --judge-model M` is paid. See
  `docs/VERIFICATION_COMPARISON_PROTOCOL.md`.

Planned later: `init_db.py`, `run_benchmark.py`, `export_report.py`.
