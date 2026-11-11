/**
 * TaskHeader -- the title unit at the top of the task page (/tasks/<id>).
 *
 * Same interaction model as DocHeader / ConversationHeader: the title plus a
 * chevron open a small dropdown (`role="menu"`, closed by an outside click or
 * Escape), single-key hints act while it is open, and Rename swaps the title
 * for an inline input. Items (spec 8.3: title, archive and delete live here,
 * not in the panel):
 *
 * - Rename (R): Enter or blur saves through PATCH /tasks/{id}, Escape
 *   cancels; an empty or unchanged title cancels. A 400 `invalid_title`
 *   keeps the input open with the server's message under it; blurring away
 *   from the refused title gives up instead of resubmitting it.
 * - Archive (A) on an unarchived task, disabled while a run is active (the
 *   server refuses with 409 `task_run_active`, still handled since the shown
 *   state can lag); Unarchive (U) on an archived one (409
 *   `tasks_cap_reached`).
 * - Delete (D), behind a danger confirm with the tasks list's wording; on
 *   success the page navigates to the tasks list (replace: the task route
 *   would 404), failures show in the dialog.
 *
 * The "Archived" chip sits beside the title. Writes hand the returned Task
 * to `onTaskDetail` (useTask.applyDetail) rather than waiting for the
 * realtime re-fetch. Failures show as a dismissible TaskNotice under the
 * title until dismissed or the next menu action; the archive confirmation is
 * a transient status line (NOTICE_MS).
 *
 * The page-level `.task-header`, `.task-header-title` and `.task-header-chip`
 * rules are in TaskPage.css; everything the menu adds is in TaskHeader.css.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Archive, ArchiveRestore, ChevronDown, Pencil, Trash2 } from 'lucide-react'
import {
  archiveTask,
  deleteTask,
  taskActiveRunFromError,
  TASK_TITLE_MAX_CHARS,
  unarchiveTask,
  updateTask,
} from '../../api/tasksApi'
import { ApiClientError } from '../../api/request'
import type { Task } from '../../api/types'
import { tasksListPath } from '../../utils/tasksRoute'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { TaskNotice } from './TaskNotice'
import './TaskHeader.css'

const NOTICE_MS = 5000
const ARCHIVED_NOTICE = 'Task archived. It is hidden from the sidebar and takes no new runs.'
const CAP_REACHED_FALLBACK =
  'You have reached the limit of active tasks; archive or delete another task first.'

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

function errorCode(err: unknown): string | null {
  return err instanceof ApiClientError ? err.errorCode ?? null : null
}

function runActiveMessage(seq: number | null | undefined): string {
  const run = seq ? `Run ${seq}` : 'A run'
  return `${run} is still working. Stop it or wait for it to finish before archiving.`
}

interface HeaderNotice {
  text: string
  /** `status`: transient confirmation; `error`: stays until dismissed. */
  tone: 'status' | 'error'
  /** Restarts the timer when the same text is shown again. */
  seq: number
}

export interface TaskHeaderProps {
  task: Task
  /** A Task returned by rename / archive / unarchive (useTask.applyDetail). */
  onTaskDetail?: (task: Task) => void
}

export function TaskHeader({ task, onTaskDetail }: TaskHeaderProps) {
  const navigate = useNavigate()
  const activeRun = task.active_run
  const canArchive = !task.archived && activeRun === null

  const [menuOpen, setMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [renameSaving, setRenameSaving] = useState(false)
  const [renameError, setRenameError] = useState<string | null>(null)
  const renameInputRef = useRef<HTMLInputElement>(null)
  // Guards the blur-after-Enter double submit and the blur while saving.
  const renameSubmittedRef = useRef(false)
  // The last title the server refused while the input stayed open.
  const rejectedTitleRef = useRef<string | null>(null)

  const [archiving, setArchiving] = useState(false)

  const [notice, setNotice] = useState<HeaderNotice | null>(null)
  const noticeSeqRef = useRef(0)
  const showNotice = useCallback((text: string, tone: HeaderNotice['tone']) => {
    noticeSeqRef.current += 1
    setNotice({ text, tone, seq: noticeSeqRef.current })
  }, [])

  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useEffect(() => {
    if (!notice || notice.tone !== 'status') return
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS)
    return () => window.clearTimeout(timer)
  }, [notice])

  // Close the menu on any outside click (the toggle stops propagation).
  useEffect(() => {
    if (!menuOpen) return
    const close = () => setMenuOpen(false)
    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [menuOpen])

  const toggleMenu = useCallback(() => {
    setNotice(null)
    setMenuOpen((open) => !open)
  }, [])

  // --- Rename ---------------------------------------------------------------

  const startRename = useCallback(() => {
    setMenuOpen(false)
    setNotice(null)
    setRenameError(null)
    renameSubmittedRef.current = false
    rejectedTitleRef.current = null
    setRenameValue(task.title)
    setRenaming(true)
  }, [task.title])

  useEffect(() => {
    if (renaming) {
      renameInputRef.current?.focus()
      renameInputRef.current?.select()
    }
  }, [renaming])

  const cancelRename = useCallback(() => {
    renameSubmittedRef.current = true
    rejectedTitleRef.current = null
    setRenaming(false)
    setRenameValue('')
    setRenameError(null)
  }, [])

  const submitRename = useCallback(
    async (trigger: 'enter' | 'blur') => {
      if (renameSubmittedRef.current) return
      const title = renameValue.trim()
      if (!title || title === task.title) {
        cancelRename()
        return
      }
      if (trigger === 'blur' && title === rejectedTitleRef.current) {
        cancelRename()
        return
      }
      renameSubmittedRef.current = true
      setNotice(null)
      setRenameError(null)
      setRenameSaving(true)
      try {
        const updated = await updateTask(task.id, { title })
        onTaskDetail?.(updated)
        setRenaming(false)
        setRenameValue('')
      } catch (err) {
        if (errorCode(err) === 'invalid_title') {
          // Fixable by editing the typed title: keep the input open.
          setRenameError(errorMessage(err, 'That title cannot be used.'))
          rejectedTitleRef.current = title
          renameSubmittedRef.current = false
          if (trigger === 'enter') renameInputRef.current?.focus()
        } else {
          showNotice(errorMessage(err, 'The task could not be renamed.'), 'error')
          setRenaming(false)
          setRenameValue('')
        }
      } finally {
        setRenameSaving(false)
      }
    },
    [cancelRename, onTaskDetail, renameValue, showNotice, task.id, task.title],
  )

  // --- Archive / unarchive --------------------------------------------------

  const runArchive = useCallback(
    async (action: 'archive' | 'unarchive') => {
      setMenuOpen(false)
      setNotice(null)
      if (archiving) return
      setArchiving(true)
      try {
        const updated = action === 'archive' ? await archiveTask(task.id) : await unarchiveTask(task.id)
        onTaskDetail?.(updated)
        if (action === 'archive') showNotice(ARCHIVED_NOTICE, 'status')
      } catch (err) {
        const code = errorCode(err)
        if (code === 'task_run_active') {
          showNotice(runActiveMessage(taskActiveRunFromError(err)?.seq ?? activeRun?.seq), 'error')
        } else if (code === 'tasks_cap_reached') {
          showNotice(errorMessage(err, CAP_REACHED_FALLBACK), 'error')
        } else {
          const verb = action === 'archive' ? 'archived' : 'unarchived'
          showNotice(errorMessage(err, `The task could not be ${verb}.`), 'error')
        }
      } finally {
        setArchiving(false)
      }
    },
    [activeRun?.seq, archiving, onTaskDetail, showNotice, task.id],
  )

  // --- Delete ---------------------------------------------------------------

  const openDelete = useCallback(() => {
    setMenuOpen(false)
    setNotice(null)
    setDeleteError(null)
    setDeleteOpen(true)
  }, [])

  const leaveDeleted = useCallback(() => {
    setDeleteOpen(false)
    // Back to the list the task was in; replace, since the task route is gone.
    navigate(tasksListPath({ archived: task.archived }), { replace: true })
  }, [navigate, task.archived])

  const confirmDelete = useCallback(async () => {
    setDeleting(true)
    setDeleteError(null)
    try {
      await deleteTask(task.id)
      setDeleting(false)
      leaveDeleted()
    } catch (err) {
      setDeleting(false)
      if (errorCode(err) === 'task_not_found') {
        // Already gone (another tab): leave as on success.
        leaveDeleted()
      } else {
        setDeleteError(errorMessage(err, 'The task could not be deleted.'))
      }
    }
  }, [leaveDeleted, task.id])

  // --- Keys -------------------------------------------------------------------

  // Single-key shortcuts while the menu is open (mirror the hint letters).
  useEffect(() => {
    if (!menuOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const key = e.key.toLowerCase()
      if (key === 'r') { e.preventDefault(); startRename() }
      else if (key === 'a' && canArchive && !archiving) { e.preventDefault(); void runArchive('archive') }
      else if (key === 'u' && task.archived && !archiving) { e.preventDefault(); void runArchive('unarchive') }
      else if (key === 'd') { e.preventDefault(); openDelete() }
      else if (key === 'escape') { setMenuOpen(false) }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [menuOpen, canArchive, archiving, task.archived, startRename, runArchive, openDelete])

  const titleLength = renameValue.length

  return (
    <header className="task-header">
      <div className="task-header-bar">
        <div className="task-header-unit">
          {renaming ? (
            <div className="task-header-rename">
              <input
                ref={renameInputRef}
                className="task-header-rename-input"
                value={renameValue}
                placeholder="Task title"
                maxLength={TASK_TITLE_MAX_CHARS}
                readOnly={renameSaving}
                aria-busy={renameSaving}
                aria-invalid={renameError !== null}
                aria-describedby="task-header-rename-count"
                onChange={(e) => {
                  setRenameValue(e.target.value)
                  setRenameError(null)
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); void submitRename('enter') }
                  else if (e.key === 'Escape') { e.preventDefault(); cancelRename() }
                }}
                onBlur={() => void submitRename('blur')}
                aria-label="Rename task"
              />
              <span
                id="task-header-rename-count"
                className={`task-header-rename-count${titleLength >= TASK_TITLE_MAX_CHARS ? ' at-cap' : ''}`}
              >
                {titleLength} / {TASK_TITLE_MAX_CHARS}
              </span>
            </div>
          ) : (
            <h1 className="task-header-heading">
              <button
                type="button"
                className={`task-header-title-button${menuOpen ? ' open' : ''}`}
                onClick={(e) => {
                  e.stopPropagation()
                  toggleMenu()
                }}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                title={task.title}
              >
                <span className="task-header-title">{task.title}</span>
                <span className="task-header-chevron" aria-hidden="true">
                  <ChevronDown size={16} />
                </span>
              </button>
            </h1>
          )}

          {menuOpen && (
            <div
              className="task-header-menu"
              role="menu"
              aria-label="Task actions"
              onClick={(e) => e.stopPropagation()}
            >
              <button type="button" className="task-header-menu-item" role="menuitem" onClick={startRename}>
                <Pencil size={16} className="task-header-menu-icon" aria-hidden="true" />
                <span>Rename</span>
                <span className="task-header-menu-key">R</span>
              </button>
              {task.archived ? (
                <button
                  type="button"
                  className="task-header-menu-item"
                  role="menuitem"
                  disabled={archiving}
                  onClick={() => void runArchive('unarchive')}
                  title="Move this task back to the active list"
                >
                  <ArchiveRestore size={16} className="task-header-menu-icon" aria-hidden="true" />
                  <span>Unarchive</span>
                  <span className="task-header-menu-key">U</span>
                </button>
              ) : (
                <button
                  type="button"
                  className="task-header-menu-item"
                  role="menuitem"
                  disabled={!canArchive || archiving}
                  onClick={() => void runArchive('archive')}
                  title={
                    activeRun
                      ? runActiveMessage(activeRun.seq)
                      : 'Hide this task from the sidebar; it takes no new runs until unarchived'
                  }
                >
                  <Archive size={16} className="task-header-menu-icon" aria-hidden="true" />
                  <span>Archive</span>
                  <span className="task-header-menu-key">A</span>
                </button>
              )}
              <div className="task-header-menu-separator" role="separator" />
              <button
                type="button"
                className="task-header-menu-item task-header-menu-item--danger"
                role="menuitem"
                onClick={openDelete}
              >
                <Trash2 size={16} className="task-header-menu-icon" aria-hidden="true" />
                <span>Delete</span>
                <span className="task-header-menu-key">D</span>
              </button>
            </div>
          )}
        </div>

        {task.archived && <span className="task-header-chip">Archived</span>}
      </div>

      {renaming && renameError && (
        <p className="task-header-rename-error" role="alert">
          {renameError}
        </p>
      )}

      {notice?.tone === 'status' && (
        <div className="task-header-notice" role="status" aria-live="polite">
          {notice.text}
        </div>
      )}
      {notice?.tone === 'error' && (
        <div className="task-header-error">
          <TaskNotice message={notice.text} onDismiss={() => setNotice(null)} />
        </div>
      )}

      <DocConfirmDialog
        isOpen={deleteOpen}
        title="Delete task?"
        confirmLabel="Delete"
        busyLabel="Deleting..."
        tone="danger"
        busy={deleting}
        error={deleteError}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteOpen(false)}
      >
        <p>
          <strong>{task.title}</strong> is deleted for good, with all its runs and their
          conversations, its brief and its files.
        </p>
        {activeRun && <p>Run {activeRun.seq} is working and will be stopped.</p>}
        <p>This cannot be undone.</p>
      </DocConfirmDialog>
    </header>
  )
}
