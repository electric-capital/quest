import { useCallback, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { autoprojectsListPath } from '../../utils/autoprojectsRoute'
import './AutoProjectViewState.css'

/**
 * Centered loading / not-found / error notices shared by the AutoProjects
 * views (AutoProjectPage, AutoProjectsListView), styled like the doc and
 * table viewers' states. Not a placeholder: later phases keep using it.
 *
 * `delayed` (the loading states, AutoProjectViewLoading) fades the notice in only
 * after a short delay (AutoProjectViewState.css), so a fast load shows an empty
 * frame instead of flashing the text; errors and not-found show at once.
 */
export function AutoProjectViewState({ children, delayed = false }: { children: ReactNode; delayed?: boolean }) {
  return <div className={`autoproject-view-state${delayed ? ' autoproject-view-state--delayed' : ''}`}>{children}</div>
}

/** A view's loading notice ("Loading AutoProject..."), shown only once the load is slow. */
export function AutoProjectViewLoading({ text }: { text: string }) {
  return (
    <AutoProjectViewState delayed>
      <p className="autoproject-view-state-text" role="status">{text}</p>
    </AutoProjectViewState>
  )
}

/** An AutoProject that does not exist, was deleted or is someone else's, with a link to the AutoProjects list. */
export function AutoProjectNotFound() {
  return (
    <AutoProjectViewState>
      <p className="autoproject-view-state-text">This AutoProject does not exist or was deleted.</p>
      <Link to={autoprojectsListPath()} className="autoproject-view-state-link">
        All AutoProjects
      </Link>
    </AutoProjectViewState>
  )
}

/**
 * A failed load with a Retry button that calls `onRetry` (the hook's
 * `refresh`) and is disabled while it runs.
 */
export function AutoProjectLoadError({
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
    <AutoProjectViewState>
      <p className="autoproject-view-state-text" role="alert">{message}</p>
      <button
        type="button"
        className="autoproject-view-retry"
        onClick={() => void retry()}
        disabled={retrying}
      >
        {retrying ? 'Retrying...' : 'Retry'}
      </button>
    </AutoProjectViewState>
  )
}
