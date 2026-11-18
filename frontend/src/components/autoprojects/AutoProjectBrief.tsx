import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { History, Pencil } from 'lucide-react'
import type { FileSource } from '../../api/fileApi'
import type { AutoProject, AutoProjectBriefState } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { AutoProjectBriefEditor } from './AutoProjectBriefEditor'
import { AutoProjectBriefMarkdown } from './AutoProjectBriefMarkdown'
import { briefAttribution, parseBriefSource } from './autoprojectBriefSource'
import { AutoProjectTimeAgo } from './AutoProjectBriefControls'

const EMPTY_TEXT = 'No brief yet. The first update writes it, or write one yourself.'

/**
 * The Brief card, the main content of the AutoProject page (AutoProjectPage's main
 * column, under the header): the AutoProject's markdown brief, written by runs
 * and editable by the user in place.
 *
 * View: the card header with "Brief", the byline ("Updated 3h ago · update
 * N", "Edited by you · 3h ago", "Restored from version n · 3h ago", full
 * time on hover) and Edit and History, then the rendered brief (AutoProjectBriefMarkdown;
 * `autoproject://` links open in the page's file viewer through `onOpenFile`). An
 * empty brief says the first run writes it and offers Edit. History opens
 * the version list, diff and restore in the detail panel beside the brief
 * (`onOpenHistory`; `historyOpen` marks the button expanded while it is
 * showing), and stays available while editing.
 *
 * Edit: AutoProjectBriefEditor in place of the rendered brief (save, conflicts
 * with the stale merge box, and the discard confirm live there). A save
 * merges the response through `onBrief` (useAutoProject's applyBrief) and returns
 * to view; `onStale` (useAutoProject's refresh) runs after a conflict. Leaving the
 * editor puts focus back on Edit.
 *
 * Phones (useIsMobile): a long brief is clamped to about a screenful with a
 * fade and a "Show full brief" / "Show less" toggle, so the files and the
 * updates below stay within reach. Whether the clamp hides anything is
 * measured, as for a long prompt: a brief that fits gets no toggle.
 */
export function AutoProjectBrief({
  autoproject,
  onBrief,
  onStale,
  historyOpen,
  onOpenHistory,
  onOpenFile,
}: {
  autoproject: AutoProject
  onBrief: (brief: AutoProjectBriefState) => void
  onStale: () => void
  historyOpen: boolean
  onOpenHistory: () => void
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
}) {
  const [editing, setEditing] = useState(false)
  const editButtonRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef(false)
  const headingId = useId()

  useEffect(() => {
    if (editing || !returnFocusRef.current) return
    returnFocusRef.current = false
    editButtonRef.current?.focus()
  }, [editing])

  const leaveEditor = () => {
    returnFocusRef.current = true
    setEditing(false)
  }

  const hasBrief = autoproject.brief.trim() !== ''
  const attribution = briefAttribution(autoproject.brief_source, autoproject.brief_run_seq)
  const byUpdate = parseBriefSource(autoproject.brief_source).kind === 'run'
  const updatedAt = autoproject.brief_updated_at

  let body
  if (editing) {
    body = (
      <AutoProjectBriefEditor
        autoproject={autoproject}
        onSaved={(brief) => {
          onBrief(brief)
          leaveEditor()
        }}
        onCancel={leaveEditor}
        onStale={onStale}
      />
    )
  } else if (hasBrief) {
    body = <AutoProjectBriefBody autoprojectId={autoproject.id} content={autoproject.brief} onOpenFile={onOpenFile} />
  } else {
    body = <p className="autoproject-card-empty">{EMPTY_TEXT}</p>
  }

  return (
    <section className="autoproject-card autoproject-brief" aria-labelledby={headingId}>
      <div className="autoproject-card-head">
        <h2 id={headingId} className="autoproject-card-title">
          Brief
        </h2>
        {hasBrief && !editing && (attribution || updatedAt) && (
          <p className="autoproject-card-meta">
            {byUpdate && updatedAt ? (
              <>
                Updated <AutoProjectTimeAgo iso={updatedAt} />
                {attribution && (
                  <>
                    <span aria-hidden="true"> · </span>
                    {attribution.toLowerCase()}
                  </>
                )}
              </>
            ) : (
              <>
                {attribution}
                {attribution && updatedAt && <span aria-hidden="true"> · </span>}
                {updatedAt && <AutoProjectTimeAgo iso={updatedAt} />}
              </>
            )}
          </p>
        )}
        <div className="autoproject-card-actions">
          {!editing && (
            <button
              ref={editButtonRef}
              type="button"
              className="autoproject-ctl-button"
              onClick={() => setEditing(true)}
              aria-label={hasBrief ? 'Edit brief' : 'Write the brief'}
            >
              <Pencil size={13} aria-hidden="true" />
              Edit
            </button>
          )}
          {autoproject.brief_version > 0 && (
            <button
              type="button"
              className="autoproject-ctl-button"
              onClick={onOpenHistory}
              aria-expanded={historyOpen}
              aria-label="Brief history"
            >
              <History size={13} aria-hidden="true" />
              History
            </button>
          )}
        </div>
      </div>
      <div className="autoproject-card-body">{body}</div>
    </section>
  )
}

/**
 * The rendered brief; on phones clamped while it is taller than the clamp
 * (AutoProjectPage.css `.autoproject-brief-body.clamp`) with a "Show full brief" toggle.
 */
function AutoProjectBriefBody({
  autoprojectId,
  content,
  onOpenFile,
}: {
  autoprojectId: string
  content: string
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
}) {
  const isMobile = useIsMobile()
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const bodyId = useId()

  // Measure while clamped; an expanded brief keeps the last answer.
  useLayoutEffect(() => {
    const el = bodyRef.current
    if (!isMobile || expanded || !el) return
    const check = () => setOverflows(el.scrollHeight > el.clientHeight + 1)
    check()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(check)
    observer.observe(el)
    const inner = el.firstElementChild
    if (inner) observer.observe(inner)
    return () => observer.disconnect()
  }, [isMobile, expanded, content])

  const clampable = isMobile && overflows
  const clamped = isMobile && !expanded
  return (
    <>
      <div
        ref={bodyRef}
        id={bodyId}
        className={`autoproject-brief-body${clamped ? ' clamp' : ''}${clamped && overflows ? ' clamped' : ''}`}
      >
        <AutoProjectBriefMarkdown autoprojectId={autoprojectId} content={content} onOpenFile={onOpenFile} />
      </div>
      {clampable && (
        <button
          type="button"
          className="autoproject-brief-toggle"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show less' : 'Show full brief'}
        </button>
      )}
    </>
  )
}
