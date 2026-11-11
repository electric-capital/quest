import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { Pencil } from 'lucide-react'
import { TASK_INSTRUCTIONS_MAX_CHARS, updateTask } from '../../api/tasksApi'
import type { Task } from '../../api/types'
import {
  TaskCharCounter,
  TaskDiscardConfirm,
  isOverCap,
  taskSaveErrorMessage,
  taskTextLength,
} from './TaskControls'

const ABOUT_TEXT =
  'Standing rules every run of this task follows, like tone, sources to use or things to avoid. Only you write them; runs never change them.'
const CHANGED_NOTE =
  'The instructions were changed elsewhere while you were editing. Saving replaces them with your text.'

/** An edit in progress: the draft and the instructions it started from. */
interface InstructionsEdit {
  draft: string
  base: string
}

/**
 * The instructions in the task page's detail panel (TaskPage wraps this
 * body in TaskDetailPanel, opened from the instructions row above the
 * brief): the task's standing rules as plain text (pre-wrap), user-written
 * only, under a line saying what they are for.
 *
 * Edit (or "Add instructions", or opening the panel from the row's "Add
 * instructions", `startEditing`) opens a textarea (focused, caret after the
 * text) with an `n / 8,000` counter (TaskCharCounter: no truncation,
 * warning near the cap, Save disabled over it), Save and Cancel. Save
 * PATCHes `instructions`, hands the returned task to `onSaved` (useTask's
 * applyDetail) and goes back to the text; any error (400
 * `invalid_instructions` included) shows inline and the editor stays open.
 * Cancel, or Escape while the draft is unchanged, closes the editor and
 * puts focus back on the control that opened it. Escape over a changed
 * draft asks first (TaskDiscardConfirm in place of the Cancel / Save row).
 *
 * The panel itself must not close over a changed draft: the page tracks
 * `onDirtyChange` and, when the panel is asked to close or switch to
 * another view (Escape elsewhere in the panel, its close button, View run,
 * History), sets `closePending` instead; the same discard row then shows,
 * and Discard calls `onConfirmClose` (the page carries out the close),
 * Keep editing `onCancelClose`.
 *
 * The draft is never replaced from outside: if the task's instructions
 * change while editing (another tab), a quiet note says that saving
 * replaces them.
 */
export function TaskInstructionsPanel({
  task,
  onSaved,
  startEditing,
  onDirtyChange,
  closePending,
  onConfirmClose,
  onCancelClose,
}: {
  task: Task
  /** Merge the Task returned by the PATCH into the page's view. */
  onSaved: (task: Task) => void
  /** Open straight into the editor (the row's "Add instructions"). */
  startEditing: boolean
  /** Whether an unsaved, changed draft is open. */
  onDirtyChange: (dirty: boolean) => void
  /** The page was asked to close the panel over a changed draft. */
  closePending: boolean
  onConfirmClose: () => void
  onCancelClose: () => void
}) {
  const [edit, setEdit] = useState<InstructionsEdit | null>(() =>
    startEditing ? { draft: task.instructions, base: task.instructions } : null,
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // The Edit / Add instructions control, to take focus back after the editor closes.
  const openerRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef(false)
  const ids = useId()
  const textareaId = `${ids}-textarea`
  const counterId = `${ids}-counter`
  const overId = `${ids}-over`
  const errorId = `${ids}-error`

  const editing = edit !== null
  useEffect(() => {
    if (editing) {
      // Open with the caret after the text, ready to add to it.
      const el = textareaRef.current
      el?.focus()
      el?.setSelectionRange(el.value.length, el.value.length)
    } else if (returnFocusRef.current) {
      returnFocusRef.current = false
      openerRef.current?.focus()
    }
  }, [editing])

  const dirty = edit !== null && edit.draft !== edit.base
  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange(false), [onDirtyChange])

  const startEdit = () => {
    setError(null)
    setConfirmDiscard(false)
    setEdit({ draft: task.instructions, base: task.instructions })
  }

  const close = () => {
    returnFocusRef.current = true
    setEdit(null)
    setError(null)
    setConfirmDiscard(false)
  }

  const keepEditing = () => {
    setConfirmDiscard(false)
    if (closePending) onCancelClose()
    textareaRef.current?.focus()
  }

  const discard = () => {
    if (closePending) onConfirmClose()
    else close()
  }

  const length = edit ? taskTextLength(edit.draft) : 0
  const over = isOverCap(length, TASK_INSTRUCTIONS_MAX_CHARS)
  const unchanged = edit !== null && edit.draft.trim() === task.instructions
  const canSave = edit !== null && !saving && !over && !unchanged

  const save = async () => {
    if (!edit || !canSave) return
    setSaving(true)
    setError(null)
    try {
      const updated = await updateTask(task.id, { instructions: edit.draft })
      setSaving(false)
      onSaved(updated)
      close()
    } catch (err) {
      setSaving(false)
      setError(taskSaveErrorMessage(err, 'Failed to save the instructions.'))
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Escape' || !edit || saving) return
    e.preventDefault()
    if (edit.draft === edit.base) close()
    else setConfirmDiscard(true)
  }

  const changedUnderneath = edit !== null && !saving && task.instructions !== edit.base

  let body
  if (edit) {
    body = (
      <div className="task-ctl-editor">
        <label htmlFor={textareaId} className="task-ctl-sr-only">
          Edit instructions
        </label>
        {changedUnderneath && (
          <p className="task-ctl-note" role="status">
            {CHANGED_NOTE}
          </p>
        )}
        <textarea
          ref={textareaRef}
          id={textareaId}
          className="task-ctl-textarea"
          value={edit.draft}
          rows={12}
          placeholder="For example: Answer in English. Prefer primary sources. Never email anyone."
          readOnly={saving}
          aria-busy={saving}
          aria-invalid={over || error !== null}
          aria-describedby={`${counterId} ${overId}${error ? ` ${errorId}` : ''}`}
          onChange={(e) => {
            setEdit({ ...edit, draft: e.target.value })
            setConfirmDiscard(false)
          }}
          onKeyDown={onKeyDown}
        />
        <TaskCharCounter
          id={counterId}
          overId={overId}
          length={length}
          cap={TASK_INSTRUCTIONS_MAX_CHARS}
        />
        {error && (
          <p id={errorId} className="task-ctl-error" role="alert">
            {error}
          </p>
        )}
        {confirmDiscard || (closePending && dirty) ? (
          <TaskDiscardConfirm onDiscard={discard} onKeepEditing={keepEditing} />
        ) : (
          <div className="task-ctl-editor-actions">
            <button type="button" className="task-ctl-button" onClick={close} disabled={saving}>
              Cancel
            </button>
            <button
              type="button"
              className="task-ctl-button task-ctl-button--primary"
              onClick={() => void save()}
              disabled={!canSave}
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        )}
      </div>
    )
  } else if (task.instructions) {
    body = (
      <>
        <p className="task-ctl-text">{task.instructions}</p>
        <div className="task-ctl-editor-actions task-instructions-panel-actions">
          <button ref={openerRef} type="button" className="task-ctl-button" onClick={startEdit}>
            <Pencil size={13} aria-hidden="true" />
            Edit
          </button>
        </div>
      </>
    )
  } else {
    body = (
      <div className="task-ctl-empty">
        <p className="task-ctl-empty-text">No instructions yet.</p>
        <button ref={openerRef} type="button" className="task-ctl-button" onClick={startEdit}>
          Add instructions
        </button>
      </div>
    )
  }

  return (
    <div className="task-instructions-panel">
      <p className="task-ctl-hint">{ABOUT_TEXT}</p>
      {body}
    </div>
  )
}
