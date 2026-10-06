# frontend/ — React + Vite

- `src/api/` — the only layer that calls the backend
- `src/components/` — presentational, reusable
- `src/features/` — one folder per feature (research-run, report, claims, benchmark)
- `src/pages/` — route-level screens
- `src/hooks/`, `src/lib/`, `src/types/` — shared logic and type defs
- `src/styles/` — global CSS

Plain JavaScript for now; `src/types/` is reserved in case the project moves to
TypeScript before the data model gets large.
