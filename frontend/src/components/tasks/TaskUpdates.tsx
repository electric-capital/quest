import { forwardRef, useId } from 'react'
import './TaskUpdates.css'
import type { FileSource } from '../../api/fileApi'
import type { TaskRun } from '../../api/types'
import type { TaskRunsView } from '../../hooks/useTaskRuns'
import { TaskActivityLine } from './TaskActivityLine'
import { TaskRunEntry, TaskRunRequest, type TaskRunActivityView, type TaskRunCard } from './TaskRunEntry'
import { isActiveRunStatus } from './taskThreadComposer'

/** A run being started from the composer, shown until its run is in the runs list. */
export interface PendingStart {
  key: number
  prompt: string
  startedAt: string
  /** Known once the start resolved. */
  conversationId: string | null
}

/**
 * The "Updates" section of the task page's main column, under the brief and
 * the task files: every run is an update (TaskRunEntry), OLDEST FIRST, the
 * active run last with its activity line and Stop, then the optimistic
 * entry of a run being started from the composer (the prompt and a
 * "Starting" line) until the runs list holds it.
 *
 * Runs load newest first in pages (useTaskRuns); while older pages exist a
 * "Show earlier updates" button at the top of the section loads the next
 * one (TaskMainColumn keeps the scroll position across the prepended page).
 * A task without runs shows the "No runs yet" state; a first load that
 * failed shows its error with Retry.
 *
 * This component only renders: the page state (pending start, the cards
 * kept for the active run, the activity, answering, which run is open in
 * the detail panel) and every action come from TaskMainColumn. The section
 * element is forwarded so the column can find the updates for its scroll
 * logic (`.task-run`, the last one being the newest update).
 */
export const TaskUpdates = forwardRef<
  HTMLElement,
  {
    taskId: string
    archived: boolean
    runs: TaskRunsView
    pendingStart: PendingStart | null
    keptCards: Record<string, TaskRunCard[]>
    activeRunId: string | null
    activity: TaskRunActivityView | null
    answeringHandleId: string | null
    openRunConversationId: string | null
    onLoadOlder: () => void
    onStop: (conversationId: string) => void
    onQuestionAnswered: () => void
    onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
    onDelete: (run: TaskRun) => void
    onView: (conversationId: string) => void
  }
>(function TaskUpdates(
  {
    taskId,
    archived,
    runs,
    pendingStart,
    keptCards,
    activeRunId,
    activity,
    answeringHandleId,
    openRunConversationId,
    onLoadOlder,
    onStop,
    onQuestionAnswered,
    onOpenFile,
    onDelete,
    onView,
  },
  ref,
) {
  const headingId = useId()
  const runList = runs.runs
  const empty = runList.length === 0 && pendingStart === null

  let body
  if (runs.loading && runList.length === 0) {
    body = <p className="task-updates-note">Loading updates...</p>
  } else if (runs.error && runList.length === 0) {
    body = (
      <div className="task-updates-note" role="alert">
        <p className="task-updates-note-text">{runs.error.message || 'Failed to load the updates.'}</p>
        <button type="button" className="task-ctl-button" onClick={() => void runs.refresh()}>
          Retry
        </button>
      </div>
    )
  } else if (empty) {
    body = archived ? (
      <div className="task-updates-empty">
        <p className="task-updates-empty-text">No updates yet.</p>
      </div>
    ) : (
      <div className="task-updates-empty">
        <p className="task-updates-empty-title">No updates yet</p>
        <p className="task-updates-empty-text">
          Each prompt you send is worked on with a fresh context that reads the brief, and its
          result shows here as an update.
        </p>
      </div>
    )
  } else {
    body = (
      <div className="task-updates-list">
        {runList.map((run) => (
          <TaskRunEntry
            key={run.id}
            taskId={taskId}
            run={run}
            cards={isActiveRunStatus(run.status) ? keptCards[run.id] ?? run.open_requests : run.open_requests}
            activity={run.id === activeRunId ? activity : null}
            answeringHandleId={answeringHandleId}
            onStop={onStop}
            onQuestionAnswered={onQuestionAnswered}
            onOpenFile={onOpenFile}
            onDelete={onDelete}
            isOpen={run.conversation_id === openRunConversationId}
            onView={onView}
          />
        ))}
        {pendingStart && (
          <article className="task-run pending" aria-label="New update">
            <div className="task-run-meta">
              <span className="task-run-seq">New update</span>
            </div>
            <TaskRunRequest prompt={pendingStart.prompt} />
            <TaskActivityLine label="Starting" startedAt={pendingStart.startedAt} />
          </article>
        )}
      </div>
    )
  }

  return (
    <section ref={ref} className="task-section task-updates" aria-labelledby={headingId}>
      <div className="task-section-head">
        <h2 id={headingId} className="task-section-heading">
          Updates
        </h2>
      </div>
      {runs.hasOlder && runList.length > 0 && (
        <button
          type="button"
          className="task-updates-earlier"
          onClick={onLoadOlder}
          disabled={runs.loadingOlder}
        >
          {runs.loadingOlder ? 'Loading earlier updates...' : 'Show earlier updates'}
        </button>
      )}
      {body}
    </section>
  )
})
