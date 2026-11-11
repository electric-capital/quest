import { useCallback, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { tasksListPath } from '../../utils/tasksRoute'
import './TaskViewState.css'

/**
 * Centered loading / not-found / error notices shared by the Quest Tasks
 * views (TaskPage, TaskRunView, TasksListView), styled like the doc and
 * table viewers' states. Not a placeholder: later phases keep using it.
 *
 * `delayed` (the loading states, TaskViewLoading) fades the notice in only
 * after a short delay (TaskViewState.css), so a fast load shows an empty
 * frame instead of flashing the text; errors and not-found show at once.
 */
export function TaskViewState({ children, delayed = false }: { children: ReactNode; delayed?: boolean }) {
  return <div className={`task-view-state${delayed ? ' task-view-state--delayed' : ''}`}>{children}</div>
}

/** A view's loading notice ("Loading task..."), shown only once the load is slow. */
export function TaskViewLoading({ text }: { text: string }) {
  return (
    <TaskViewState delayed>
      <p className="task-view-state-text" role="status">{text}</p>
    </TaskViewState>
  )
}

/** A task that does not exist, was deleted or is someone else's, with a link to the tasks list. */
export function TaskNotFound() {
  return (
    <TaskViewState>
      <p className="task-view-state-text">This task does not exist or was deleted.</p>
      <Link to={tasksListPath()} className="task-view-state-link">
        All tasks
      </Link>
    </TaskViewState>
  )
}

/**
 * A failed load with a Retry button that calls `onRetry` (the hook's
 * `refresh`) and is disabled while it runs.
 */
export function TaskLoadError({
  message,
  onRetry,
}: {
  message: string
  onRetry: () => Promise<void>
}) {
  const [retrying, setRetrying] = useState(false)
  const retry = useCallback(async () => {
    setRetrying(true)
    try {
      await onRetry()
    } finally {
      setRetrying(false)
    }
  }, [onRetry])

  return (
    <TaskViewState>
      <p className="task-view-state-text" role="alert">{message}</p>
      <button
        type="button"
        className="task-view-retry"
        onClick={() => void retry()}
        disabled={retrying}
      >
        {retrying ? 'Retrying...' : 'Retry'}
      </button>
    </TaskViewState>
  )
}
