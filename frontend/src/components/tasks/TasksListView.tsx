import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { Archive, ArchiveRestore, ListChecks, LoaderCircle, Plus, Trash2 } from 'lucide-react'
import './TasksListView.css'
import { archiveTask, deleteTask, unarchiveTask } from '../../api/tasksApi'
import type { TaskListRow } from '../../api/types'
import { useTasks } from '../../hooks/useTasks'
import { useTaskUnread } from '../../hooks/useTaskUnread'
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters'
import { newTaskPath, taskThreadPath, tasksListPath } from '../../utils/tasksRoute'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { TaskNotice } from './TaskNotice'
import { TaskLoadError, TaskViewState } from './TaskViewState'

type RowAction = 'archive' | 'unarchive'

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

/**
 * The tasks list (/tasks, /tasks?archived=true): the user's unarchived
 * tasks or the archived ones, newest activity first, each row linking to
 * its thread with the active run's working / "Needs you" mark (as in the
 * sidebar's Tasks section), the relative last-activity time (the All Tables
 * format) and the unread dot.
 *
 * Row actions: an active task offers Archive (shown on hover / focus with a
 * pointer, always on touch; disabled while a run is active, which the
 * server refuses with 409 `task_run_active`); an archived one offers
 * Unarchive and Delete, Delete behind a confirm dialog. A row's buttons are
 * disabled while its request is in flight. A failed archive / unarchive
 * (e.g. 409 `tasks_cap_reached`) shows inline above the rows, a failed
 * delete in the dialog. After a success the list re-fetches at once (the
 * `task_list_changed` event would also do it, after its debounce).
 */
export function TasksListView({ archived }: { archived: boolean }) {
  const { tasks, loading, error, refresh } = useTasks({ archived })
  const [pending, setPending] = useState<Record<string, boolean>>({})
  const [actionError, setActionError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<TaskListRow | null>(null)
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
    async (row: TaskListRow, action: RowAction) => {
      setRowPending(row.id, true)
      setActionError(null)
      try {
        if (action === 'archive') await archiveTask(row.id)
        else await unarchiveTask(row.id)
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

  const openDelete = useCallback((row: TaskListRow) => {
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
      await deleteTask(row.id)
      setDeleteTarget(null)
      await refresh()
    } catch (err) {
      setDeleteError(errorText(err, 'The task could not be deleted.'))
    } finally {
      setDeleting(false)
      setRowPending(row.id, false)
    }
  }, [deleteTarget, refresh, setRowPending])

  let body
  if (loading && tasks.length === 0) {
    body = (
      <TaskViewState>
        <p className="task-view-state-text">Loading tasks...</p>
      </TaskViewState>
    )
  } else if (error && tasks.length === 0) {
    body = <TaskLoadError message={error} onRetry={refresh} />
  } else if (tasks.length === 0) {
    body = archived ? (
      <div className="tasks-list-empty">
        <p className="tasks-list-empty-title">No archived tasks.</p>
      </div>
    ) : (
      <div className="tasks-list-empty">
        <p className="tasks-list-empty-title">No tasks yet.</p>
        <p className="tasks-list-empty-hint">
          A task keeps a brief and a log of updates, so you can come back to the same piece of
          work again and again.
        </p>
        <Link to={newTaskPath()} className="tasks-list-new">
          <Plus size={16} aria-hidden="true" />
          New task
        </Link>
      </div>
    )
  } else {
    body = (
      <ul className="tasks-list-rows">
        {tasks.map((row) => (
          <TasksListRow
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
    <div className="tasks-list">
      <header className="tasks-list-header">
        <h1 className="tasks-list-title">
          <ListChecks size={20} className="tasks-list-title-icon" aria-hidden="true" />
          {archived ? 'Archived tasks' : 'Tasks'}
        </h1>
        <div className="tasks-list-actions">
          <Link to={tasksListPath({ archived: !archived })} className="tasks-list-switch">
            {archived ? 'Active tasks' : 'Archived tasks'}
          </Link>
          {!archived && (
            <Link to={newTaskPath()} className="tasks-list-new">
              <Plus size={16} aria-hidden="true" />
              New task
            </Link>
          )}
        </div>
      </header>
      <div className="tasks-list-content">
        {actionError && (
          <div className="tasks-list-error">
            <TaskNotice message={actionError} onDismiss={() => setActionError(null)} />
          </div>
        )}
        {body}
      </div>
      <DocConfirmDialog
        isOpen={deleteTarget !== null}
        title="Delete task?"
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

function TasksListRow({
  row,
  busy,
  onArchive,
  onUnarchive,
  onDelete,
}: {
  row: TaskListRow
  busy: boolean
  onArchive: () => void
  onUnarchive: () => void
  onDelete: () => void
}) {
  const unread = useTaskUnread(row)
  const active = row.active_run
  const activity = row.last_activity_at
  const activityDate = activity ? parseUTCTimestamp(activity) : null
  const activityTitle =
    activityDate && !Number.isNaN(activityDate.getTime())
      ? `Last activity ${activityDate.toLocaleString()}`
      : undefined

  return (
    <li className={`tasks-list-item${row.archived ? ' archived' : ''}`}>
      <Link to={taskThreadPath(row.id)} className="tasks-list-row">
        <span className={`tasks-list-row-title${unread ? ' unread' : ''}`}>{row.title}</span>
        {active && active.status === 'waiting' && (
          <span className="tasks-list-row-state waiting">Needs you</span>
        )}
        {active && active.status === 'running' && (
          <span className="tasks-list-row-state" title="An update is in progress">
            <LoaderCircle size={13} className="tasks-list-row-spinner" aria-hidden="true" />
            <span className="tasks-list-row-state-label">Working</span>
          </span>
        )}
        {activity && (
          <span className="tasks-list-row-time" title={activityTitle}>
            {formatRelativeTimestamp(activity)}
          </span>
        )}
        <span className="tasks-list-row-dot-slot">
          {unread && <span className="tasks-list-row-unread" role="img" aria-label="Unread" />}
        </span>
      </Link>
      <div className="tasks-list-row-actions">
        {row.archived ? (
          <>
            <button
              type="button"
              className="tasks-list-row-button"
              onClick={onUnarchive}
              disabled={busy}
              title="Move this task back to the active list"
              aria-label={`Unarchive ${row.title}`}
            >
              <ArchiveRestore size={14} aria-hidden="true" />
              <span className="tasks-list-row-button-label">Unarchive</span>
            </button>
            <button
              type="button"
              className="tasks-list-row-button danger"
              onClick={onDelete}
              disabled={busy}
              title="Delete this task, its updates and its files"
              aria-label={`Delete ${row.title}`}
            >
              <Trash2 size={14} aria-hidden="true" />
              <span className="tasks-list-row-button-label">Delete</span>
            </button>
          </>
        ) : (
          <button
            type="button"
            className="tasks-list-row-button icon-only"
            onClick={onArchive}
            disabled={busy || active !== null}
            title={active ? 'Stop or finish the update in progress to archive this task' : 'Archive'}
            aria-label={`Archive ${row.title}`}
          >
            <Archive size={15} aria-hidden="true" />
          </button>
        )}
      </div>
    </li>
  )
}
