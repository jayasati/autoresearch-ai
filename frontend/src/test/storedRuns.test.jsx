/**
 * The stored-run pages: Research Results, Sources, Evidence Audit, Evaluation
 * and the Dashboard's live widgets, driven by a stubbed backend with one run.
 *
 * Fixture values are visible placeholders in the builders' style — nothing here
 * could pass for real research output in a snapshot.
 */

import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'

import { renderApp, stubHealthyBackend } from './utils.jsx'

const RUN_ID = '7f3b2a90-0000-4000-8000-00000000000a'

const RUN_LIST = [
  {
    id: RUN_ID,
    topic: '<topic under test>',
    mode: 'search_grounded',
    status: 'completed',
    created_at: '2026-01-01T12:00:00Z',
    started_at: '2026-01-01T12:00:00Z',
    completed_at: '2026-01-01T12:00:30Z',
    error_code: null,
  },
]

const RUN_DETAIL = {
  run: {
    id: RUN_ID,
    topic: '<topic under test>',
    normalized_topic: '<topic under test>',
    status: 'completed',
    configuration_id: '7f3b2a90-0000-4000-8000-00000000000b',
    benchmark_run_id: null,
    started_at: '2026-01-01T12:00:00Z',
    completed_at: '2026-01-01T12:00:30Z',
    error_code: null,
    error_message: null,
    created_at: '2026-01-01T12:00:00Z',
    updated_at: '2026-01-01T12:00:30Z',
    configuration: {
      id: '7f3b2a90-0000-4000-8000-00000000000b',
      fingerprint: 'abc',
      mode: 'search_grounded',
      model: '<model under test>',
      temperature: 0,
      max_subquestions: 6,
      max_sources: 25,
      created_at: '2026-01-01T12:00:00Z',
    },
    subquestions: [
      { id: 'sq-1', position: 0, text: '<sub-question under test>', search_query: null, addressed: true },
    ],
  },
  report_id: 'rep-1',
  claim_count: 2,
  source_count: 1,
  conflict_count: 1,
}

const REPORT = {
  research_id: RUN_ID,
  report_id: 'rep-1',
  topic: '<topic under test>',
  title: '<report title under test>',
  markdown: '<draft report body under test>',
  final_markdown: '<final report body under test>',
  finalized: true,
  generated_at: '2026-01-01T12:00:20Z',
  finalized_at: '2026-01-01T12:00:30Z',
}

const SOURCES = [
  {
    source_id: 'src-1',
    label: 'S1',
    title: '<source title under test>',
    url: 'https://example.invalid/paper',
    doi: null,
    venue: '<venue under test>',
    year: 2025,
    source_type: 'academic',
    retriever: 'semantic_scholar',
    rank: 0,
    evidence_depth: 'full_text',
  },
  {
    source_id: 'src-2',
    label: null,
    title: '<unused source under test>',
    url: 'https://example.invalid/page',
    doi: null,
    venue: null,
    year: null,
    source_type: 'web',
    retriever: 'tavily',
    rank: 1,
    evidence_depth: 'snippet',
  },
]

const EVIDENCE = {
  research_id: RUN_ID,
  report_id: 'rep-1',
  topic: '<topic under test>',
  finalized: true,
  claims: [
    {
      claim_id: 'claim-1',
      text: '<supported claim under test>',
      claim_type: 'factual',
      importance: 'high',
      requires_citation: true,
      section: 'Findings',
      verdict: 'supported',
      confidence: 0.9,
      downgraded: false,
      rationale: '<rationale under test>',
      citations: { S1: 'valid' },
    },
    {
      claim_id: 'claim-2',
      text: '<contradicted claim under test>',
      claim_type: 'numeric',
      importance: 'medium',
      requires_citation: true,
      section: null,
      verdict: 'contradicted',
      confidence: 0.8,
      downgraded: false,
      rationale: null,
      citations: {},
    },
  ],
  conflicts: [
    {
      conflict_id: 'conflict-1',
      claim_id: 'claim-2',
      conflict_type: 'numeric_disagreement',
      description: '<conflict description under test>',
    },
  ],
  sources: [
    {
      label: 'S1',
      title: '<source title under test>',
      url: 'https://example.invalid/paper',
      doi: null,
      venue: '<venue under test>',
      year: 2025,
      source_type: 'academic',
    },
  ],
}

const BENCHMARK = {
  set_name: 'test',
  source_dir: '20260101T000000Z',
  experiment: { benchmark_run_id: '<benchmark id under test>' },
  comparison: [
    {
      metric: 'claim_support_rate',
      higher_is_better: true,
      baseline_arm: 'model_only',
      baseline: 0.1,
      autoresearch_arm: 'full_autoresearch_verified',
      autoresearch: 0.9,
      absolute_difference: 0.8,
      relative_difference: null,
      direction: 'improved',
    },
  ],
  aggregates: {},
  external: {
    primary_matched: {
      baseline_arm: 'model_only',
      system_arm: 'full_autoresearch_verified',
      n: 6,
      matched_questions: ['q1'],
      exclusions: [],
      baseline: { per_question: { q1: 0.4554 }, macro: 0.4554, micro: 0.4312 },
      system: { per_question: { q1: 0.6675 }, macro: 0.6675, micro: 0.6769 },
      macro_difference_system_minus_baseline: 0.2121,
    },
    aggregates_v2: {
      model_only: {
        eligible_questions: 7,
        external_support_rate_macro: 0.476,
        external_contradiction_rate_macro: 0.048,
      },
    },
    provenance: { benchmark_run_id: '<benchmark id under test>' },
  },
}

function stubPopulatedBackend() {
  return stubHealthyBackend({
    '/api/v1/research': RUN_LIST,
    [`/api/v1/research/${RUN_ID}`]: RUN_DETAIL,
    [`/api/v1/research/${RUN_ID}/report`]: REPORT,
    [`/api/v1/research/${RUN_ID}/sources`]: SOURCES,
    [`/api/v1/research/${RUN_ID}/evidence`]: EVIDENCE,
    '/api/v1/evaluation/benchmark': BENCHMARK,
  })
}

beforeEach(() => {
  stubPopulatedBackend()
})

describe('Research Results', () => {
  it('auto-selects the newest run and shows its header and counts', async () => {
    renderApp('/research/results')
    expect(await screen.findByRole('heading', { name: '<topic under test>' })).toBeInTheDocument()
    expect(screen.getByText('search_grounded')).toBeInTheDocument()
    expect(screen.getByText('Claims')).toBeInTheDocument()
  })

  it('shows the final report when the run was finalized, labelled as such', async () => {
    renderApp('/research/results')
    expect(await screen.findByText('<final report body under test>')).toBeInTheDocument()
    expect(screen.getByText('final report')).toBeInTheDocument()
    expect(screen.queryByText('<draft report body under test>')).not.toBeInTheDocument()
  })
})

describe('Sources', () => {
  it('lists every retrieved source, labelled or not, with its evidence depth', async () => {
    renderApp('/sources')
    expect(await screen.findByText('<source title under test>')).toBeInTheDocument()
    expect(screen.getAllByText('[S1]').length).toBeGreaterThan(0)
    expect(screen.getByText('<unused source under test>')).toBeInTheDocument()
    expect(screen.getByText('full_text')).toBeInTheDocument()
    expect(screen.getByText('snippet')).toBeInTheDocument()
  })
})

describe('Evidence Audit', () => {
  it('shows each claim with its verdict and citation outcomes', async () => {
    renderApp('/evidence')
    expect(await screen.findByText('<supported claim under test>')).toBeInTheDocument()
    expect(screen.getByText('<contradicted claim under test>')).toBeInTheDocument()
    expect(screen.getAllByText('Supported').length).toBeGreaterThan(0)
    expect(screen.getByText(/\[S1\] valid/)).toBeInTheDocument()
  })

  it('shows stored conflicts with their descriptions', async () => {
    renderApp('/evidence')
    expect(await screen.findByText('<conflict description under test>')).toBeInTheDocument()
    expect(screen.getByText('numeric_disagreement')).toBeInTheDocument()
  })
})

describe('Evaluation', () => {
  it('shows the primary matched result from the artifact, verbatim', async () => {
    renderApp('/evaluation')
    expect(await screen.findByText('0.2121')).toBeInTheDocument()
    // The macro values appear in the stat tiles and again per question.
    expect(screen.getAllByText('0.4554').length).toBeGreaterThan(0)
    expect(screen.getAllByText('0.6675').length).toBeGreaterThan(0)
  })

  it('shows the v1 comparison rows and the reading caveats', async () => {
    renderApp('/evaluation')
    expect(await screen.findByText('Claim support rate')).toBeInTheDocument()
    expect(screen.getByText('improved')).toBeInTheDocument()
    expect(screen.getByText(/supportability, not accuracy/i)).toBeInTheDocument()
  })
})

describe('Dashboard', () => {
  it('counts stored runs and shows the benchmark headline', async () => {
    renderApp('/')
    expect(await screen.findByText('+0.212')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
  })
})
