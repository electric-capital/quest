import { forwardRef, useId } from 'react'
import './AutoProjectUpdates.css'
import type { FileSource } from '../../api/fileApi'
import type { AutoProjectRun } from '../../api/types'
import type { AutoProjectRunsView } from '../../hooks/useAutoProjectRuns'
import { AutoProjectActivityLine } from './AutoProjectActivityLine'
import { AutoProjectRunEntry, AutoProjectRunRequest, type AutoProjectRunActivityView, type AutoProjectRunCard } from './AutoProjectRunEntry'
import { isActiveRunStatus } from './autoprojectThreadComposer'
import type { AutoProjectUpdatesOrder } from './autoprojectUpdatesOrder'

/** A run being started from the composer, shown until its run is in the runs list. */
export interface PendingStart {
  key: number
  prompt: string
  startedAt: string
  /** Known once the start resolved. */
  conversationId: string | null
}

/**
 * The Updates card of the AutoProject page's main column, under the brief and the
 * AutoProject files: every run is an update (AutoProjectRunEntry), separated by dividers,
 * in the viewer's `order` (oldest or most recent first, the sort control in
 * the card header, shown once there are two updates; AutoProjectMainColumn keeps
 * the choice). The active update and
 * the optimistic entry of a run being started from the composer (the
 * prompt and a "Starting" line, until the runs list holds it) sit at the
 * newest end.
 *
 * Runs load newest first in pages (useAutoProjectRuns); while older pages exist a
 * "Show earlier updates" button sits at the OLD end of the list (top when
 * oldest first, bottom when recent first; AutoProjectMainColumn keeps the scroll
 * position when a page is prepended). An AutoProject without updates shows a short
 * empty state; a first load that failed shows its error with Retry.
 *
 * This component only renders: the page state (pending start, the cards
 * kept for the active run, the activity, answering, which run is open in
 * the detail panel) and every action come from AutoProjectMainColumn. The section
 * element is forwarded so the column can find the updates for its scroll
 * logic (`.autoproject-run`, in DOM order).
 */
export const AutoProjectUpdates = forwardRef<
  HTMLElement,
  {
    autoprojectId: string
    archived: boolean
    runs: AutoProjectRunsView
    pendingStart: PendingStart | null
    keptCards: Record<string, AutoProjectRunCard[]>
    activeRunId: string | null
    activity: AutoProjectRunActivityView | null
    answeringHandleId: string | null
    openRunConversationId: string | null
    /** The AutoProject's update count (the card header's number). */
    count: number
    order: AutoProjectUpdatesOrder
    onOrderChange: (order: AutoProjectUpdatesOrder) => void
    onLoadOlder: () => void
    onStop: (conversationId: string) => void
    onQuestionAnswered: () => void
    onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
    onDelete: (run: AutoProjectRun) => void
    onView: (conversationId: string) => void
  }
>(function AutoProjectUpdates(
  {
    autoprojectId,
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
    body = <p className="autoproject-updates-note">Loading updates...</p>
  } else if (runs.error && runList.length === 0) {
    body = (
      <div className="autoproject-updates-note" role="alert">
        <p className="autoproject-updates-note-text">{runs.error.message || 'Failed to load the updates.'}</p>
        <button type="button" className="autoproject-ctl-button" onClick={() => void runs.refresh()}>
          Retry
        </button>
      </div>
    )
  } else if (empty) {
    body = archived ? (
      <div className="autoproject-updates-empty">
        <p className="autoproject-updates-empty-text">No updates yet.</p>
      </div>
    ) : (
      <div className="autoproject-updates-empty">
        <p className="autoproject-updates-empty-title">No updates yet</p>
        <p className="autoproject-updates-empty-text">
          Each prompt you send is worked on with a fresh context that reads the brief, and its
          result shows here as an update.
        </p>
      </div>
    )
  } else {
    const entries = runList.map((run) => (
      <AutoProjectRunEntry
        key={run.id}
        autoprojectId={autoprojectId}
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
      <article key="pending" className="autoproject-run pending" aria-label="New update">
        <div className="autoproject-run-meta">
          <span className="autoproject-run-seq">New update</span>
        </div>
        <AutoProjectRunRequest prompt={pendingStart.prompt} />
        <AutoProjectActivityLine label="Starting" startedAt={pendingStart.startedAt} />
      </article>
    )
    body = (
      <div className="autoproject-updates-list">
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
      className={`autoproject-updates-earlier autoproject-updates-earlier--${order}`}
      onClick={onLoadOlder}
      disabled={runs.loadingOlder}
    >
      {runs.loadingOlder ? 'Loading earlier updates...' : 'Show earlier updates'}
    </button>
  )

  return (
    <section ref={ref} className="autoproject-card autoproject-updates" aria-labelledby={headingId}>
      <div className="autoproject-card-head">
        <h2 id={headingId} className="autoproject-card-title">
          Updates
        </h2>
        {count > 0 && <span className="autoproject-card-meta">{count}</span>}
        {runList.length + (pendingStart ? 1 : 0) > 1 && (
        <div className="autoproject-card-actions">
          <div className="autoproject-updates-sort" role="group" aria-label="Sort updates">
            <button
              type="button"
              className="autoproject-updates-sort-option"
              aria-pressed={order === 'oldest'}
              onClick={() => onOrderChange('oldest')}
            >
              Oldest first
            </button>
            <button
              type="button"
              className="autoproject-updates-sort-option"
              aria-pressed={order === 'recent'}
              onClick={() => onOrderChange('recent')}
            >
              Recent first
            </button>
          </div>
        </div>
        )}
      </div>
      <div className="autoproject-card-body autoproject-updates-body">
        {order === 'oldest' && earlier}
        {body}
        {order === 'recent' && earlier}
      </div>
    </section>
  )
})
