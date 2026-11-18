import { memo, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { MoreHorizontal, PanelRight, Trash2 } from 'lucide-react'
import type {
  ActionRequestMessage as ActionRequestMessageType,
  ConnectRequestMessage as ConnectRequestMessageType,
  AutoProjectRun,
} from '../../api/types'
import type { FileSource } from '../../api/fileApi'
import { useClampOverflow } from '../../hooks/useClampOverflow'
import { formatTimestamp } from '../../utils/formatters'
import { ActionRequestMessage } from '../ActionRequestMessage'
import { ConnectRequestMessage } from '../ConnectRequestMessage'
import { MarkdownWorkspaceContext } from '../Message'
import { AutoProjectActivityLine } from './AutoProjectActivityLine'
import { AutoProjectBriefMarkdown } from './AutoProjectBriefMarkdown'
import { playArrival } from './autoprojectArrival'
import { AutoProjectQuestionCard } from './AutoProjectQuestionCard'
import { AutoProjectRunError } from './AutoProjectRunError'
import { isActiveRunStatus } from './autoprojectComposerState'

/** An approval or connect card of a run, as the runs page lists them. */
export type AutoProjectRunCard = ActionRequestMessageType | ConnectRequestMessageType

/** Stable identity of a card across refreshes (request id / wait handle id). */
export function autoprojectRunCardKey(card: AutoProjectRunCard): string {
  return card.type === 'action_request' ? `ar:${card.request_id}` : `cr:${card.wait_handle_id}`
}

/** What the active run's activity line shows; null for every other run. */
export interface AutoProjectRunActivityView {
  label: string
  waiting: boolean
  stopping: boolean
  stopError: string | null
}

/**
 * One update of the AutoProject page: one run (AutoProjectUpdates lists them
 * in the viewer's sort order), top to bottom:
 *
 * - a header: "Update N", its start time, "View details" (opens the run's
 *   transcript in the page's detail panel; "Close details" while it is the
 *   one showing there, `isOpen`, which also marks the update) and, on a
 *   finished run, the row menu ("Delete update", confirmed by the page);
 * - the prompt as the request (AutoProjectRunRequest: "You asked", a quiet left
 *   rule, clamped with a "Show full prompt" toggle when it is long);
 * - its questions (AutoProjectQuestionCard, oldest first; answered / cancelled
 *   ones keep showing on finished runs);
 * - the approval and connect cards the page keeps for it (`cards`: the
 *   open ones plus, while the run is active, those resolved since they were
 *   first seen, which flip themselves to their resolved state);
 * - for the active run the activity line, else the outcome: the `result` as
 *   plain body text in the column, no bubble, and the handoff-missing,
 *   failed (AutoProjectRunError: one line plus "Show details") or stopped line.
 *   The handoff notes are in the run's details (AutoProjectRunPanel).
 *
 * Arrivals the page observed live (`claimArrival`, autoprojectArrival.ts):
 * a new update's row opens in when it mounts, and the outcome opens in from
 * the activity line's height when the run finishes, with a short highlight
 * on a result. Each is claimed once, so a remount never replays it.
 *
 * Everything is inside a MarkdownWorkspaceContext for the RUN's
 * conversation and its AutoProject (the result's AutoProjectBriefMarkdown sets the
 * same), so workspace images and relative links in the
 * prompt, cards and result resolve against that run's workspace,
 * `autoproject://` references against the AutoProject's, and both open through the
 * page's one file viewer (`onOpenFile`, told the file's space).
 */
export const AutoProjectRunEntry = memo(function AutoProjectRunEntry({
  autoprojectId,
  run,
  cards,
  activity,
  answeringHandleId,
  onStop,
  onQuestionAnswered,
  onOpenFile,
  onDelete,
  isOpen,
  onView,
  claimArrival,
}: {
  autoprojectId: string
  run: AutoProjectRun
  cards: AutoProjectRunCard[]
  activity: AutoProjectRunActivityView | null
  /** The question the composer is answering right now (its card is locked). */
  answeringHandleId: string | null
  onStop: (conversationId: string) => void
  onQuestionAnswered: () => void
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
  onDelete: (run: AutoProjectRun) => void
  /** This run's transcript is the one showing in the detail panel. */
  isOpen: boolean
  /** View details / Close details: the page toggles the run's panel. */
  onView: (conversationId: string) => void
  /** True once for an arrival the page observed live (`row:<id>`, `result:<id>`). */
  claimArrival: (key: string) => boolean
}) {
  const conversationId = run.conversation_id
  const workspaceCtx = useMemo(
    () => ({ conversationId, autoprojectId, onOpenFile }),
    [conversationId, autoprojectId, onOpenFile],
  )
  const finished = activity === null && !isActiveRunStatus(run.status)
  const runId = run.id
  const hasResult = Boolean(run.result?.trim())

  // Arrivals: the row on mount, the outcome when it replaces the activity
  // line (from that line's last height, so nothing jumps).
  const articleRef = useRef<HTMLElement>(null)
  const activityRef = useRef<HTMLDivElement>(null)
  const outcomeRef = useRef<HTMLDivElement>(null)
  const activityHeightRef = useRef(0)
  const showsActivity = activity !== null
  useLayoutEffect(() => {
    if (showsActivity) activityHeightRef.current = activityRef.current?.offsetHeight ?? 0
  })
  useLayoutEffect(() => {
    const el = articleRef.current
    if (el && claimArrival(`row:${runId}`)) playArrival(el)
  }, [claimArrival, runId])
  useLayoutEffect(() => {
    const el = outcomeRef.current
    if (showsActivity || !el || !claimArrival(`result:${runId}`)) return
    playArrival(el, { fromHeight: activityHeightRef.current, highlight: hasResult })
  }, [showsActivity, claimArrival, runId, hasResult])

  return (
    <MarkdownWorkspaceContext.Provider value={workspaceCtx}>
      <article
        ref={articleRef}
        className={`autoproject-run${isOpen ? ' is-open' : ''}`}
        data-run-id={run.id}
        data-conversation-id={conversationId}
        aria-label={`Update ${run.seq}`}
        aria-current={isOpen ? 'true' : undefined}
      >
        <div className="autoproject-run-meta">
          <span className="autoproject-run-seq">Update {run.seq}</span>
          <span className="autoproject-run-time">{formatTimestamp(run.started_at)}</span>
          <button
            type="button"
            className="autoproject-run-view"
            aria-pressed={isOpen}
            onClick={() => onView(conversationId)}
          >
            <PanelRight size={14} aria-hidden="true" />
            {isOpen ? 'Close details' : 'View details'}
          </button>
          {finished && <AutoProjectRunMenu seq={run.seq} onDelete={() => onDelete(run)} />}
        </div>

        <AutoProjectRunRequest prompt={run.prompt} />

        {run.questions.map((question) => (
          <AutoProjectQuestionCard
            key={question.handle_id}
            autoprojectId={autoprojectId}
            question={question}
            disabled={answeringHandleId === question.handle_id}
            onAnswered={onQuestionAnswered}
          />
        ))}

        {cards.map((card) =>
          card.type === 'action_request' ? (
            <ActionRequestMessage
              key={autoprojectRunCardKey(card)}
              message={card}
              conversationId={conversationId}
            />
          ) : (
            <ConnectRequestMessage key={autoprojectRunCardKey(card)} message={card} />
          ),
        )}

        {activity ? (
          <div ref={activityRef}>
            <AutoProjectActivityLine
              label={activity.label}
              waiting={activity.waiting}
              startedAt={run.started_at}
              onStop={() => onStop(conversationId)}
              stopping={activity.stopping}
              stopError={activity.stopError}
            />
          </div>
        ) : (
          <div ref={outcomeRef} className="autoproject-run-arrival">
            <AutoProjectRunOutcome autoprojectId={autoprojectId} run={run} onOpenFile={onOpenFile} />
          </div>
        )}
      </article>
    </MarkdownWorkspaceContext.Provider>
  )
})

/** A finished run's result and state line. */
function AutoProjectRunOutcome({
  autoprojectId,
  run,
  onOpenFile,
}: {
  autoprojectId: string
  run: AutoProjectRun
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
}) {
  const result = run.result?.trim() ? run.result : null

  return (
    <div className="autoproject-run-outcome">
      {run.status === 'failed' && <AutoProjectRunError error={run.error} className="autoproject-run-outcome-error" />}
      {run.status === 'stopped' && <p className="autoproject-run-state">Stopped.</p>}
      {run.handoff_missing && (
        <p className="autoproject-run-state">
          This update ended without a handoff; its final text is shown as the result.
        </p>
      )}
      {result && (
        <AutoProjectBriefMarkdown
          autoprojectId={autoprojectId}
          conversationId={run.conversation_id}
          content={result}
          className="autoproject-run-result"
          onOpenFile={onOpenFile}
        />
      )}
      {!result && run.status === 'completed' && (
        <p className="autoproject-run-state">This update finished without a result.</p>
      )}
    </div>
  )
}

/** A prompt longer than this many characters or lines is shown clamped. */
const PROMPT_CLAMP_CHARS = 600
const PROMPT_CLAMP_LINES = 12

function isLongPrompt(prompt: string): boolean {
  if (prompt.length > PROMPT_CLAMP_CHARS) return true
  let lines = 1
  for (let i = 0; i < prompt.length; i += 1) {
    if (prompt[i] === '\n' && ++lines > PROMPT_CLAMP_LINES) return true
  }
  return false
}

/**
 * The run's prompt as the update's request: a small "You asked" label over
 * the text (plain, pre-wrapped, as the user typed it), set off from the
 * result below by a quiet left rule and the secondary text colour, all
 * left-anchored in the column (AutoProjectUpdates.css). Also the optimistic entry
 * of a run being started (AutoProjectUpdates).
 *
 * A long prompt (isLongPrompt) is clamped to about eight lines with a fade
 * (`.autoproject-run-request.clamped`) and a "Show full prompt" / "Show less"
 * toggle under it. Whether the clamp actually hides anything is measured
 * (the column's width changes with the viewport): when the whole prompt
 * fits, no fade and no toggle.
 */
export function AutoProjectRunRequest({ prompt }: { prompt: string }) {
  const long = isLongPrompt(prompt)
  const [expanded, setExpanded] = useState(false)
  const textRef = useRef<HTMLDivElement>(null)
  const contentId = useId()
  // Measured while clamped; an expanded prompt keeps the last answer.
  const overflows = useClampOverflow(textRef, long && !expanded, prompt, true)

  const clampable = long && overflows
  const clamped = clampable && !expanded
  return (
    <div
      className={`autoproject-run-request${long && !expanded ? ' clamp' : ''}${clamped ? ' clamped' : ''}`}
    >
      <p className="autoproject-run-request-label">You asked</p>
      <div ref={textRef} id={contentId} className="autoproject-run-request-text">
        {prompt}
      </div>
      {clampable && (
        <button
          type="button"
          className="autoproject-run-request-toggle"
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show less' : 'Show full prompt'}
        </button>
      )}
    </div>
  )
}

/**
 * The finished run's "more" menu (one item, "Delete update"), following the
 * conversation header's menu: the toggle stops propagation and any outside
 * click closes it; Escape closes it and returns focus to the toggle.
 */
function AutoProjectRunMenu({ seq, onDelete }: { seq: number; onDelete: () => void }) {
  const [open, setOpen] = useState(false)
  const toggleRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const close = () => setOpen(false)
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      setOpen(false)
      toggleRef.current?.focus()
    }
    document.addEventListener('click', close)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('click', close)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="autoproject-run-menu-wrapper">
      <button
        ref={toggleRef}
        type="button"
        className={`autoproject-run-menu-button${open ? ' open' : ''}`}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Update ${seq} options`}
        title="More"
      >
        <MoreHorizontal size={16} aria-hidden="true" />
      </button>
      {open && (
        <div className="autoproject-run-menu" role="menu" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            className="autoproject-run-menu-item danger"
            role="menuitem"
            autoFocus
            onClick={() => {
              setOpen(false)
              onDelete()
            }}
          >
            <Trash2 size={15} className="autoproject-run-menu-icon" aria-hidden="true" />
            <span>Delete update</span>
          </button>
        </div>
      )}
    </div>
  )
}
