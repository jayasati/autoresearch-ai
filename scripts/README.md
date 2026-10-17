# scripts/

Developer convenience only. Nothing here is imported by the application.

- `dev_backend.sh` / `dev_backend.ps1` — start the API with reload
- `dev_frontend.sh` — start Vite
- `run_quality_pipeline.py` — run report-quality-v1 once on one question in an
  isolated temporary SQLite database and vector store. Preflight only (no provider
  calls) unless `--execute` is passed; `--execute` makes paid calls under the
  configured per-run budget and writes an artifact to `data/exports/quality-runs/`.

Planned later: `init_db.py`, `run_benchmark.py`, `export_report.py`.
