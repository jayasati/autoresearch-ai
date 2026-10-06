# frontend/ — React + Vite

Plain JavaScript, not TypeScript. The project started in JS, and "consistently" is
the requirement — a half-migrated codebase would be worse than either choice. API
response shapes are documented as JSDoc typedefs in `src/types/api.js`, which
editors use for completion without a build step.

## Layout

```
src/
├── main.jsx              mounts React and the BrowserRouter
├── App.jsx               route definitions, built from routes.js
├── routes.js             the route table — drives the router AND the sidebar
├── api/
│   ├── client.js         HTTP transport: base URL, error envelope, ApiError
│   └── endpoints.js      named operations; only ones the backend actually serves
├── hooks/
│   ├── useApi.js         loading / error / data / reload, modelled once
│   └── useBackendStatus.js
├── components/
│   ├── layout/AppLayout.jsx   sidebar, header, backend status badge
│   └── ui/
│       ├── states.jsx         LoadingState, ErrorState, EmptyState,
│       │                      NotImplemented, AsyncBoundary
│       └── primitives.jsx     Card, PageHeader, Badge, Button, StatTile
├── pages/                Dashboard, NewResearch, ResearchResults, Sources,
│                         EvidenceAudit, Evaluation, NotFound
├── lib/constants.js      the vocabulary shared with the backend enums
├── types/api.js          JSDoc typedefs for the API contract
├── styles/index.css      design tokens; dark and light via prefers-color-scheme
└── test/                 Vitest + Testing Library
```

### Why one route table

`routes.js` is the single source for both the router and the navigation, so a page
cannot end up routable but unreachable, or listed in the sidebar but broken.

### Why components never call `fetch`

Pages call named operations from `api/endpoints.js`, which call `api/client.js`.
The base URL, the error contract and the correlation header are each handled in
exactly one place.

## Commands

```bash
npm install
npm run dev          # http://localhost:5173
npm test             # Vitest, 67 tests
npm run lint         # ESLint
npm run build        # production bundle into dist/
```

The dev server proxies `/api` to `http://localhost:8000`, so the browser only ever
talks to the Vite origin and CORS never applies in development. For a deployed
build, set `VITE_API_BASE` to the backend's origin. Only `VITE_*` variables reach
the browser — the prefix is a deliberate signal that the value is public.

## Status

**No research functionality.** The Dashboard reads real data from
`GET /api/v1/system/capabilities`; every other page is an explicit placeholder that
names the stage its feature arrives in. No page displays sample or mock research
output — see DEVELOPMENT_LOG for why that rule matters for this project.
