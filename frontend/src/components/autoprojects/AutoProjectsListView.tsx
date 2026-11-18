import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { Archive, ArchiveRestore, ListChecks, LoaderCircle, Plus, Trash2 } from 'lucide-react'
import './AutoProjectsListView.css'
import { archiveAutoProject, deleteAutoProject, unarchiveAutoProject } from '../../api/autoprojectsApi'
import type { AutoProjectListRow } from '../../api/types'
import { useAutoProjects } from '../../hooks/useAutoProjects'
import { useAutoProjectUnread } from '../../hooks/useAutoProjectUnread'
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters'
import { newAutoProjectPath, autoprojectPath, autoprojectsListPath } from '../../utils/autoprojectsRoute'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { AutoProjectNotice } from './AutoProjectNotice'
import { AutoProjectLoadError, AutoProjectViewState } from './AutoProjectViewState'

type RowAction = 'archive' | 'unarchive'

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

/**
 * The AutoProjects list (/autoprojects, /autoprojects?archived=true): the user's unarchived
 * AutoProjects or the archived ones, newest activity first, each row linking to
 * its page with the active run's working / "Needs you" mark (as in the
 * sidebar's AutoProjects section), the relative last-activity time (the All Tables
 * format) and the unread dot.
 *
 * Row actions: an active AutoProject offers Archive (shown on hover / focus with a
 * pointer, always on touch; disabled while a run is active, which the
 * server refuses with 409 `autoproject_run_active`); an archived one offers
 * Unarchive and Delete, Delete behind a confirm dialog. A row's buttons are
 * disabled while its request is in flight. A failed archive / unarchive
 * (e.g. 409 `autoprojects_cap_reached`) shows inline above the rows, a failed
 * delete in the dialog. After a success the list re-fetches at once (the
 * `autoproject_list_changed` event would also do it, after its debounce).
 */
export function AutoProjectsListView({ archived }: { archived: boolean }) {
  const { autoprojects, loading, error, refresh } = useAutoProjects({ archived })
  const [pending, setPending] = useState<Record<string, boolean>>({})
  const [actionError, setActionError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<AutoProjectListRow | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const setRowPending = useCallback((id: string, value: boolean) => {
    setPending((prev) => {
      const next = { ...prev }
      if (value) next[id] = true
      else delete next[id]
      return next
    })
  }, [])

  const runRowAction = useCallback(
    async (row: AutoProjectListRow, action: RowAction) => {
      setRowPending(row.id, true)
      setActionError(null)
      try {
        if (action === 'archive') await archiveAutoProject(row.id)
        else await unarchiveAutoProject(row.id)
        await refresh()
      } catch (err) {
        const verb = action === 'archive' ? 'archived' : 'unarchived'
        setActionError(errorText(err, `"${row.title}" could not be ${verb}.`))
      } finally {
        setRowPending(row.id, false)
      }
    },
    [refresh, setRowPending],
  )

  const openDelete = useCallback((row: AutoProjectListRow) => {
    setDeleteError(null)
    setDeleteTarget(row)
  }, [])

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return
    const row = deleteTarget
    setDeleting(true)
    setDeleteError(null)
    setRowPending(row.id, true)
    try {
      await deleteAutoProject(row.id)
      setDeleteTarget(null)
      await refresh()
    } catch (err) {
      setDeleteError(errorText(err, 'The AutoProject could not be deleted.'))
    } finally {
      setDeleting(false)
      setRowPending(row.id, false)
    }
  }, [deleteTarget, refresh, setRowPending])

  let body
  if (loading && autoprojects.length === 0) {
    body = (
      <AutoProjectViewState>
        <p className="autoproject-view-state-text">Loading AutoProjects...</p>
      </AutoProjectViewState>
    )
  } else if (error && autoprojects.length === 0) {
    body = <AutoProjectLoadError message={error} onRetry={refresh} />
  } else if (autoprojects.length === 0) {
    body = archived ? (
      <div className="autoprojects-list-empty">
        <p className="autoprojects-list-empty-title">No archived AutoProjects.</p>
      </div>
    ) : (
      <div className="autoprojects-list-empty">
        <p className="autoprojects-list-empty-title">No AutoProjects yet.</p>
        <p className="autoprojects-list-empty-hint">
          An AutoProject keeps a brief and a log of updates, so you can come back to the same piece of
          work again and again.
        </p>
        <Link to={newAutoProjectPath()} className="autoprojects-list-new">
          <Plus size={16} aria-hidden="true" />
          New AutoProject
        </Link>
      </div>
    )
  } else {
    body = (
      <ul className="autoprojects-list-rows">
        {autoprojects.map((row) => (
          <AutoProjectsListRow
            key={row.id}
            row={row}
            busy={Boolean(pending[row.id])}
            onArchive={() => void runRowAction(row, 'archive')}
            onUnarchive={() => void runRowAction(row, 'unarchive')}
            onDelete={() => openDelete(row)}
          />
        ))}
      </ul>
    )
  }

  return (
    <div className="autoprojects-list">
      <header className="autoprojects-list-header">
        <h1 className="autoprojects-list-title">
          <ListChecks size={20} className="autoprojects-list-title-icon" aria-hidden="true" />
          {archived ? 'Archived AutoProjects' : 'AutoProjects'}
        </h1>
        <div className="autoprojects-list-actions">
          <Link to={autoprojectsListPath({ archived: !archived })} className="autoprojects-list-switch">
            {archived ? 'Active AutoProjects' : 'Archived AutoProjects'}
          </Link>
          {!archived && (
            <Link to={newAutoProjectPath()} className="autoprojects-list-new">
              <Plus size={16} aria-hidden="true" />
              New AutoProject
            </Link>
          )}
        </div>
      </header>
      <div className="autoprojects-list-content">
        {actionError && (
          <div className="autoprojects-list-error">
            <AutoProjectNotice message={actionError} onDismiss={() => setActionError(null)} />
          </div>
        )}
        {body}
      </div>
      <DocConfirmDialog
        isOpen={deleteTarget !== null}
        title="Delete AutoProject?"
        confirmLabel="Delete"
        busyLabel="Deleting..."
        tone="danger"
        busy={deleting}
        error={deleteError}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      >
        <p>
          <strong>{deleteTarget?.title}</strong> is deleted for good, with all its updates and the work
          behind them, its brief and its files.
        </p>
        <p>This cannot be undone.</p>
      </DocConfirmDialog>
    </div>
  )
}

function AutoProjectsListRow({
  row,
  busy,
  onArchive,
  onUnarchive,
  onDelete,
}: {
  row: AutoProjectListRow
  busy: boolean
  onArchive: () => void
  onUnarchive: () => void
  onDelete: () => void
}) {
  const unread = useAutoProjectUnread(row)
  const active = row.active_run
  const activity = row.last_activity_at
  const activityDate = activity ? parseUTCTimestamp(activity) : null
  const activityTitle =
    activityDate && !Number.isNaN(activityDate.getTime())
      ? `Last activity ${activityDate.toLocaleString()}`
      : undefined

  return (
    <li className={`autoprojects-list-item${row.archived ? ' archived' : ''}`}>
      <Link to={autoprojectPath(row.id)} className="autoprojects-list-row">
        <span className={`autoprojects-list-row-title${unread ? ' unread' : ''}`}>{row.title}</span>
        {active && active.status === 'waiting' && (
          <span className="autoprojects-list-row-state waiting">Needs you</span>
        )}
        {active && active.status === 'running' && (
          <span className="autoprojects-list-row-state" title="An update is in progress">
            <LoaderCircle size={13} className="autoprojects-list-row-spinner" aria-hidden="true" />
            <span>Working</span>
          </span>
        )}
        {activity && (
          <span className="autoprojects-list-row-time" title={activityTitle}>
            {formatRelativeTimestamp(activity)}
          </span>
        )}
        <span className="autoprojects-list-row-dot-slot">
          {unread && <span className="autoprojects-list-row-unread" role="img" aria-label="Unread" />}
        </span>
      </Link>
      <div className="autoprojects-list-row-actions">
        {row.archived ? (
          <>
            <button
              type="button"
              className="autoprojects-list-row-button"
              onClick={onUnarchive}
              disabled={busy}
              title="Move this AutoProject back to the active list"
              aria-label={`Unarchive ${row.title}`}
            >
              <ArchiveRestore size={14} aria-hidden="true" />
              <span className="autoprojects-list-row-button-label">Unarchive</span>
            </button>
            <button
              type="button"
              className="autoprojects-list-row-button danger"
              onClick={onDelete}
              disabled={busy}
              title="Delete this AutoProject, its updates and its files"
              aria-label={`Delete ${row.title}`}
            >
              <Trash2 size={14} aria-hidden="true" />
              <span className="autoprojects-list-row-button-label">Delete</span>
            </button>
          </>
        ) : (
          <button
            type="button"
            className="autoprojects-list-row-button icon-only"
            onClick={onArchive}
            disabled={busy || active !== null}
            title={active ? 'Stop or finish the update in progress to archive this AutoProject' : 'Archive'}
            aria-label={`Archive ${row.title}`}
          >
            <Archive size={15} aria-hidden="true" />
          </button>
        )}
      </div>
    </li>
  )
}
