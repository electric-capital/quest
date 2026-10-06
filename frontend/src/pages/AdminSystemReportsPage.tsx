/**
 * AdminSystemReportsPage - admin-only operator dashboard ("System Reports").
 *
 * Shell page with a left-hand section nav; each section is a self-contained
 * component, so adding a new report is one entry in SECTIONS plus its
 * component. Section choice is local state (the AdminOpsMenu always enters
 * at the default section; deep-linking per section hasn't been needed).
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { LatestActiveConversationsTable } from '../components/LatestActiveConversationsTable';
import { TotalUsageReport } from '../components/TotalUsageReport';
import { CostAnalysisTable } from '../components/CostAnalysisTable';
import { UsersReportTable } from '../components/UsersReportTable';
import { ModelsReportTable } from '../components/ModelsReportTable';
import { GuidesReportTable } from '../components/GuidesReportTable';
import { AdminOpsMenu } from '../components/AdminOpsMenu';
import './AdminSystemReportsPage.css';

const SECTIONS = [
  {
    key: 'latest',
    label: 'Latest Conversations',
    component: LatestActiveConversationsTable,
  },
  {
    key: 'usage',
    label: 'Total Usage',
    component: TotalUsageReport,
  },
  {
    key: 'costs',
    label: 'Cost Analysis',
    component: CostAnalysisTable,
  },
  {
    key: 'users',
    label: 'Users',
    component: UsersReportTable,
  },
  {
    key: 'models',
    label: 'Models',
    component: ModelsReportTable,
  },
  {
    key: 'guides',
    label: 'Guides',
    component: GuidesReportTable,
  },
] as const;

type SectionKey = (typeof SECTIONS)[number]['key'];

export function AdminSystemReportsPage() {
  const { isAdmin } = useAuth();
  const [sectionKey, setSectionKey] = useState<SectionKey>('latest');

  if (!isAdmin) {
    return (
      <div className="admin-system-reports-page">
        <div className="admin-system-reports-unauthorized">
          <div>Not authorized.</div>
          <Link to="/">Back to chats</Link>
        </div>
        <AdminOpsMenu />
      </div>
    );
  }

  const section = SECTIONS.find((s) => s.key === sectionKey) ?? SECTIONS[0];
  const SectionComponent = section.component;

  return (
    <div className="admin-system-reports-page">
      <div className="admin-system-reports-header">
        <Link to="/" className="back-link">&larr; Back</Link>
        <h1>System Reports</h1>
      </div>
      <div className="admin-system-reports-body">
        <nav className="admin-system-reports-nav" aria-label="Report sections">
          {SECTIONS.map((s) => (
            <button
              key={s.key}
              type="button"
              className={`admin-system-reports-nav-item${
                s.key === sectionKey ? ' active' : ''
              }`}
              onClick={() => setSectionKey(s.key)}
            >
              {s.label}
            </button>
          ))}
        </nav>
        <div className="admin-system-reports-content">
          <section>
            <SectionComponent />
          </section>
        </div>
      </div>
      <AdminOpsMenu />
    </div>
  );
}
