import { useCallback, useState, type ReactNode } from 'react'
import './TaskViewState.css'

/**
 * Centered loading / not-found / error notices shared by the Quest Tasks
 * views (TaskPage, TaskRunView, TasksListView), styled like the doc and
 * table viewers' states. Not a placeholder: later phases keep using it.
 */
export function TaskViewState({ children }: { children: ReactNode }) {
  return <div className="task-view-state">{children}</div>
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
