import { X } from 'lucide-react'
import './AutoProjectNotice.css'

/**
 * A dismissible inline notice on the AutoProject page (role="alert"), e.g. the
 * New AutoProject page's "the AutoProject was created but its run did not start" message
 * carried to the AutoProject page in the router state. No modal anywhere.
 */
export function AutoProjectNotice({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  return (
    <div className="autoproject-notice" role="alert">
      <span className="autoproject-notice-text">{message}</span>
      {onDismiss && (
        <button
          type="button"
          className="autoproject-notice-dismiss"
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
 * The router state a page navigating to an AutoProject page may carry: a notice
 * the AutoProject page shows once (dismissing it replaces the history entry's
 * state, so a reload or Back does not show it again).
 */
export interface AutoProjectPageLocationState {
  autoprojectNotice?: string
}

export function autoprojectNoticeState(message: string): AutoProjectPageLocationState {
  return { autoprojectNotice: message }
}

/** The notice carried in a location's state, or null. */
export function readAutoProjectNotice(state: unknown): string | null {
  if (!state || typeof state !== 'object') return null
  const notice = (state as AutoProjectPageLocationState).autoprojectNotice
  return typeof notice === 'string' && notice ? notice : null
}
