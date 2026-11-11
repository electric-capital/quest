import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { History, Pencil } from 'lucide-react'
import type { FileSource } from '../../api/fileApi'
import type { Task, TaskBriefState } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { TaskBriefEditor } from './TaskBriefEditor'
import { TaskBriefMarkdown } from './TaskBriefMarkdown'
import { briefAttribution } from './taskBriefSource'
import { TaskTimeAgo } from './TaskControls'

const EMPTY_TEXT = 'No brief yet. The first run writes it, or write one yourself.'

/**
 * The brief, the main content of the task page (TaskPage's main column,
 * under the instructions row): the task's markdown brief, written by runs
 * and editable by the user in place.
 *
 * View: a "Brief" heading row with the attribution ("Updated by run N",
 * "Edited by you", "Restored from version n", and when, full time on
 * hover) and Edit and History, then the rendered brief (TaskBriefMarkdown;
 * `task://` links open in the page's file viewer through `onOpenFile`). An
 * empty brief says the first run writes it and offers Edit. History opens
 * the version list, diff and restore in the detail panel beside the brief
 * (`onOpenHistory`; `historyOpen` marks the button expanded while it is
 * showing), and stays available while editing.
 *
 * Edit: TaskBriefEditor in place of the rendered brief (save, conflicts
 * with the stale merge box, and the discard confirm live there). A save
 * merges the response through `onBrief` (useTask's applyBrief) and returns
 * to view; `onStale` (useTask's refresh) runs after a conflict. Leaving the
 * editor puts focus back on Edit.
 *
 * Phones (useIsMobile): a long brief is clamped to about a screenful with a
 * fade and a "Show full brief" / "Show less" toggle, so the files and the
 * updates below stay within reach. Whether the clamp hides anything is
 * measured, as for a long prompt: a brief that fits gets no toggle.
 */
export function TaskBrief({
  task,
  onBrief,
  onStale,
  historyOpen,
  onOpenHistory,
  onOpenFile,
}: {
  task: Task
  onBrief: (brief: TaskBriefState) => void
  onStale: () => void
  historyOpen: boolean
  onOpenHistory: () => void
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
}) {
  const [editing, setEditing] = useState(false)
  const editButtonRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef(false)
  const headingId = useId()

  useEffect(() => {
    if (editing || !returnFocusRef.current) return
    returnFocusRef.current = false
    editButtonRef.current?.focus()
  }, [editing])

  const leaveEditor = () => {
    returnFocusRef.current = true
    setEditing(false)
  }

  const hasBrief = task.brief.trim() !== ''
  const attribution = briefAttribution(task.brief_source, task.brief_run_seq)
  const updatedAt = task.brief_updated_at

  let body
  if (editing) {
    body = (
      <TaskBriefEditor
        task={task}
        onSaved={(brief) => {
          onBrief(brief)
          leaveEditor()
        }}
        onCancel={leaveEditor}
        onStale={onStale}
      />
    )
  } else if (hasBrief) {
    body = <TaskBriefBody taskId={task.id} content={task.brief} onOpenFile={onOpenFile} />
  } else {
    body = <p className="task-brief-empty">{EMPTY_TEXT}</p>
  }

  return (
    <section className="task-section task-brief" aria-labelledby={headingId}>
      <div className="task-section-head">
        <h2 id={headingId} className="task-section-heading">
          Brief
        </h2>
        {hasBrief && !editing && (attribution || updatedAt) && (
          <p className="task-section-meta">
            {attribution}
            {attribution && updatedAt && <span aria-hidden="true"> · </span>}
            {updatedAt && <TaskTimeAgo iso={updatedAt} />}
          </p>
        )}
        <div className="task-section-actions">
          {!editing && (
            <button
              ref={editButtonRef}
              type="button"
              className="task-ctl-button"
              onClick={() => setEditing(true)}
              aria-label={hasBrief ? 'Edit brief' : 'Write the brief'}
            >
              <Pencil size={13} aria-hidden="true" />
              Edit
            </button>
          )}
          {task.brief_version > 0 && (
            <button
              type="button"
              className="task-ctl-button"
              onClick={onOpenHistory}
              aria-expanded={historyOpen}
              aria-label="Brief history"
            >
              <History size={13} aria-hidden="true" />
              History
            </button>
          )}
        </div>
      </div>
      {body}
    </section>
  )
}

/**
 * The rendered brief; on phones clamped while it is taller than the clamp
 * (TaskPage.css `.task-brief-body.clamp`) with a "Show full brief" toggle.
 */
function TaskBriefBody({
  taskId,
  content,
  onOpenFile,
}: {
  taskId: string
  content: string
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
}) {
  const isMobile = useIsMobile()
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const bodyId = useId()

  // Measure while clamped; an expanded brief keeps the last answer.
  useLayoutEffect(() => {
    const el = bodyRef.current
    if (!isMobile || expanded || !el) return
    const check = () => setOverflows(el.scrollHeight > el.clientHeight + 1)
    check()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(check)
    observer.observe(el)
    const inner = el.firstElementChild
    if (inner) observer.observe(inner)
    return () => observer.disconnect()
  }, [isMobile, expanded, content])

  const clampable = isMobile && overflows
  const clamped = isMobile && !expanded
  return (
    <>
      <div
        ref={bodyRef}
        id={bodyId}
        className={`task-brief-body${clamped ? ' clamp' : ''}${clamped && overflows ? ' clamped' : ''}`}
      >
        <TaskBriefMarkdown taskId={taskId} content={content} onOpenFile={onOpenFile} />
      </div>
      {clampable && (
        <button
          type="button"
          className="task-brief-toggle"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show less' : 'Show full brief'}
        </button>
      )}
    </>
  )
}
