import { Link } from 'react-router-dom'

import { Card, PageHeader } from '../components/ui/primitives.jsx'

export default function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" description="That route does not exist in this build." />
      <Card>
        <p className="note">
          Use the sidebar, or go back to the{' '}
          <Link className="link" to="/">
            dashboard
          </Link>
          .
        </p>
      </Card>
    </>
  )
}
