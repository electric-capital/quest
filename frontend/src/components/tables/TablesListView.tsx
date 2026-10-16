/**
 * All Tables: the main-pane takeover at /tables and /tables?project=<id>
 * (spec 8.1), a copy of DocsListView. Rendered by App / MobileShell inside
 * `.main-content.docs-main.tables-main`, the scrolling column; the header
 * sticks to its top.
 *
 * Data. The backend has no cross-project table list, so:
 *   - unfiltered: "Your tables" = useTables({limit: 50}) (the user's own
 *     user tables, server-paged, "Load more"); "Shared with you" =
 *     useTables({shared: true, limit: 50}) (tables other people shared, user
 *     and project tables, its own keyset stream and "Load more"; hidden while
 *     empty unless it failed); plus one group per project with tables --
 *     archived projects included and marked -- from useProjectTablesIndex
 *     (one listTables per project, up to 200 tables each);
 *   - filtered (?project=<id>): useTables({projectId, limit: 50}) only (no
 *     shared group).
 * Every hook always runs (the shared list and the index are simply disabled
 * while filtered, the index also until the project list has loaded).
 * Grouping, ordering (newest-updated first), dedupe and the scope / count
 * cells are the pure helpers in utils/allTablesGrouping.ts. Columns: title /
 * mode / scope / rows / columns / updated. The mode badge follows
 * utils/docMode (DocModeBadge): only a public table shows one, and the "Mode"
 * column label is shown only while some rendered row is public.
 *
 * New Table opens NewTableDialog (title, description, location preselected
 * to the filtered project, and the column editor), which creates the table
 * and navigates to it; closed without creating, it returns focus to the New
 * Table button. A host may pass `onNewTable` to open something else
 * instead; then the view renders no dialog of its own.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ChevronLeft, Folder, Globe, Plus, Users } from 'lucide-react';
import type { Project, TableListRow } from '../../api/types';
import { useProjects } from '../../contexts/ProjectsContext';
import { useTables, type TablesList } from '../../hooks/useTables';
import { useProjectTablesIndex } from '../../hooks/useProjectTablesIndex';
import {
  SHARED_TABLES_GROUP_KEY,
  USER_TABLES_GROUP_KEY,
  formatColumnCount,
  formatRowCount,
  formatTableCount,
  groupTables,
  projectTablesGroupKey,
  tableScopeLabel,
  tableScopeTitle,
  type TableGroup,
} from '../../utils/allTablesGrouping';
import { shouldShowDocModeBadge } from '../../utils/docMode';
import { tableViewerPath, tablesListPath } from '../../utils/tablesRoute';
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters';
import { DocModeBadge } from '../docs/DocModeBadge';
import { NewTableDialog } from './NewTableDialog';
import './TablesListView.css';

/** Page size of the server-paged lists ("Your tables" / "Shared with you",
 * or the filtered project). */
const PAGE_SIZE = 50;

const PUBLIC_PROJECT_TITLE = 'Public project — internet access, no internal data';

export interface TablesListViewProps {
  /** The `?project=<id>` filter; null = all tables. */
  projectId: string | null;
  /**
   * Overrides the New Table button; without it the view opens its own
   * NewTableDialog.
   */
  onNewTable?: () => void;
}

export function TablesListView({ projectId, onNewTable }: TablesListViewProps) {
  const { projects, projectsLoaded } = useProjects();
  const filtered = projectId !== null;
  const [newTableOpen, setNewTableOpen] = useState(false);
  const openNewTable = onNewTable ?? (() => setNewTableOpen(true));
  // The dialog closed (without navigating away): focus back on its opener,
  // unless focus already went somewhere else on the page.
  const newButtonRef = useRef<HTMLButtonElement>(null);
  const [dialogClosed, setDialogClosed] = useState(0);
  const closeNewTable = () => {
    setNewTableOpen(false);
    setDialogClosed((n) => n + 1);
  };
  useEffect(() => {
    if (dialogClosed === 0) return;
    const active = document.activeElement;
    if (!active || active === document.body || !active.isConnected) newButtonRef.current?.focus();
  }, [dialogClosed]);

  // One paged list (the user's tables, or the filtered project's) plus the
  // shared list and the per-project index, which only the unfiltered view
  // needs. The index waits for the project list: fanning out over the empty
  // pre-load list would "load" an index with no projects in it.
  const list = useTables({ projectId, limit: PAGE_SIZE });
  const shared = useTables({ shared: true, limit: PAGE_SIZE, enabled: !filtered });
  const index = useProjectTablesIndex(projects, !filtered && projectsLoaded);

  const projectsById = useMemo(() => new Map(projects.map((p) => [p.id, p])), [projects]);
  const filterProject = projectId ? (projectsById.get(projectId) ?? null) : null;

  // The group the paged list feeds; it owns the "Load more" footer.
  const pagedGroupKey = projectId ? projectTablesGroupKey(projectId) : USER_TABLES_GROUP_KEY;

  const groups = useMemo<TableGroup[]>(
    () =>
      groupTables({
        projectId,
        userTables: list.tables,
        projectTables: list.tables,
        byProject: index.byProject,
        projects,
        sharedTables: shared.tables,
      }),
    [projectId, list.tables, index.byProject, projects, shared.tables],
  );

  // The paged group stays (empty line + "Load more") unless it failed; an
  // empty "Shared with you" shows only to report its error.
  const visibleGroups = groups.filter((group) => {
    if (group.tables.length > 0) return true;
    if (group.key === SHARED_TABLES_GROUP_KEY) return shared.error !== null;
    return !list.error && group.key === pagedGroupKey;
  });

  const totalTables = groups.reduce((n, group) => n + group.tables.length, 0);
  const anyModeBadge = visibleGroups.some((group) =>
    group.tables.some((table) => shouldShowDocModeBadge(table.mode)),
  );

  // Wait for the first page, the shared list's first page AND the project
  // index (which itself waits for the project list) before deciding between
  // rows and the empty state, so a user whose tables are all project tables
  // or all shared ones never sees "No tables yet." flash. Initial load only:
  // a project-set change keeps the previous groups on screen meanwhile.
  const loading = list.loading
    || (!filtered && (shared.loading || !projectsLoaded || (index.loading && !index.loaded)));

  const streamFor = (group: TableGroup): TablesList | null => {
    if (group.key === SHARED_TABLES_GROUP_KEY) return shared;
    return group.key === pagedGroupKey ? list : null;
  };

  let body: ReactNode;
  if (loading) {
    body = <div className="tables-list-status">Loading...</div>;
  } else if (totalTables === 0 && !list.error && !shared.error) {
    body = (
      <div className="tables-list-empty">
        <p className="tables-list-empty-title">
          {filtered ? 'No tables in this project yet.' : 'No tables yet.'}
        </p>
        <p className="tables-list-empty-hint">
          Ask Quest to create one, or use New Table.
        </p>
      </div>
    );
  } else {
    body = (
      <>
        {list.error && (
          <div className="tables-list-error" role="alert">
            {list.error}
          </div>
        )}
        {visibleGroups.length > 0 && (
          <>
            <div className="tables-columns" aria-hidden="true">
              <span>Title</span>
              <span>{anyModeBadge ? 'Mode' : ''}</span>
              <span className="tables-col-scope">Scope</span>
              <span className="tables-col-num">Rows</span>
              <span className="tables-col-num tables-col-columns">Columns</span>
              <span>Updated</span>
            </div>
            {visibleGroups.map((group) => {
              const stream = streamFor(group);
              return (
                <section
                  key={group.key}
                  className="tables-group"
                  aria-label={groupAriaLabel(group, filtered)}
                >
                  <GroupHeading
                    group={group}
                    filtered={filtered}
                    countSuffix={stream?.hasMore ? '+' : ''}
                  />
                  {group.tables.length > 0 ? (
                    <div className="tables-rows">
                      {group.tables.map((table) => (
                        <TableRowLink
                          key={table.id}
                          table={table}
                          project={table.project_id ? (projectsById.get(table.project_id) ?? null) : null}
                        />
                      ))}
                    </div>
                  ) : group.key === SHARED_TABLES_GROUP_KEY ? (
                    // Shown empty only when its first page failed.
                    <div className="tables-group-error" role="alert">
                      {shared.error}
                    </div>
                  ) : (
                    <div className="tables-group-empty">
                      {filtered ? 'No tables in this project yet.' : 'No tables of your own yet.'}
                    </div>
                  )}
                  {stream?.hasMore && (
                    <div className="tables-group-footer">
                      <button
                        type="button"
                        className="tables-load-more"
                        onClick={stream.loadMore}
                        disabled={stream.loadingMore}
                      >
                        {stream.loadingMore ? 'Loading...' : 'Load more'}
                      </button>
                    </div>
                  )}
                </section>
              );
            })}
          </>
        )}
      </>
    );
  }

  return (
    <div className="tables-list">
      <header className="tables-list-header">
        <div className="tables-list-header-inner">
          <div className="tables-list-heading">
            {filtered && (
              <Link to={tablesListPath()} className="tables-list-back">
                <ChevronLeft size={16} aria-hidden="true" />
                All tables
              </Link>
            )}
            <h2 className="tables-list-title">
              {filtered ? (
                <>
                  <Folder size={18} className="tables-list-title-icon" aria-hidden="true" />
                  <span className="tables-list-title-text">{filterProject?.name ?? 'Project'}</span>
                  {filterProject?.public && (
                    <span className="tables-list-public-badge" title={PUBLIC_PROJECT_TITLE}>
                      <Globe size={11} aria-hidden="true" />
                      Public
                    </span>
                  )}
                </>
              ) : (
                <span className="tables-list-title-text">All Tables</span>
              )}
            </h2>
          </div>
          <div className="tables-list-actions">
            <button type="button" ref={newButtonRef} className="tables-list-new-button" onClick={openNewTable}>
              <Plus size={16} aria-hidden="true" />
              New Table
            </button>
          </div>
        </div>
      </header>

      <div className="tables-list-content">
        <div className="tables-list-inner">
          {body}
          {!loading && index.failedProjectIds.length > 0 && (
            <div className="tables-list-warning" role="status">
              Some project tables could not be loaded.
            </div>
          )}
        </div>
      </div>

      {!onNewTable && (
        <NewTableDialog
          isOpen={newTableOpen}
          onClose={closeNewTable}
          projects={projects}
          initialProjectId={projectId}
        />
      )}
    </div>
  );
}

function groupAriaLabel(group: TableGroup, filtered: boolean): string {
  if (filtered) return 'Tables';
  return group.projectId ? `${group.label} tables` : group.label;
}

function GroupHeading({
  group,
  filtered,
  countSuffix,
}: {
  group: TableGroup;
  filtered: boolean;
  countSuffix: string;
}) {
  const count = (
    <span className="tables-group-count">
      {group.tables.length}
      {countSuffix}
    </span>
  );
  // In the unfiltered view a project heading links to that project's
  // filtered view; the filtered view's single group is just "Tables".
  if (group.projectId && !filtered) {
    return (
      <div className="tables-group-heading">
        <Link
          to={tablesListPath(group.projectId)}
          className="tables-group-label tables-group-link"
          title={`Show only ${group.label} tables`}
        >
          <Folder size={14} className="tables-group-folder" aria-hidden="true" />
          <span className="tables-group-label-text">{group.label}</span>
          {group.project?.public && (
            <span className="tables-group-public" title={PUBLIC_PROJECT_TITLE}>
              <Globe size={12} aria-hidden="true" />
            </span>
          )}
          {group.project?.archived && <span className="tables-group-archived">Archived</span>}
        </Link>
        {count}
      </div>
    );
  }
  return (
    <div className="tables-group-heading">
      <span className="tables-group-label">
        {group.key === SHARED_TABLES_GROUP_KEY && (
          <Users size={14} className="tables-group-folder" aria-hidden="true" />
        )}
        <span className="tables-group-label-text">{group.label}</span>
      </span>
      {count}
    </div>
  );
}

function TableRowLink({ table, project }: { table: TableListRow; project: Project | null }) {
  const updated = parseUTCTimestamp(table.updated_at);
  const updatedTitle = Number.isNaN(updated.getTime()) ? undefined : updated.toLocaleString();
  return (
    <Link to={tableViewerPath(table.id)} className="tables-row">
      <span className="tables-row-main">
        <span className="tables-row-title">{table.title}</span>
        {table.description && <span className="tables-row-description">{table.description}</span>}
      </span>
      <span className="tables-row-mode">
        {shouldShowDocModeBadge(table.mode) && <DocModeBadge mode={table.mode} size="sm" noun="table" />}
      </span>
      <span className="tables-row-meta tables-row-scope" title={tableScopeTitle(table)}>
        {tableScopeLabel(table, project)}
      </span>
      <span className="tables-row-meta tables-row-rows" title={formatRowCount(table.row_count)}>
        <span className="tables-row-count">{formatTableCount(table.row_count)}</span>
        <span className="tables-row-unit"> {unit(table.row_count, 'row', 'rows')}</span>
      </span>
      <span
        className="tables-row-meta tables-row-columns"
        title={formatColumnCount(table.column_count)}
      >
        <span className="tables-row-count">{formatTableCount(table.column_count)}</span>
        <span className="tables-row-unit"> {unit(table.column_count, 'column', 'columns')}</span>
      </span>
      <span className="tables-row-meta tables-row-updated" title={updatedTitle}>
        {formatRelativeTimestamp(table.updated_at)}
      </span>
    </Link>
  );
}

/** The unit word shown after a count on the phone meta line only. */
function unit(n: number, one: string, many: string): string {
  return formatTableCount(n) === '1' ? one : many;
}
