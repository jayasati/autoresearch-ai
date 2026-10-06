/**
 * Route definitions.
 *
 * Built from `NAV_ROUTES` rather than listed again here, so a page cannot end up
 * routable but missing from the sidebar, or listed but broken.
 *
 * The router itself is mounted in `main.jsx`, not here: tests wrap `<App />` in a
 * MemoryRouter to drive navigation, which a BrowserRouter inside App would
 * prevent.
 */

import { Route, Routes } from 'react-router-dom'

import AppLayout from './components/layout/AppLayout.jsx'
import NotFoundPage from './pages/NotFoundPage.jsx'
import { NAV_ROUTES } from './routes.js'

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        {NAV_ROUTES.map(({ path, element: Page }) => (
          <Route key={path} path={path} element={<Page />} />
        ))}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
