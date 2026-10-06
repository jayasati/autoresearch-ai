# Start the backend with auto-reload. Run from anywhere.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\backend")
uvicorn app.main:app --reload --port 8000
