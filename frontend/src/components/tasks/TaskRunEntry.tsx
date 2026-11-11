import { memo, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ChevronRight, MoreHorizontal, PanelRight, Trash2 } from 'lucide-react'
import type {
  ActionRequestMessage as ActionRequestMessageType,
  ConnectRequestMessage as ConnectRequestMessageType,
  TaskRun,
} from '../../api/types'
import type { FileSource } from '../../api/fileApi'
import { formatTimestamp } from '../../utils/formatters'
import { ActionRequestMessage } from '../ActionRequestMessage'
import { ConnectRequestMessage } from '../ConnectRequestMessage'
import { MarkdownWorkspaceContext, markdownComponents } from '../Message'
import { RichMarkdown } from '../RichMarkdown'
import { TaskActivityLine } from './TaskActivityLine'
import { TaskQuestionCard } from './TaskQuestionCard'
import { TaskRunError } from './TaskRunError'
import { isActiveRunStatus } from './taskThreadComposer'

/** An approval or connect card of a run, as the runs page lists them. */
export type TaskRunCard = ActionRequestMessageType | ConnectRequestMessageType

/** Stable identity of a card across refreshes (request id / wait handle id). */
export function taskRunCardKey(card: TaskRunCard): string {
  return card.type === 'action_request' ? `ar:${card.request_id}` : `cr:${card.wait_handle_id}`
}

/** What the active run's activity line shows; null for every other run. */
export interface TaskRunActivityView {
  label: string
  waiting: boolean
  stopping: boolean
  stopError: string | null
}

/**
 * One update of the task page: one run (TaskUpdates lists them oldest
 * first), top to bottom:
 *
 * - a header: "Run N", its start time, "View run" (opens the run's
 *   transcript in the page's detail panel; "Close run" while it is the one
 *   showing there, `isOpen`, which also marks the update) and, on a
 *   finished run, the row menu ("Delete entry", confirmed by the page);
 * - the prompt as the request (TaskRunRequest: "You asked", a quiet left
 *   rule, clamped with a "Show full prompt" toggle when it is long);
 * - its questions (TaskQuestionCard, oldest first; answered / cancelled
 *   ones keep showing on finished runs);
 * - the approval and connect cards the page keeps for it (`cards`: the
 *   open ones plus, while the run is active, those resolved since they were
 *   first seen, which flip themselves to their resolved state);
 * - for the active run the activity line, else the outcome: the `result` as
 *   plain body text in the column, no bubble (with a "Handoff notes"
 *   disclosure for the notes),
 *   the handoff-missing, failed (TaskRunError: one line plus "Show
 *   details") or stopped line.
 *
 * Everything is inside a MarkdownWorkspaceContext for the RUN's
 * conversation and its task, so workspace images and relative links in the
 * prompt, cards, result and notes resolve against that run's workspace,
 * `task://` references against the task's, and both open through the
 * page's one file viewer (`onOpenFile`, told the file's space).
 */
export const TaskRunEntry = memo(function TaskRunEntry({
  taskId,
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
}: {
  taskId: string
  run: TaskRun
  cards: TaskRunCard[]
  activity: TaskRunActivityView | null
  /** The question the composer is answering right now (its card is locked). */
  answeringHandleId: string | null
  onStop: (conversationId: string) => void
  onQuestionAnswered: () => void
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
  onDelete: (run: TaskRun) => void
  /** This run's transcript is the one showing in the detail panel. */
  isOpen: boolean
  /** View details / Close details: the page toggles the run's panel. */
  onView: (conversationId: string) => void
}) {
  const conversationId = run.conversation_id
  const workspaceCtx = useMemo(
    () => ({ conversationId, taskId, onOpenFile }),
    [conversationId, taskId, onOpenFile],
  )
  const finished = activity === null && !isActiveRunStatus(run.status)

  return (
    <MarkdownWorkspaceContext.Provider value={workspaceCtx}>
      <article
        className={`task-run${isOpen ? ' is-open' : ''}`}
        data-run-id={run.id}
        data-conversation-id={conversationId}
        aria-label={`Update ${run.seq}`}
        aria-current={isOpen ? 'true' : undefined}
      >
        <div className="task-run-meta">
          <span className="task-run-seq">Update {run.seq}</span>
          <span className="task-run-time">{formatTimestamp(run.started_at)}</span>
          <button
            type="button"
            className="task-run-view"
            aria-pressed={isOpen}
            onClick={() => onView(conversationId)}
          >
            <PanelRight size={14} aria-hidden="true" />
            {isOpen ? 'Close details' : 'View details'}
          </button>
          {finished && <TaskRunMenu seq={run.seq} onDelete={() => onDelete(run)} />}
        </div>

        <TaskRunRequest prompt={run.prompt} />

        {run.questions.map((question) => (
          <TaskQuestionCard
            key={question.handle_id}
            taskId={taskId}
            question={question}
            disabled={answeringHandleId === question.handle_id}
            onAnswered={onQuestionAnswered}
          />
        ))}

        {cards.map((card) =>
          card.type === 'action_request' ? (
            <ActionRequestMessage
              key={taskRunCardKey(card)}
              message={card}
              conversationId={conversationId}
            />
          ) : (
            <ConnectRequestMessage key={taskRunCardKey(card)} message={card} />
          ),
        )}

        {activity ? (
          <TaskActivityLine
            label={activity.label}
            waiting={activity.waiting}
            startedAt={run.started_at}
            onStop={() => onStop(conversationId)}
            stopping={activity.stopping}
            stopError={activity.stopError}
          />
        ) : (
          <TaskRunOutcome run={run} />
        )}
      </article>
    </MarkdownWorkspaceContext.Provider>
  )
})

/** A finished run's result, notes and state line. */
function TaskRunOutcome({ run }: { run: TaskRun }) {
  const result = run.result?.trim() ? run.result : null
  const notes = run.notes?.trim() ? run.notes : null

  return (
    <div className="task-run-outcome">
      {run.status === 'failed' && <TaskRunError error={run.error} className="task-run-outcome-error" />}
      {run.status === 'stopped' && <p className="task-run-state">Stopped.</p>}
      {run.handoff_missing && (
        <p className="task-run-state">
          This update ended without a handoff; its final text is shown as the result.
        </p>
      )}
      {result && (
        <div className="message-content task-ctl-markdown task-run-result">
          <RichMarkdown components={markdownComponents}>{result}</RichMarkdown>
        </div>
      )}
      {!result && run.status === 'completed' && (
        <p className="task-run-state">This update finished without a result.</p>
      )}
      {notes && <TaskRunNotes notes={notes} />}
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
 * left-anchored in the column (TaskUpdates.css). Also the optimistic entry
 * of a run being started (TaskUpdates).
 *
 * A long prompt (isLongPrompt) is clamped to about eight lines with a fade
 * (`.task-run-request.clamped`) and a "Show full prompt" / "Show less"
 * toggle under it. Whether the clamp actually hides anything is measured
 * (the column's width changes with the viewport): when the whole prompt
 * fits, no fade and no toggle.
 */
export function TaskRunRequest({ prompt }: { prompt: string }) {
  const long = isLongPrompt(prompt)
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(true)
  const textRef = useRef<HTMLDivElement>(null)
  const contentId = useId()

  // Measure while clamped; an expanded prompt keeps the last answer.
  useLayoutEffect(() => {
    const text = textRef.current
    if (!long || expanded || !text) return
    const check = () => setOverflows(text.scrollHeight > text.clientHeight + 1)
    check()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(check)
    observer.observe(text)
    return () => observer.disconnect()
  }, [long, expanded, prompt])

  const clampable = long && overflows
  const clamped = clampable && !expanded
  return (
    <div
      className={`task-run-request${long && !expanded ? ' clamp' : ''}${clamped ? ' clamped' : ''}`}
    >
      <p className="task-run-request-label">You asked</p>
      <div ref={textRef} id={contentId} className="task-run-request-text">
        {prompt}
      </div>
      {clampable && (
        <button
          type="button"
          className="task-run-request-toggle"
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

/** The collapsed "Handoff notes" disclosure under a result. */
function TaskRunNotes({ notes }: { notes: string }) {
  const [open, setOpen] = useState(false)
  const regionId = useId()
  return (
    <div className={`task-run-notes${open ? ' open' : ''}`}>
      <button
        type="button"
        className="task-run-notes-toggle"
        aria-expanded={open}
        aria-controls={regionId}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronRight size={14} className="task-run-notes-chevron" aria-hidden="true" />
        Handoff notes
      </button>
      {open && (
        <div id={regionId} className="task-run-notes-body message-content" role="region" aria-label="Handoff notes">
          <RichMarkdown components={markdownComponents}>{notes}</RichMarkdown>
        </div>
      )}
    </div>
  )
}

/**
 * The finished run's "more" menu (one item, "Delete entry"), following the
 * conversation header's menu: the toggle stops propagation and any outside
 * click closes it; Escape closes it and returns focus to the toggle.
 */
function TaskRunMenu({ seq, onDelete }: { seq: number; onDelete: () => void }) {
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
    <div className="task-run-menu-wrapper">
      <button
        ref={toggleRef}
        type="button"
        className={`task-run-menu-button${open ? ' open' : ''}`}
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
        <div className="task-run-menu" role="menu" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            className="task-run-menu-item danger"
            role="menuitem"
            autoFocus
            onClick={() => {
              setOpen(false)
              onDelete()
            }}
          >
            <Trash2 size={15} className="task-run-menu-icon" aria-hidden="true" />
            <span>Delete update</span>
          </button>
        </div>
      )}
    </div>
  )
}
