import { ApiClientError } from '../../api/request'
import { taskErrorDetail } from '../../api/tasksApi'
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters'

/**
 * Small shared pieces of the task panel (TaskPanel.css):
 *
 * - `taskTextLength`: the length the server checks a brief or instructions
 *   text against: after stripping, in code points (Python `len`), so an
 *   emoji counts once and trailing blank lines do not count.
 * - `TaskCharCounter`: the `n / cap` counter under an editor. It never
 *   truncates (a paste or a merged text may go over); it turns to a warning
 *   tone at 90% of the cap and an error tone over it, with an "N characters
 *   over the limit" line in a polite live region. Editors disable Save while
 *   `isOverCap`.
 * - `TaskTimeAgo`: a relative time with the full local time on hover.
 * - `taskPanelErrorMessage`: the inline message for a failed save or
 *   restore, mapping 400 `brief_too_long` (its `cap` / `actual` extras) to a
 *   sentence and anything else to the server's message or a fallback.
 */

/** Share of the cap from which the counter takes its warning tone. */
const NEAR_CAP_RATIO = 0.9

export function taskTextLength(text: string): number {
  const stripped = text.trim()
  let count = stripped.length
  for (let i = 0; i < stripped.length; i += 1) {
    const code = stripped.charCodeAt(i)
    // A high surrogate followed by a low one is one code point.
    if (code >= 0xd800 && code <= 0xdbff && i + 1 < stripped.length) {
      const next = stripped.charCodeAt(i + 1)
      if (next >= 0xdc00 && next <= 0xdfff) {
        count -= 1
        i += 1
      }
    }
  }
  return count
}

export function isOverCap(length: number, cap: number): boolean {
  return length > cap
}

export function TaskCharCounter({
  id,
  overId,
  length,
  cap,
}: {
  /** Id of the counter, for the textarea's aria-describedby. */
  id: string
  /** Id of the over-the-limit line, for the textarea's aria-describedby. */
  overId: string
  length: number
  cap: number
}) {
  const over = length - cap
  const tone = over > 0 ? 'over' : length >= cap * NEAR_CAP_RATIO ? 'near' : 'ok'
  return (
    <div className="task-panel-counter-row">
      <p id={overId} className="task-panel-over-cap" aria-live="polite">
        {over > 0 ? `${over.toLocaleString()} characters over the limit` : ''}
      </p>
      <span id={id} className={`task-panel-counter task-panel-counter--${tone}`}>
        {length.toLocaleString()} / {cap.toLocaleString()}
        <span className="task-panel-sr-only"> characters</span>
      </span>
    </div>
  )
}

export function TaskTimeAgo({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} title={parseUTCTimestamp(iso).toLocaleString()}>
      {formatRelativeTimestamp(iso)}
    </time>
  )
}

export function taskPanelErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiClientError && err.errorCode === 'brief_too_long') {
    const detail = taskErrorDetail(err)
    const cap = typeof detail?.cap === 'number' ? detail.cap : null
    const actual = typeof detail?.actual === 'number' ? detail.actual : null
    if (cap !== null && actual !== null && actual > cap) {
      return `The brief is ${(actual - cap).toLocaleString()} characters over the limit of ${cap.toLocaleString()}.`
    }
    return 'The brief is over the length limit.'
  }
  return err instanceof Error && err.message ? err.message : fallback
}
