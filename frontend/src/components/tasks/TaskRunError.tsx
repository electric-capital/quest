import { useId, useState } from 'react'
import './TaskRunError.css'

/** Longest summary line shown before the full text is asked for. */
const SUMMARY_MAX_CHARS = 160

/**
 * One line that stands for a run's error: the first non-empty line of the
 * text, cut to SUMMARY_MAX_CHARS on a word boundary where one is near.
 * Returns the line and whether anything was left out.
 */
export function summarizeRunError(error: string): { line: string; truncated: boolean } {
  const text = error.trim()
  const firstLine = text.split('\n').find((l) => l.trim() !== '')?.trim() ?? ''
  let line = firstLine
  if (line.length > SUMMARY_MAX_CHARS) {
    const cut = line.slice(0, SUMMARY_MAX_CHARS)
    const space = cut.lastIndexOf(' ')
    line = `${(space > SUMMARY_MAX_CHARS * 0.6 ? cut.slice(0, space) : cut).trimEnd()}...`
  }
  return { line, truncated: line !== text }
}

/**
 * A failed run's error (its update on the task page and its run panel): "Run failed: <one
 * line>", and when the raw text (often a long provider error) is longer
 * than that line, a "Show details" toggle that reveals the whole text in a
 * bounded, scrollable block. `role="alert"` is left to the caller's
 * context: the updates render finished runs, so it stays a plain line.
 */
export function TaskRunError({ error, className = '' }: { error: string | null; className?: string }) {
  const [open, setOpen] = useState(false)
  const detailsId = useId()
  const text = error?.trim() || ''
  if (!text) {
    return <p className={`task-run-error ${className}`}>Failed: unknown error</p>
  }
  const { line, truncated } = summarizeRunError(text)
  return (
    <div className={`task-run-error ${className}`}>
      <p className="task-run-error-line">
        <span>Failed: {line}</span>
        {truncated && (
          <button
            type="button"
            className="task-run-error-toggle"
            aria-expanded={open}
            aria-controls={detailsId}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? 'Hide details' : 'Show details'}
          </button>
        )}
      </p>
      {truncated && open && (
        <pre id={detailsId} className="task-run-error-details">
          {text}
        </pre>
      )}
    </div>
  )
}
