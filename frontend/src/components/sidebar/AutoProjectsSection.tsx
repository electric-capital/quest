/**
 * The Sidebar "AutoProjects" block (main panel only, between Projects and Docs;
 * AutoProjects are top-level, so the drill-down has none): the user's unarchived
 * AutoProjects with the newest activity first. The whole header is a button that
 * opens the AutoProjects list, with a "+" beside it for New AutoProject; a row opens that
 * AutoProject's page. Each row carries the unread dot and one status slot: a
 * working spinner while a run is running, "Needs you" while one waits on the
 * user, otherwise the last-activity time. A quiet footer links to the full
 * list (when there are more AutoProjects than rows) and to the archived list.
 *
 * Data comes from useAutoProjects (owned by the Sidebar, which renders this block
 * only while the `autoprojects` feature gate is open) and row order from
 * utils/sidebarAutoProjects; this component only renders it. Styling reuses the
 * docs block's classes (Sidebar.css), so the blocks read alike; the
 * `autoprojects-section` / `autoproject-row` classes identify this one.
 */

import { useMemo } from 'react';
import { ListChecks, LoaderCircle } from 'lucide-react';
import type { AutoProjectListRow } from '../../api/types';
import { useAutoProjectUnread } from '../../hooks/useAutoProjectUnread';
import { deriveSidebarAutoProjectItems, SIDEBAR_AUTOPROJECT_LIMIT } from '../../utils/sidebarAutoProjects';
import { formatSidebarDocTime, formatSidebarDocTimeTitle } from '../../utils/sidebarDocs';
import { ChevronRightIcon, PlusIcon } from './icons';

export const EMPTY_HINT = 'No AutoProjects yet';

export interface AutoProjectsSectionProps {
  // The unarchived AutoProjects (useAutoProjects().autoprojects), in any order.
  autoprojects: AutoProjectListRow[];
  // The first load has not landed yet.
  loading: boolean;
  // useAutoProjects().error: the first load failed (no AutoProjects to show).
  error?: string | null;
  // The AutoProject whose page is showing (with or without a run open), so its row is highlighted.
  activeAutoProjectId: string | null;
  onOpenAutoProject: (id: string) => void;
  // The unarchived AutoProjects list (the header and "Show all").
  onOpenAll: () => void;
  onOpenArchived: () => void;
  onNewAutoProject: () => void;
  // The unarchived AutoProjects list is the view showing.
  headerActive?: boolean;
  // The archived AutoProjects list is the view showing.
  archivedActive?: boolean;
}

function AutoProjectRowStatus({ autoproject }: { autoproject: AutoProjectListRow }) {
  const status = autoproject.active_run?.status;
  if (status === 'running') {
    return (
      <span className="autoproject-row-status autoproject-row-working" role="img" aria-label="Working" title="Working">
        <LoaderCircle size={12} aria-hidden="true" />
      </span>
    );
  }
  if (status === 'waiting') {
    return (
      <span className="autoproject-row-status autoproject-row-needs-you" title="Waiting for you">
        Needs you
      </span>
    );
  }
  if (!autoproject.last_activity_at) return null;
  return (
    <span className="doc-row-time" title={formatSidebarDocTimeTitle(autoproject.last_activity_at)}>
      {formatSidebarDocTime(autoproject.last_activity_at)}
    </span>
  );
}

function AutoProjectRow({
  autoproject,
  active,
  onOpen,
}: {
  autoproject: AutoProjectListRow;
  active: boolean;
  onOpen: () => void;
}) {
  const unread = useAutoProjectUnread(autoproject);
  return (
    <button
      type="button"
      className={`conversation-item doc-row autoproject-row${active ? ' active' : ''}`}
      onClick={onOpen}
      title={autoproject.title}
      aria-current={active ? 'page' : undefined}
    >
      <ListChecks size={14} className="doc-row-icon" aria-hidden="true" />
      <span className="doc-row-title">
        {unread && (
          <span className="conversation-unread-dot" role="img" aria-label="Unread" />
        )}
        {autoproject.title}
      </span>
      <AutoProjectRowStatus autoproject={autoproject} />
    </button>
  );
}

export function AutoProjectsSection({
  autoprojects,
  loading,
  error = null,
  activeAutoProjectId,
  onOpenAutoProject,
  onOpenAll,
  onOpenArchived,
  onNewAutoProject,
  headerActive = false,
  archivedActive = false,
}: AutoProjectsSectionProps) {
  const items = useMemo(() => deriveSidebarAutoProjectItems(autoprojects), [autoprojects]);
  // A failed load is not "no AutoProjects": no empty hint.
  const failed = !loading && Boolean(error) && autoprojects.length === 0;

  return (
    <div className="docs-section autoprojects-section">
      <div className="section-header docs-section-header autoprojects-section-header">
        <button
          type="button"
          className={`docs-section-header-button${headerActive ? ' active' : ''}`}
          onClick={onOpenAll}
          title="All AutoProjects"
          aria-current={headerActive ? 'page' : undefined}
        >
          <span className="section-label">AutoProjects</span>
          <ChevronRightIcon className="docs-section-chevron" />
        </button>
        <div className="section-header-actions">
          <button
            type="button"
            className="section-add-button"
            onClick={onNewAutoProject}
            title="New AutoProject"
            aria-label="New AutoProject"
          >
            <PlusIcon />
          </button>
        </div>
      </div>

      {items.map((autoproject) => (
        <AutoProjectRow
          key={autoproject.id}
          autoproject={autoproject}
          active={activeAutoProjectId === autoproject.id}
          onOpen={() => onOpenAutoProject(autoproject.id)}
        />
      ))}

      {/* Quiet while the first load is pending: no "Loading..." line that
          would flash before the rows or the empty state. */}
      {failed ? (
        <div className="sidebar-empty">Couldn't load AutoProjects</div>
      ) : (
        !loading && autoprojects.length === 0 && (
          <div className="sidebar-empty">{EMPTY_HINT}</div>
        )
      )}

      {!loading && (
        <div className="autoprojects-section-footer">
          {autoprojects.length > SIDEBAR_AUTOPROJECT_LIMIT && (
            <button type="button" className="autoprojects-section-link" onClick={onOpenAll}>
              Show all ({autoprojects.length})
            </button>
          )}
          <button
            type="button"
            className={`autoprojects-section-link${archivedActive ? ' active' : ''}`}
            onClick={onOpenArchived}
            aria-current={archivedActive ? 'page' : undefined}
          >
            Archived
          </button>
        </div>
      )}
    </div>
  );
}
