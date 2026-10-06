/**
 * The route table.
 *
 * One list drives both the router and the sidebar, so a new page cannot end up
 * routable but unreachable, or listed but broken.
 */

import {
  BarChart3,
  FileSearch,
  LayoutDashboard,
  Library,
  PlusCircle,
  ShieldCheck,
} from 'lucide-react'

import DashboardPage from './pages/DashboardPage.jsx'
import EvaluationPage from './pages/EvaluationPage.jsx'
import EvidenceAuditPage from './pages/EvidenceAuditPage.jsx'
import NewResearchPage from './pages/NewResearchPage.jsx'
import ResearchResultsPage from './pages/ResearchResultsPage.jsx'
import SourcesPage from './pages/SourcesPage.jsx'

export const NAV_ROUTES = [
  {
    path: '/',
    label: 'Dashboard',
    icon: LayoutDashboard,
    element: DashboardPage,
    description: 'System status and what is built so far',
  },
  {
    path: '/research/new',
    label: 'New Research',
    icon: PlusCircle,
    element: NewResearchPage,
    description: 'Choose a topic and a research configuration',
  },
  {
    path: '/research/results',
    label: 'Research Results',
    icon: FileSearch,
    element: ResearchResultsPage,
    description: 'Generated reports with inline citations',
  },
  {
    path: '/sources',
    label: 'Sources',
    icon: Library,
    element: SourcesPage,
    description: 'Every web page and paper a run retrieved',
  },
  {
    path: '/evidence',
    label: 'Evidence Audit',
    icon: ShieldCheck,
    element: EvidenceAuditPage,
    description: 'Claim-level verification, citations and conflicts',
  },
  {
    path: '/evaluation',
    label: 'Evaluation',
    icon: BarChart3,
    element: EvaluationPage,
    description: 'Quality metrics and the benchmark comparison',
  },
]
