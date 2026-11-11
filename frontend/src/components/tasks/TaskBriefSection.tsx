import { useEffect, useRef, useState } from 'react'
import { History, Pencil } from 'lucide-react'
import type { Task, TaskBriefState } from '../../api/types'
import { formatRelativeTimestamp } from '../../utils/formatters'
import { TaskBriefEditor } from './TaskBriefEditor'
import { TaskBriefHistory } from './TaskBriefHistory'
import { TaskBriefMarkdown } from './TaskBriefMarkdown'
import { briefAttribution } from './taskBriefSource'
import { TaskTimeAgo } from './TaskPanelControls'
import { TaskPanelSection } from './TaskPanelSection'

type BriefMode = 'view' | 'edit' | 'history'

const EMPTY_TEXT =
  'No brief yet. The first run writes it: what the task is about, where things stand and what is left, so every later run starts from it.'

/**
 * The Brief section of the task panel (spec 8.3): the task's markdown
 * brief, written by runs and editable by the user, in one of three modes:
 *
 * - view: the rendered brief (TaskBriefMarkdown) under an attribution line
 *   ("Updated by run N", "Edited by you", "Restored from version n", and
 *   when, full time on hover), with Edit and History on the heading row.
 *   An empty brief (`brief_version` 0, or emptied since) explains that the
 *   first run writes it and offers "Write the brief".
 * - edit: TaskBriefEditor (save, conflicts and merge live there).
 * - history: TaskBriefHistory (versions, word diff, restore).
 *
 * The mode is one state, so Edit and History are mutually exclusive: their
 * buttons only show in view mode, and an open draft has to be saved or
 * cancelled before History can open. A save or restore merges the response
 * through `onBrief` (useTask's applyBrief) and returns to view; `onStale`
 * (useTask's refresh) runs after a conflict. Leaving edit or history puts
 * focus back on the control that opened it.
 */
export function TaskBriefSection({
  task,
  onBrief,
  onStale,
  collapsible,
}: {
  task: Task
  onBrief: (brief: TaskBriefState) => void
  onStale: () => void
  collapsible: boolean
}) {
  const [mode, setMode] = useState<BriefMode>('view')
  const editButtonRef = useRef<HTMLButtonElement>(null)
  const historyButtonRef = useRef<HTMLButtonElement>(null)
  // Which control takes focus once view mode renders again.
  const returnFocusRef = useRef<'edit' | 'history' | null>(null)

  useEffect(() => {
    if (mode !== 'view') return
    const target = returnFocusRef.current
    returnFocusRef.current = null
    if (target === 'edit') editButtonRef.current?.focus()
    else if (target === 'history') (historyButtonRef.current ?? editButtonRef.current)?.focus()
  }, [mode])

  const backToView = (from: 'edit' | 'history') => {
    returnFocusRef.current = from
    setMode('view')
  }

  const applied = (brief: TaskBriefState, from: 'edit' | 'history') => {
    onBrief(brief)
    backToView(from)
  }

  const hasBrief = task.brief.trim() !== ''
  const attribution = briefAttribution(task.brief_source, task.brief_run_seq)
  const updatedAt = task.brief_updated_at

  let summary: string
  if (mode === 'edit') summary = 'Editing'
  else if (mode === 'history') summary = 'Viewing history'
  else if (!hasBrief) summary = 'None yet'
  else {
    const parts = [attribution, updatedAt ? formatRelativeTimestamp(updatedAt) : null]
    summary = parts.filter(Boolean).join(' · ') || `Version ${task.brief_version}`
  }

  const actions =
    mode === 'view' ? (
      <>
        {hasBrief && (
          <button
            ref={editButtonRef}
            type="button"
            className="task-panel-button"
            onClick={() => setMode('edit')}
            aria-label="Edit brief"
          >
            <Pencil size={13} aria-hidden="true" />
            Edit
          </button>
        )}
        {task.brief_version > 0 && (
          <button
            ref={historyButtonRef}
            type="button"
            className="task-panel-button"
            onClick={() => setMode('history')}
            aria-label="Brief history"
          >
            <History size={13} aria-hidden="true" />
            History
          </button>
        )}
      </>
    ) : null

  let body
  if (mode === 'edit') {
    body = (
      <TaskBriefEditor
        task={task}
        onSaved={(brief) => applied(brief, 'edit')}
        onCancel={() => backToView('edit')}
        onStale={onStale}
      />
    )
  } else if (mode === 'history') {
    body = (
      <TaskBriefHistory
        task={task}
        onClose={() => backToView('history')}
        onRestored={(brief) => applied(brief, 'history')}
        onStale={onStale}
      />
    )
  } else if (hasBrief) {
    body = (
      <>
        {(attribution || updatedAt) && (
          <p className="task-panel-meta">
            {attribution}
            {attribution && updatedAt && <span aria-hidden="true"> · </span>}
            {updatedAt && <TaskTimeAgo iso={updatedAt} />}
          </p>
        )}
        <TaskBriefMarkdown taskId={task.id} content={task.brief} />
      </>
    )
  } else {
    body = (
      <div className="task-panel-empty">
        <p className="task-panel-empty-text">{EMPTY_TEXT}</p>
        <button
          ref={editButtonRef}
          type="button"
          className="task-panel-button"
          onClick={() => setMode('edit')}
        >
          <Pencil size={13} aria-hidden="true" />
          Write the brief
        </button>
      </div>
    )
  }

  return (
    <TaskPanelSection title="Brief" summary={summary} actions={actions} collapsible={collapsible}>
      {body}
    </TaskPanelSection>
  )
}
