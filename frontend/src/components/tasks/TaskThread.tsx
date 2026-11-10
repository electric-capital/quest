import { Link } from 'react-router-dom'
import type { Task, TaskRun } from '../../api/types'
import type { TaskRunsView } from '../../hooks/useTaskRuns'
import { taskRunPath } from '../../utils/tasksRoute'

/**
 * PLACEHOLDER thread of the task page, replaced by B3 (prompts, activity
 * line, question / approval / connect cards, results, the composer). For
 * now it proves the useTaskRuns data path: the loaded runs oldest first as
 * "#seq status" plus the prompt's first line, "Load older runs" while older
 * pages exist, and the active run's state. Styles in TaskPage.css.
 */
export function TaskThread({
  taskId,
  task,
  runs,
}: {
  taskId: string
  task: Task
  runs: TaskRunsView
}) {
  const activeRun = task.active_run

  return (
    <section className="task-thread" aria-label="Runs">
      {runs.hasOlder && (
        <button
          type="button"
          className="task-thread-older"
          onClick={runs.loadOlder}
          disabled={runs.loadingOlder}
        >
          {runs.loadingOlder ? 'Loading older runs...' : 'Load older runs'}
        </button>
      )}

      {runs.loading && runs.runs.length === 0 ? (
        <p className="task-thread-note">Loading runs...</p>
      ) : runs.error && runs.runs.length === 0 ? (
        <p className="task-thread-note" role="alert">
          {runs.error.message || 'Failed to load the runs.'}{' '}
          <button type="button" className="task-thread-inline-button" onClick={() => void runs.refresh()}>
            Retry
          </button>
        </p>
      ) : runs.runs.length === 0 ? (
        <p className="task-thread-note">No runs yet.</p>
      ) : (
        <ol className="task-thread-runs">
          {runs.runs.map((run) => (
            <li key={run.id} className="task-thread-run">
              <span className="task-thread-run-seq">#{run.seq}</span>
              <span className="task-thread-run-status">{run.status}</span>
              <span className="task-thread-run-prompt">{promptFirstLine(run)}</span>
              <Link to={taskRunPath(taskId, run.conversation_id)} className="task-thread-run-open">
                Open run
              </Link>
            </li>
          ))}
        </ol>
      )}

      {activeRun && (
        <p className="task-thread-active" role="status">
          {activeRun.status === 'waiting'
            ? `Run ${activeRun.seq} is waiting for you`
            : `Run ${activeRun.seq} is working`}
        </p>
      )}
    </section>
  )
}

/** The prompt's first non-empty line, as plain text. */
function promptFirstLine(run: TaskRun): string {
  const line = run.prompt.split('\n').find((l) => l.trim() !== '')
  return line ? line.trim() : '(empty prompt)'
}
