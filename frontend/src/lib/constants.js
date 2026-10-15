/**
 * The vocabulary the frontend shares with the backend.
 *
 * These values mirror `backend/app/core/constants.py`. They are duplicated here
 * rather than fetched because routing and labels need them before any request
 * resolves -- but the *values* must match the backend exactly, so the strings
 * below are the enum values, and the labels are presentation only.
 *
 * `GET /api/v1/system/capabilities` returns the authoritative mode list, and the
 * dashboard checks it against this file so a drift becomes visible instead of
 * silent.
 */

/** Mirrors ResearchMode. */
export const RESEARCH_MODES = [
  {
    value: 'model_only',
    label: 'Model only',
    summary: 'No retrieval. The model answers from parametric knowledge alone.',
    detail:
      'The baseline. We expect this configuration to hallucinate, and measuring that is the point.',
    sources: 'None',
  },
  {
    value: 'hybrid',
    label: 'Hybrid',
    summary: 'Model plus live web search.',
    detail: 'Tests whether web grounding measurably reduces unsupported claims.',
    sources: 'Web (Tavily)',
  },
  {
    value: 'search_grounded',
    label: 'Search grounded',
    summary: 'Model plus web, academic papers, and RAG over fetched full text.',
    detail: 'The full evidence pipeline runs on this configuration.',
    sources: 'Web + Semantic Scholar + RAG',
  },
]

/** Mirrors ResearchDepth. How much retrieval and synthesis a run pays for. */
export const RESEARCH_DEPTHS = [
  { value: 'quick', label: 'Quick', summary: 'Cheapest and fastest — good for trying it out.' },
  { value: 'medium', label: 'Medium', summary: 'The default balance of cost and coverage.' },
  { value: 'deep', label: 'Deep', summary: 'Most retrieval and synthesis; slowest and costliest.' },
]

/** Mirrors VerificationVerdict. */
export const VERIFICATION_VERDICTS = [
  { value: 'supported', label: 'Supported', tone: 'positive' },
  { value: 'partially_supported', label: 'Partially supported', tone: 'warning' },
  { value: 'unsupported', label: 'Unsupported', tone: 'negative' },
  { value: 'contradicted', label: 'Contradicted', tone: 'negative' },
  { value: 'not_enough_evidence', label: 'Not enough evidence', tone: 'neutral' },
]

/** Mirrors CitationStatus. */
export const CITATION_STATUSES = [
  { value: 'valid', label: 'Valid', tone: 'positive' },
  { value: 'broken', label: 'Broken link', tone: 'warning' },
  { value: 'misattributed', label: 'Misattributed', tone: 'negative' },
  { value: 'fabricated', label: 'Fabricated', tone: 'negative' },
]

/** Where the project currently is. Shown in the sidebar. */
export const CURRENT_STAGE = { number: 23, total: 23, name: 'complete — pipeline, evidence, benchmark' }
