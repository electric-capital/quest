import { forwardRef, useId } from 'react'
import './TaskUpdates.css'
import type { FileSource } from '../../api/fileApi'
import type { TaskRun } from '../../api/types'
import type { TaskRunsView } from '../../hooks/useTaskRuns'
import { TaskActivityLine } from './TaskActivityLine'
import { TaskRunEntry, TaskRunRequest, type TaskRunActivityView, type TaskRunCard } from './TaskRunEntry'
import { isActiveRunStatus } from './taskThreadComposer'
import type { TaskUpdatesOrder } from './taskUpdatesOrder'

/** A run being started from the composer, shown until its run is in the runs list. */
export interface PendingStart {
  key: number
  prompt: string
  startedAt: string
  /** Known once the start resolved. */
  conversationId: string | null
}

/**
 * The Updates card of the task page's main column, under the brief and the
 * task files: every run is an update (TaskRunEntry), separated by dividers,
 * in the viewer's `order` (oldest or most recent first, the sort control in
 * the card header, shown once there are two updates; TaskMainColumn keeps
 * the choice). The active update and
 * the optimistic entry of a run being started from the composer (the
 * prompt and a "Starting" line, until the runs list holds it) sit at the
 * newest end.
 *
 * Runs load newest first in pages (useTaskRuns); while older pages exist a
 * "Show earlier updates" button sits at the OLD end of the list (top when
 * oldest first, bottom when recent first; TaskMainColumn keeps the scroll
 * position when a page is prepended). A task without updates shows a short
 * empty state; a first load that failed shows its error with Retry.
 *
 * This component only renders: the page state (pending start, the cards
 * kept for the active run, the activity, answering, which run is open in
 * the detail panel) and every action come from TaskMainColumn. The section
 * element is forwarded so the column can find the updates for its scroll
 * logic (`.task-run`, in DOM order).
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
    /** The task's update count (the card header's number). */
    count: number
    order: TaskUpdatesOrder
    onOrderChange: (order: TaskUpdatesOrder) => void
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
    count,
    order,
    onOrderChange,
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
    const entries = runList.map((run) => (
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
    ))
    const pending = pendingStart && (
      <article key="pending" className="task-run pending" aria-label="New update">
        <div className="task-run-meta">
          <span className="task-run-seq">New update</span>
        </div>
        <TaskRunRequest prompt={pendingStart.prompt} />
        <TaskActivityLine label="Starting" startedAt={pendingStart.startedAt} />
      </article>
    )
    body = (
      <div className="task-updates-list">
        {order === 'oldest' ? (
          <>
            {entries}
            {pending}
          </>
        ) : (
          <>
            {pending}
            {entries.reverse()}
          </>
        )}
      </div>
    )
  }

  const earlier = runs.hasOlder && runList.length > 0 && (
    <button
      type="button"
      className={`task-updates-earlier task-updates-earlier--${order}`}
      onClick={onLoadOlder}
      disabled={runs.loadingOlder}
    >
      {runs.loadingOlder ? 'Loading earlier updates...' : 'Show earlier updates'}
    </button>
  )

  return (
    <section ref={ref} className="task-card task-updates" aria-labelledby={headingId}>
      <div className="task-card-head">
        <h2 id={headingId} className="task-card-title">
          Updates
        </h2>
        {count > 0 && <span className="task-card-meta">{count}</span>}
        {runList.length + (pendingStart ? 1 : 0) > 1 && (
        <div className="task-card-actions">
          <div className="task-updates-sort" role="group" aria-label="Sort updates">
            <button
              type="button"
              className="task-updates-sort-option"
              aria-pressed={order === 'oldest'}
              onClick={() => onOrderChange('oldest')}
            >
              Oldest first
            </button>
            <button
              type="button"
              className="task-updates-sort-option"
              aria-pressed={order === 'recent'}
              onClick={() => onOrderChange('recent')}
            >
              Recent first
            </button>
          </div>
        </div>
        )}
      </div>
      <div className="task-card-body task-updates-body">
        {order === 'oldest' && earlier}
        {body}
        {order === 'recent' && earlier}
      </div>
    </section>
  )
})
