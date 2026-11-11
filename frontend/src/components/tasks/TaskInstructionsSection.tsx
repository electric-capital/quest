import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { Pencil } from 'lucide-react'
import { TASK_INSTRUCTIONS_MAX_CHARS, updateTask } from '../../api/tasksApi'
import type { Task } from '../../api/types'
import {
  TaskCharCounter,
  isOverCap,
  taskPanelErrorMessage,
  taskTextLength,
} from './TaskPanelControls'
import { TaskPanelSection } from './TaskPanelSection'

const EMPTY_TEXT =
  'Standing rules every run of this task follows, like tone, sources to use or things to avoid. Only you write them; runs never change them.'
const CHANGED_NOTE =
  'The instructions were changed elsewhere while you were editing. Saving replaces them with your text.'

/** An edit in progress: the draft and the instructions it started from. */
interface InstructionsEdit {
  draft: string
  base: string
}

/**
 * The Instructions section of the task panel (spec 8.3): the task's
 * standing rules as plain text (pre-wrap), user-written only.
 *
 * Edit opens a textarea (focused) with an `n / 8,000` counter
 * (TaskCharCounter: no truncation, warning near the cap, Save disabled over
 * it), Save and Cancel. Save PATCHes `instructions` and hands the returned
 * task to `onSaved` (useTask's applyDetail); any error (400
 * `invalid_instructions` included) shows inline under the textarea and the
 * editor stays open. Cancel, or Escape while the draft is unchanged,
 * closes the editor and puts focus back on the control that opened it.
 *
 * The draft is never replaced from outside: if the task's instructions
 * change while editing (another tab), a quiet note says that saving
 * replaces them. With no instructions the section explains what they are
 * for and offers "Add instructions".
 */
export function TaskInstructionsSection({
  task,
  onSaved,
  collapsible,
}: {
  task: Task
  /** Merge the Task returned by the PATCH into the page's view. */
  onSaved: (task: Task) => void
  collapsible: boolean
}) {
  const [edit, setEdit] = useState<InstructionsEdit | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
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
      textareaRef.current?.focus()
    } else if (returnFocusRef.current) {
      returnFocusRef.current = false
      openerRef.current?.focus()
    }
  }, [editing])

  const startEdit = () => {
    setError(null)
    setEdit({ draft: task.instructions, base: task.instructions })
  }

  const close = () => {
    returnFocusRef.current = true
    setEdit(null)
    setError(null)
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
      setError(taskPanelErrorMessage(err, 'Failed to save the instructions.'))
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape' && edit && edit.draft === edit.base && !saving) {
      e.preventDefault()
      close()
    }
  }

  const changedUnderneath = edit !== null && !saving && task.instructions !== edit.base

  let summary: string
  if (editing) summary = 'Editing'
  else if (task.instructions) summary = task.instructions.split('\n', 1)[0]
  else summary = 'None yet'

  const actions =
    !editing && task.instructions ? (
      <button ref={openerRef} type="button" className="task-panel-button" onClick={startEdit}>
        <Pencil size={13} aria-hidden="true" />
        Edit
      </button>
    ) : null

  let body
  if (edit) {
    body = (
      <div className="task-panel-editor">
        <label htmlFor={textareaId} className="task-panel-sr-only">
          Edit instructions
        </label>
        {changedUnderneath && (
          <p className="task-panel-note" role="status">
            {CHANGED_NOTE}
          </p>
        )}
        <textarea
          ref={textareaRef}
          id={textareaId}
          className="task-panel-textarea"
          value={edit.draft}
          rows={8}
          placeholder="For example: Answer in English. Prefer primary sources. Never email anyone."
          readOnly={saving}
          aria-busy={saving}
          aria-invalid={over || error !== null}
          aria-describedby={`${counterId} ${overId}${error ? ` ${errorId}` : ''}`}
          onChange={(e) => setEdit({ ...edit, draft: e.target.value })}
          onKeyDown={onKeyDown}
        />
        <TaskCharCounter
          id={counterId}
          overId={overId}
          length={length}
          cap={TASK_INSTRUCTIONS_MAX_CHARS}
        />
        {error && (
          <p id={errorId} className="task-panel-error" role="alert">
            {error}
          </p>
        )}
        <div className="task-panel-editor-actions">
          <button type="button" className="task-panel-button" onClick={close} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="task-panel-button task-panel-button--primary"
            onClick={() => void save()}
            disabled={!canSave}
          >
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
    )
  } else if (task.instructions) {
    body = <p className="task-panel-text">{task.instructions}</p>
  } else {
    body = (
      <div className="task-panel-empty">
        <p className="task-panel-empty-text">{EMPTY_TEXT}</p>
        <button ref={openerRef} type="button" className="task-panel-button" onClick={startEdit}>
          Add instructions
        </button>
      </div>
    )
  }

  return (
    <TaskPanelSection title="Instructions" summary={summary} actions={actions} collapsible={collapsible}>
      {body}
    </TaskPanelSection>
  )
}
