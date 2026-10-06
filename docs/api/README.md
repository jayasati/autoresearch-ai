# API notes

The authoritative, always-current spec is the running backend's OpenAPI page:
http://localhost:8000/docs

This directory holds what OpenAPI cannot express: example payloads, the
status-transition contract, and streaming/polling conventions for long runs.

## Current endpoints (stage 1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + version |
| GET | `/api/v1/system/ping` | trivial reachability check |
| GET | `/api/v1/system/capabilities` | which integrations have credentials; which stages have landed |
