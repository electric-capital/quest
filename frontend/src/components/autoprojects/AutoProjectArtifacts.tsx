import { useId, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import './AutoProjectArtifacts.css'
import './AutoProjectBriefControls.css'
import { DOCS_FEATURE } from '../../api/docsApi'
import { TABLES_FEATURE } from '../../api/tablesApi'
import type { AutoProject, Doc, TableListRow } from '../../api/types'
import { useAuth } from '../../contexts/AuthContext'
import { useProjects } from '../../contexts/ProjectsContext'
import { useDocs } from '../../hooks/useDocs'
import { useTables } from '../../hooks/useTables'
import { docViewerPath } from '../../utils/docsRoute'
import { formatRelativeTimestamp } from '../../utils/formatters'
import { tableViewerPath } from '../../utils/tablesRoute'
import { NewDocModal } from '../docs/NewDocModal'
import { NewTableDialog } from '../tables/NewTableDialog'

/** Rows fetched per kind; "Show more" pages past it. */
const PAGE_SIZE = 50

/** One row of the card: a doc or a table of this AutoProject. */
export interface AutoProjectArtifactRow {
  kind: 'doc' | 'table'
  id: string
  title: string
  description: string
  updatedAt: string
  /** A table's row count; null for a doc. */
  rowCount: number | null
}

/**
 * The AutoProject's docs and tables as one list, newest-updated first (the
 * timestamps are naive-UTC ISO strings of one format, so they sort as text;
 * ties keep docs before tables, then by id).
 */
export function mergeAutoProjectArtifacts(docs: Doc[], tables: TableListRow[]): AutoProjectArtifactRow[] {
  const rows: AutoProjectArtifactRow[] = [
    ...docs.map((doc) => ({
      kind: 'doc' as const,
      id: doc.id,
      title: doc.title,
      description: doc.description,
      updatedAt: doc.updated_at,
      rowCount: null,
    })),
    ...tables.map((table) => ({
      kind: 'table' as const,
      id: table.id,
      title: table.title,
      description: table.description,
      updatedAt: table.updated_at,
      rowCount: table.row_count,
    })),
  ]
  return rows.sort(
    (a, b) =>
      b.updatedAt.localeCompare(a.updatedAt) || a.kind.localeCompare(b.kind) || a.id.localeCompare(b.id),
  )
}

function summary(docCount: number, tableCount: number, more: boolean): string {
  const parts: string[] = []
  if (docCount > 0) parts.push(`${docCount}${more ? '+' : ''} doc${docCount === 1 ? '' : 's'}`)
  if (tableCount > 0) parts.push(`${tableCount}${more ? '+' : ''} table${tableCount === 1 ? '' : 's'}`)
  return parts.join(', ')
}

function emptyText(docsEnabled: boolean, tablesEnabled: boolean, archived: boolean): string {
  const what = docsEnabled && tablesEnabled ? 'docs or tables' : docsEnabled ? 'docs' : 'tables'
  if (archived) return `No ${what}.`
  return `No ${what} yet. An update can create them for this AutoProject, or add one here.`
}

function DocIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="8" y1="13" x2="16" y2="13" />
      <line x1="8" y1="17" x2="13" y2="17" />
    </svg>
  )
}

function TableIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <line x1="3" y1="10" x2="21" y2="10" />
      <line x1="3" y1="15" x2="21" y2="15" />
      <line x1="9" y1="4" x2="9" y2="20" />
    </svg>
  )
}

/**
 * The "Docs and tables" card of the AutoProject page: the Quest Docs and
 * Quest Tables that belong to this AutoProject (`autoproject_id`), created
 * by its updates or with New doc / New table here, as one list linking to
 * the doc and table viewers. Each kind shows only while its feature gate is
 * open for the user; with both closed the card renders nothing.
 *
 * The two lists are useDocs / useTables over the AutoProject stream, so they
 * follow `doc_list_changed` / `table_list_changed` (a run creating or
 * writing one moves it to the top). An archived AutoProject keeps the list
 * and loses the two buttons.
 */
export function AutoProjectArtifacts({ autoproject }: { autoproject: AutoProject }) {
  const headingId = useId()
  const navigate = useNavigate()
  const { enabledFeatures } = useAuth()
  const { projects } = useProjects()
  const docsEnabled = enabledFeatures.includes(DOCS_FEATURE)
  const tablesEnabled = enabledFeatures.includes(TABLES_FEATURE)

  const docs = useDocs({ autoprojectId: autoproject.id, limit: PAGE_SIZE, enabled: docsEnabled })
  const tables = useTables({ autoprojectId: autoproject.id, limit: PAGE_SIZE, enabled: tablesEnabled })
  const rows = useMemo(() => mergeAutoProjectArtifacts(docs.docs, tables.tables), [docs.docs, tables.tables])

  const [newDocOpen, setNewDocOpen] = useState(false)
  const [newTableOpen, setNewTableOpen] = useState(false)

  if (!docsEnabled && !tablesEnabled) return null

  const location = { id: autoproject.id, title: autoproject.title }
  const loading = docs.loading || tables.loading
  const error = docs.error || tables.error
  const hasMore = docs.hasMore || tables.hasMore
  const loadingMore = docs.loadingMore || tables.loadingMore
  const loadMore = () => {
    if (docs.hasMore) docs.loadMore()
    if (tables.hasMore) tables.loadMore()
  }

  let body
  if (rows.length > 0) {
    body = (
      <ul className="autoproject-artifacts-list">
        {rows.map((row) => (
          <li key={`${row.kind}:${row.id}`}>
            <Link
              className="autoproject-artifact"
              to={row.kind === 'doc' ? docViewerPath(row.id) : tableViewerPath(row.id)}
            >
              <span className="autoproject-artifact-icon">{row.kind === 'doc' ? <DocIcon /> : <TableIcon />}</span>
              <span className="autoproject-artifact-text">
                <span className="autoproject-artifact-title">{row.title}</span>
                {row.description && <span className="autoproject-artifact-description">{row.description}</span>}
              </span>
              <span className="autoproject-artifact-meta">
                {row.kind === 'doc'
                  ? 'Doc'
                  : `Table, ${row.rowCount} row${row.rowCount === 1 ? '' : 's'}`}
                {' · '}
                {formatRelativeTimestamp(row.updatedAt)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    )
  } else if (loading) {
    body = <p className="autoproject-card-empty">Loading...</p>
  } else if (!error) {
    body = <p className="autoproject-card-empty">{emptyText(docsEnabled, tablesEnabled, autoproject.archived)}</p>
  }

  return (
    <section className="autoproject-card autoproject-artifacts" aria-labelledby={headingId}>
      <div className="autoproject-card-head">
        <h2 id={headingId} className="autoproject-card-title">
          {docsEnabled && tablesEnabled ? 'Docs and tables' : docsEnabled ? 'Docs' : 'Tables'}
        </h2>
        <p className="autoproject-card-meta">{summary(docs.docs.length, tables.tables.length, hasMore)}</p>
        {!autoproject.archived && (
          <div className="autoproject-card-actions">
            {docsEnabled && (
              <button type="button" className="autoproject-ctl-button" onClick={() => setNewDocOpen(true)}>
                New doc
              </button>
            )}
            {tablesEnabled && (
              <button type="button" className="autoproject-ctl-button" onClick={() => setNewTableOpen(true)}>
                New table
              </button>
            )}
          </div>
        )}
      </div>
      <div className="autoproject-card-body autoproject-artifacts-body">
        {error && (
          <p className="new-doc-error autoproject-artifacts-error" role="alert">
            {error}
          </p>
        )}
        {body}
        {hasMore && (
          <button
            type="button"
            className="autoproject-ctl-button autoproject-artifacts-more"
            onClick={loadMore}
            disabled={loadingMore}
          >
            {loadingMore ? 'Loading...' : 'Show more'}
          </button>
        )}
      </div>

      {docsEnabled && (
        <NewDocModal
          isOpen={newDocOpen}
          onClose={() => setNewDocOpen(false)}
          projects={projects}
          initialProjectId={null}
          autoproject={location}
          onCreated={(doc) => navigate(docViewerPath(doc.id))}
        />
      )}
      {tablesEnabled && (
        <NewTableDialog
          isOpen={newTableOpen}
          onClose={() => setNewTableOpen(false)}
          projects={projects}
          initialProjectId={null}
          autoproject={location}
        />
      )}
    </section>
  )
}
