import { useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { ChevronLeft } from 'lucide-react'
import './TaskRunView.css'
import type { Task, TaskRun } from '../../api/types'
import { useTask } from '../../hooks/useTask'
import { useTaskRun } from '../../hooks/useTaskRun'
import { formatTimestamp } from '../../utils/formatters'
import { taskThreadPath } from '../../utils/tasksRoute'
import { ChatPanel } from '../ChatPanel'
import { TaskLoadError, TaskNotFound, TaskViewLoading, TaskViewState } from './TaskViewState'
import { TaskRunError } from './TaskRunError'
import { TaskRunViewContext, type TaskRunViewContextValue } from './TaskRunViewContext'

/** The run's state in the header ("Run 3 · Needs you"). */
function taskRunStatusLabel(run: Pick<TaskRun, 'status' | 'handoff_missing'>): string {
  switch (run.status) {
    case 'running':
      return 'Working'
    case 'waiting':
      return 'Needs you'
    case 'completed':
      return run.handoff_missing ? 'Completed without handoff' : 'Completed'
    case 'failed':
      return 'Failed'
    case 'stopped':
      return 'Stopped'
  }
}

/**
 * The run view (/tasks/<id>/runs/<conversation id>, spec 8.4): one run's
 * transcript, ChatPanel for the run's conversation, read-only through its
 * `task` origin ("This is one run of a task. Continue in the task."), under
 * this view's own header in place of the conversation title unit: "Back to
 * task", the task title, and the run's number, live status and start time.
 * The run's Chat Files card is the RightPanel beside the view (App.tsx; the
 * workspace drawer in MobileShell), with copy and move to and from the task
 * files.
 *
 * The header's meta line says why a run ended without its handoff: a
 * failed run's error (TaskRunError, one line with "Show details"), and
 * "Stopped by you." for a stopped one.
 *
 * `onRunResolved` tells the host whether the route's run resolved (the
 * ChatPanel is up), so App.tsx renders the RightPanel, and MobileShell
 * offers the workspace drawer, only beside a real run: never while loading
 * and never beside a not-found or failed lookup. The host keys what it
 * stores by the route, so a report for the previous run never counts.
 *
 * The run is looked up among the task's runs by its conversation
 * (useTaskRun) and re-read on every `task_changed` of the task, so the
 * status moves live and a deleted run turns into the not-found state. The
 * ChatPanel mounts only once the conversation is known to be a run of THIS
 * task: anything else (another task's run, a plain chat, a deleted run)
 * shows a not-found notice with "Back to task".
 *
 * TaskRunViewContext hands the transcript's `task_question` rows the task
 * id (their option buttons answer through it) and the run's questions as
 * last listed. Approval and connect cards resolve through their own
 * endpoints, as in any chat. The run conversation never becomes the app's
 * active conversation (see the route note in App.tsx).
 */
export interface TaskRunViewProps {
  taskId: string
  conversationId: string
  /** Called with whether the run resolved (true once the transcript shows), on every change. */
  onRunResolved?: (resolved: boolean) => void
}

export function TaskRunView({ taskId, conversationId, onRunResolved }: TaskRunViewProps) {
  const { task, loading, error, notFound, refresh, changeSeq } = useTask(taskId)
  const lookup = useTaskRun(taskId, conversationId, changeSeq)
  const run = lookup.run

  // Both hooks drop their data synchronously when the ids change, so this is
  // false on the first render of a new route.
  const resolved = task !== null && run !== null
  useEffect(() => {
    onRunResolved?.(resolved)
  }, [onRunResolved, resolved])

  const runViewCtx = useMemo<TaskRunViewContextValue | null>(
    () => (run ? { taskId, questions: run.questions } : null),
    [taskId, run],
  )

  if (loading || (task && lookup.loading)) {
    return <TaskViewLoading text="Loading run..." />
  }

  if (notFound) {
    return <TaskNotFound />
  }

  if (!task) {
    return <TaskLoadError message={error?.message || 'Failed to load the task.'} onRetry={refresh} />
  }

  if (lookup.notFound) {
    return (
      <TaskViewState>
        <p className="task-view-state-text">This run does not exist, was deleted, or belongs to another task.</p>
        <Link to={taskThreadPath(taskId)} className="task-view-state-link">
          Back to task
        </Link>
      </TaskViewState>
    )
  }

  if (!run || !runViewCtx) {
    return (
      <TaskLoadError message={lookup.error?.message || 'Failed to load the run.'} onRetry={lookup.refresh} />
    )
  }

  return (
    <TaskRunViewContext.Provider value={runViewCtx}>
      <div className="task-run-view">
        <ChatPanel
          conversationId={conversationId}
          header={<TaskRunViewHeader taskId={taskId} task={task} run={run} />}
        />
      </div>
    </TaskRunViewContext.Provider>
  )
}

/**
 * "Back to task", the task title, then "Run N", the live status and the
 * start time; under them a failed run's error or a stopped run's line.
 */
function TaskRunViewHeader({ taskId, task, run }: { taskId: string; task: Task; run: TaskRun }) {
  return (
    <header className="task-run-view-header" aria-label={`Run ${run.seq} of ${task.title}`}>
      <Link to={taskThreadPath(taskId)} className="task-run-view-back">
        <ChevronLeft size={16} aria-hidden="true" />
        Back to task
      </Link>
      <h1 className="task-run-view-title" title={task.title}>
        {task.title}
      </h1>
      <div className="task-run-view-meta">
        <span className="task-run-view-seq">Run {run.seq}</span>
        <span className={`task-run-view-status ${run.status}`} role="status">
          {taskRunStatusLabel(run)}
        </span>
        <time className="task-run-view-time" dateTime={run.started_at}>
          {formatTimestamp(run.started_at)}
        </time>
      </div>
      {run.status === 'failed' && <TaskRunError error={run.error} className="task-run-view-error" />}
      {run.status === 'stopped' && <p className="task-run-view-ended">Stopped by you.</p>}
    </header>
  )
}
