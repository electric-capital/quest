import { X } from 'lucide-react'
import './TaskNotice.css'

/**
 * A dismissible inline notice on the task page (role="alert"), e.g. the
 * New Task page's "the task was created but its run did not start" message
 * carried to the thread in the router state. No modal anywhere.
 */
export function TaskNotice({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  return (
    <div className="task-notice" role="alert">
      <span className="task-notice-text">{message}</span>
      {onDismiss && (
        <button
          type="button"
          className="task-notice-dismiss"
          onClick={onDismiss}
          aria-label="Dismiss"
          title="Dismiss"
        >
          <X size={14} aria-hidden="true" />
        </button>
      )}
    </div>
  )
}

/**
 * The router state a page navigating to a task thread may carry: a notice
 * the task page shows once (dismissing it replaces the history entry's
 * state, so a reload or Back does not show it again).
 */
export interface TaskPageLocationState {
  taskNotice?: string
}

export function taskNoticeState(message: string): TaskPageLocationState {
  return { taskNotice: message }
}

/** The notice carried in a location's state, or null. */
export function readTaskNotice(state: unknown): string | null {
  if (!state || typeof state !== 'object') return null
  const notice = (state as TaskPageLocationState).taskNotice
  return typeof notice === 'string' && notice ? notice : null
}
