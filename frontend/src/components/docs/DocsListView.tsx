/**
 * All Docs: the main-pane takeover at /docs and /docs?project=<id> (spec
 * 8.2). Rendered by App / MobileShell inside `.main-content.docs-main`, the
 * scrolling column; the header sticks to its top.
 *
 * Data. The backend has no cross-project doc list, so:
 *   - unfiltered: "Your docs" = useDocs({limit: 50}) (server-paged, "Load
 *     more"), plus one group per project with docs -- archived projects
 *     included and marked, since archiving has no effect on docs (spec 10)
 *     -- from useProjectDocsIndex (one fetchDocs per project, up to 200
 *     docs each);
 *   - filtered (?project=<id>): useDocs({projectId, limit: 50}) only.
 * Both hooks always run (the index is simply disabled while filtered, and
 * until the project list has loaded).
 * Search filters everything loaded, client-side. Grouping, filtering and the
 * size column are the pure helpers in utils/allDocsGrouping.ts. The mode
 * badge follows utils/docMode: a private doc shows none while the
 * public_projects gate is closed for the user, and the "Mode" column label
 * goes too when no rendered row shows a badge.
 */

import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ChevronLeft, Folder, Globe, Plus, Search } from 'lucide-react';
import type { Doc, Project } from '../../api/types';
import { useAuth } from '../../contexts/AuthContext';
import { useProjects } from '../../contexts/ProjectsContext';
import { useDocs } from '../../hooks/useDocs';
import { useProjectDocsIndex } from '../../hooks/useProjectDocsIndex';
import {
  USER_DOCS_GROUP_KEY,
  docScopeLabel,
  filterDocs,
  formatDocSize,
  groupDocs,
  projectGroupKey,
  type DocGroup,
} from '../../utils/allDocsGrouping';
import { isPublicProjectsEnabled, shouldShowDocModeBadge } from '../../utils/docMode';
import { docsListPath, docViewerPath } from '../../utils/docsRoute';
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters';
import { DocModeBadge } from './DocModeBadge';
import { NewDocModal } from './NewDocModal';
import './DocsListView.css';

/** Page size of the server-paged list ("Your docs", or the filtered project). */
const PAGE_SIZE = 50;

const PUBLIC_PROJECT_TITLE = 'Public project — internet access, no internal data';

export function DocsListView({ projectId }: { projectId: string | null }) {
  const navigate = useNavigate();
  const { projects, projectsLoaded } = useProjects();
  const { enabledFeatures } = useAuth();
  const publicProjectsEnabled = isPublicProjectsEnabled(enabledFeatures);
  const filtered = projectId !== null;

  // One paged list (the user's docs, or the filtered project's) plus the
  // per-project index, which only the unfiltered view needs. The index waits
  // for the project list: fanning out over the empty pre-load list would
  // "load" an index with no projects in it.
  const list = useDocs({ projectId, limit: PAGE_SIZE });
  const index = useProjectDocsIndex(projects, !filtered && projectsLoaded);

  const [query, setQuery] = useState('');
  const [newDocOpen, setNewDocOpen] = useState(false);

  const projectsById = useMemo(() => new Map(projects.map((p) => [p.id, p])), [projects]);
  const filterProject = projectId ? (projectsById.get(projectId) ?? null) : null;

  // The group the paged list feeds; it owns the "Load more" footer.
  const pagedGroupKey = projectId ? projectGroupKey(projectId) : USER_DOCS_GROUP_KEY;

  const groups = useMemo<DocGroup[]>(() => {
    if (projectId) {
      return [
        {
          key: projectGroupKey(projectId),
          label: 'Docs',
          project: filterProject,
          projectId,
          docs: list.docs,
        },
      ];
    }
    return groupDocs(list.docs, index.byProject, projects);
  }, [projectId, filterProject, list.docs, index.byProject, projects]);

  const trimmedQuery = query.trim();
  const visibleGroups = useMemo(
    () =>
      groups
        .map((group) => ({ ...group, docs: filterDocs(group.docs, trimmedQuery) }))
        // A query that hides a whole group drops it; without a query the
        // paged group stays (empty line + "Load more") unless it failed.
        .filter(
          (group) =>
            group.docs.length > 0 ||
            (!trimmedQuery && !list.error && group.key === pagedGroupKey),
        ),
    [groups, trimmedQuery, list.error, pagedGroupKey],
  );

  const totalDocs = groups.reduce((n, group) => n + group.docs.length, 0);
  const visibleDocs = visibleGroups.reduce((n, group) => n + group.docs.length, 0);
  // The "Mode" column label only while some rendered row shows a badge:
  // with public projects closed for the user, all-private rows show none.
  const anyModeBadge = visibleGroups.some((group) =>
    group.docs.some((doc) => shouldShowDocModeBadge(doc.mode, publicProjectsEnabled)),
  );

  // Wait for the first page AND the project index (which itself waits for
  // the project list) before deciding between rows and the empty state, so
  // a user whose docs are all project docs never sees "No docs yet." flash.
  // Initial load only: a project-set change (create / delete / archive while
  // this view is open) keeps the previous groups on screen while the index
  // re-fans-out, like useDocs keeps its rows during a refresh.
  const loading = list.loading
    || (!filtered && (!projectsLoaded || (index.loading && !index.loaded)));

  const handleCreated = useCallback(
    (doc: Doc) => {
      setNewDocOpen(false);
      navigate(docViewerPath(doc.id));
    },
    [navigate],
  );

  const loadMoreButton = list.hasMore ? (
    <button
      type="button"
      className="docs-load-more"
      onClick={list.loadMore}
      disabled={list.loadingMore}
    >
      {list.loadingMore ? 'Loading...' : 'Load more'}
    </button>
  ) : null;

  let body: ReactNode;
  if (loading) {
    body = <div className="docs-list-status">Loading...</div>;
  } else if (totalDocs === 0 && !list.error) {
    body = (
      <div className="docs-list-empty">
        <p className="docs-list-empty-title">No docs yet.</p>
        <p className="docs-list-empty-hint">Ask Quest to create one, or use New Doc.</p>
      </div>
    );
  } else {
    body = (
      <>
        {list.error && (
          <div className="docs-list-error" role="alert">
            {list.error}
          </div>
        )}
        {visibleDocs === 0 && trimmedQuery ? (
          <div className="docs-list-no-match">
            <p>No docs match '{trimmedQuery}'</p>
            {loadMoreButton && (
              <div className="docs-group-footer">
                <span className="docs-list-no-match-hint">Search covers the docs loaded so far.</span>
                {loadMoreButton}
              </div>
            )}
          </div>
        ) : visibleGroups.length > 0 ? (
          <>
            <div className="docs-columns" aria-hidden="true">
              <span>Title</span>
              <span>{anyModeBadge ? 'Mode' : ''}</span>
              <span className="docs-col-scope">Scope</span>
              <span>Updated</span>
              <span className="docs-col-size">Size</span>
            </div>
            {visibleGroups.map((group) => (
              <section key={group.key} className="docs-group" aria-label={groupAriaLabel(group, filtered)}>
                <GroupHeading
                  group={group}
                  filtered={filtered}
                  countSuffix={group.key === pagedGroupKey && list.hasMore && !trimmedQuery ? '+' : ''}
                />
                {group.docs.length === 0 ? (
                  <div className="docs-group-empty">No docs of your own yet.</div>
                ) : (
                  <div className="docs-rows">
                    {group.docs.map((doc) => (
                      <DocRow
                        key={doc.id}
                        doc={doc}
                        project={doc.project_id ? (projectsById.get(doc.project_id) ?? null) : null}
                        showModeBadge={shouldShowDocModeBadge(doc.mode, publicProjectsEnabled)}
                      />
                    ))}
                  </div>
                )}
                {group.key === pagedGroupKey && loadMoreButton && (
                  <div className="docs-group-footer">{loadMoreButton}</div>
                )}
              </section>
            ))}
          </>
        ) : null}
      </>
    );
  }

  return (
    <div className="docs-list">
      <header className="docs-list-header">
        <div className="docs-list-header-inner">
          <div className="docs-list-heading">
            {filtered && (
              <Link to={docsListPath()} className="docs-list-back">
                <ChevronLeft size={16} aria-hidden="true" />
                All docs
              </Link>
            )}
            <h2 className="docs-list-title">
              {filtered ? (
                <>
                  <Folder size={18} className="docs-list-title-icon" aria-hidden="true" />
                  <span className="docs-list-title-text">{filterProject?.name ?? 'Project'}</span>
                  {filterProject?.public && (
                    <span className="docs-list-public-badge" title={PUBLIC_PROJECT_TITLE}>
                      <Globe size={11} aria-hidden="true" />
                      Public
                    </span>
                  )}
                </>
              ) : (
                <span className="docs-list-title-text">All Docs</span>
              )}
            </h2>
          </div>
          <div className="docs-list-actions">
            <label className="docs-list-search">
              <Search size={15} className="docs-list-search-icon" aria-hidden="true" />
              <input
                type="search"
                className="docs-list-search-input"
                placeholder="Search docs"
                aria-label="Search docs"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="docs-list-new-button"
              onClick={() => setNewDocOpen(true)}
            >
              <Plus size={16} aria-hidden="true" />
              New Doc
            </button>
          </div>
        </div>
      </header>

      <div className="docs-list-content">
        <div className="docs-list-inner">
          {body}
          {!loading && index.failedProjectIds.length > 0 && (
            <div className="docs-list-warning" role="status">
              Some project docs could not be loaded.
            </div>
          )}
        </div>
      </div>

      <NewDocModal
        isOpen={newDocOpen}
        onClose={() => setNewDocOpen(false)}
        projects={projects}
        initialProjectId={projectId}
        onCreated={handleCreated}
      />
    </div>
  );
}

function groupAriaLabel(group: DocGroup, filtered: boolean): string {
  if (filtered) return 'Docs';
  return group.projectId ? `${group.label} docs` : group.label;
}

function GroupHeading({
  group,
  filtered,
  countSuffix,
}: {
  group: DocGroup;
  filtered: boolean;
  countSuffix: string;
}) {
  const count = (
    <span className="docs-group-count">
      {group.docs.length}
      {countSuffix}
    </span>
  );
  // In the unfiltered view a project heading links to that project's
  // filtered view; the filtered view's single group is just "Docs".
  if (group.projectId && !filtered) {
    return (
      <div className="docs-group-heading">
        <Link
          to={docsListPath(group.projectId)}
          className="docs-group-label docs-group-link"
          title={`Show only ${group.label} docs`}
        >
          <Folder size={14} className="docs-group-folder" aria-hidden="true" />
          <span className="docs-group-label-text">{group.label}</span>
          {group.project?.public && (
            <span className="docs-group-public" title={PUBLIC_PROJECT_TITLE}>
              <Globe size={12} aria-hidden="true" />
            </span>
          )}
          {group.project?.archived && <span className="docs-group-archived">Archived</span>}
        </Link>
        {count}
      </div>
    );
  }
  return (
    <div className="docs-group-heading">
      <span className="docs-group-label">
        <span className="docs-group-label-text">{group.label}</span>
      </span>
      {count}
    </div>
  );
}

function DocRow({
  doc,
  project,
  showModeBadge,
}: {
  doc: Doc;
  project: Project | null;
  showModeBadge: boolean;
}) {
  const updated = parseUTCTimestamp(doc.updated_at);
  const updatedTitle = Number.isNaN(updated.getTime()) ? undefined : updated.toLocaleString();
  return (
    <Link to={docViewerPath(doc.id)} className="docs-row">
      <span className="docs-row-main">
        <span className="docs-row-title">{doc.title}</span>
        {doc.description && <span className="docs-row-description">{doc.description}</span>}
      </span>
      <span className="docs-row-mode">
        {showModeBadge && <DocModeBadge mode={doc.mode} size="sm" />}
      </span>
      <span className="docs-row-meta docs-row-scope">{docScopeLabel(doc, project)}</span>
      <span className="docs-row-meta docs-row-updated" title={updatedTitle}>
        {formatRelativeTimestamp(doc.updated_at)}
      </span>
      <span className="docs-row-meta docs-row-size">
        {formatDocSize(doc.content_size, doc.asset_count)}
      </span>
    </Link>
  );
}
