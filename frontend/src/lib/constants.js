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

/**
 * Which build stage each part of the system arrives in.
 *
 * Every placeholder in the UI cites one of these, so "not implemented" always
 * comes with a specific answer to "when".
 */
export const STAGES = {
  DATA_MODEL: 'stage 4 — data model and run lifecycle',
  MODEL_ONLY: 'stage 5 — model-only pipeline',
  RETRIEVAL: 'stage 6 — retrieval (web, academic, RAG)',
  EVIDENCE: 'stage 7 — evidence layer',
  EVALUATION: 'stage 8 — evaluation and benchmark',
}

/** Where the project currently is. Shown in the sidebar and on the dashboard. */
export const CURRENT_STAGE = { number: 3, total: 9, name: 'frontend foundation' }
