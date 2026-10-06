/**
 * Sources.
 *
 * Every web page and paper a run retrieved. The table headers are real -- they are
 * the columns the backend will need to provide -- but there are no rows, because
 * no retrieval has ever happened.
 */

import { Card, PageHeader, PlannedContents } from '../components/ui/primitives.jsx'
import { EmptyState, NotImplemented } from '../components/ui/states.jsx'
import { STAGES } from '../lib/constants.js'

const COLUMNS = ['Title', 'Type', 'Venue / domain', 'Year', 'Evidence depth', 'Used by claims']

export default function SourcesPage() {
  return (
    <>
      <PageHeader
        title="Sources"
        description="Every web page and paper retrieved, and how deeply each one was read."
      />

      <Card title="Retrieved sources">
        <table className="table">
          <thead>
            <tr>
              {COLUMNS.map((column) => (
                <th key={column} scope="col">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <td colSpan={COLUMNS.length} className="table__empty">
                <EmptyState
                  title="No sources"
                  message="No run has retrieved anything. Web search, academic search and page fetching are not implemented yet."
                />
              </td>
            </tr>
          </tbody>
        </table>
      </Card>

      <Card title="Retrieval">
        <NotImplemented feature="Source retrieval" stage={STAGES.RETRIEVAL}>
          <PlannedContents
            items={[
              { label: 'Tavily', detail: 'web search returning clean, LLM-ready results' },
              { label: 'Semantic Scholar', detail: 'papers, abstracts, venues, citation counts' },
              { label: 'Fetcher', detail: 'URL to clean text, with timeouts and politeness' },
              {
                label: 'Chunker',
                detail: 'overlapping chunks that keep their character offsets',
              },
              { label: 'Embeddings', detail: 'Sentence Transformers, run locally' },
              { label: 'Vector store', detail: 'ChromaDB, persisted under data/vectorstore' },
            ]}
          />
        </NotImplemented>
      </Card>

      <Card title="Why evidence depth is a column">
        <p className="note">
          Many papers yield only an abstract. A claim grounded in a full text and a claim grounded in
          an abstract are not equally well supported, so each source records how deeply it was
          actually read — otherwise the groundedness metrics would overstate the result.
        </p>
      </Card>
    </>
  )
}
