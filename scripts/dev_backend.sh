#!/usr/bin/env bash
# Start the backend with auto-reload. Run from the repository root.
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec uvicorn app.main:app --reload --port 8000
