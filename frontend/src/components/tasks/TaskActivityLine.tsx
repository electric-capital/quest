import { useEffect, useState } from 'react'
import { parseUTCTimestamp } from '../../utils/formatters'

/**
 * The compact activity line under an active run in the task thread (spec
 * 8.2): a working indicator (the chat's typing dots while the run works, a
 * still "needs you" dot while it waits), the label of what the run is doing
 * now, the elapsed time since the run started (ticking every second, `m:ss`
 * or `h:mm:ss`) and Stop. "View run" sits on the update's header row
 * (TaskRunEntry).
 *
 * The label is single-line with an ellipsis and the elapsed time has a fixed
 * tabular width, so a changing label never shifts the layout. On phones a
 * line with Stop (`has-controls`) wraps instead: the label takes the first
 * line, the time and Stop the second (TaskUpdates.css). The label is a
 * polite live region; the ticking time is not announced.
 *
 * Stop is optional: the optimistic "Starting..." entry of a run whose
 * conversation is not known yet has none. While `stopping` the
 * button reads "Stopping..." and is disabled; `stopError` (e.g. "Not
 * connected") shows after the time.
 */
export function TaskActivityLine({
  label,
  waiting = false,
  startedAt,
  onStop,
  stopping = false,
  stopError = null,
}: {
  label: string
  /** The run waits for the user (a question or a card) rather than working. */
  waiting?: boolean
  /** ISO timestamp the elapsed time counts from. */
  startedAt: string
  onStop?: () => void
  stopping?: boolean
  stopError?: string | null
}) {
  const elapsed = useElapsedSeconds(startedAt)

  return (
    <div
      className={`task-activity${waiting ? ' waiting' : ''}${onStop ? ' has-controls' : ''}`}
      data-testid="task-activity-line"
    >
      <span className="task-activity-indicator" aria-hidden="true">
        {waiting ? (
          <span className="task-activity-needs-you" />
        ) : (
          <span className="typing-indicator">
            <span className="dot"></span>
            <span className="dot"></span>
            <span className="dot"></span>
          </span>
        )}
      </span>
      <span className="task-activity-label" aria-live="polite" title={label}>
        {label}
      </span>
      <span className="task-activity-elapsed" title="Time since the run started">
        {formatElapsed(elapsed)}
      </span>
      {stopError && (
        <span className="task-activity-error" role="alert">
          {stopError}
        </span>
      )}
      {onStop && (
        <button
          type="button"
          className="task-activity-button"
          onClick={onStop}
          disabled={stopping}
        >
          {stopping ? 'Stopping...' : 'Stop'}
        </button>
      )}
    </div>
  )
}

/** Whole seconds since `startedAt`, re-rendering every second. */
function useElapsedSeconds(startedAt: string): number {
  const startMs = parseUTCTimestamp(startedAt).getTime()
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  if (Number.isNaN(startMs)) return 0
  return Math.max(0, Math.floor((now - startMs) / 1000))
}

/** `m:ss` under an hour, `h:mm:ss` from there. */
export function formatElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const hours = Math.floor(s / 3600)
  const minutes = Math.floor((s % 3600) / 60)
  const seconds = s % 60
  const ss = String(seconds).padStart(2, '0')
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${ss}`
  return `${minutes}:${ss}`
}
