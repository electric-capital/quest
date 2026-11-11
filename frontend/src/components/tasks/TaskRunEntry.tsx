import { memo, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, MoreHorizontal, Trash2 } from 'lucide-react'
import type {
  ActionRequestMessage as ActionRequestMessageType,
  ConnectRequestMessage as ConnectRequestMessageType,
  TaskRun,
} from '../../api/types'
import type { FileSource } from '../../api/fileApi'
import { formatTimestamp } from '../../utils/formatters'
import { taskRunPath } from '../../utils/tasksRoute'
import { ActionRequestMessage } from '../ActionRequestMessage'
import { ConnectRequestMessage } from '../ConnectRequestMessage'
import { MarkdownWorkspaceContext, Message, markdownComponents } from '../Message'
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
 * One run of the task thread (spec 8.2), top to bottom:
 *
 * - a header: "Run N", its start time and, on a finished run, the row menu
 *   ("Delete entry", confirmed by the thread);
 * - the prompt, rendered as Message.tsx renders a user message, clamped
 *   with a "Show full prompt" toggle when it is long (TaskRunPrompt);
 * - its questions (TaskQuestionCard, oldest first; answered / cancelled
 *   ones keep showing on finished runs);
 * - the approval and connect cards the thread keeps for it (`cards`: the
 *   open ones plus, while the run is active, those resolved since they were
 *   first seen, which flip themselves to their resolved state);
 * - for the active run the activity line, else the outcome: the `result` as
 *   the assistant reply (with a "Handoff notes" disclosure for the notes),
 *   the handoff-missing, failed (TaskRunError: one line plus "Show
 *   details") or stopped line, and a quiet "Open run".
 *
 * Everything is inside a MarkdownWorkspaceContext for the RUN's
 * conversation and its task, so workspace images and relative links in the
 * prompt, cards, result and notes resolve against that run's workspace,
 * `task://` references against the task's, and both open through the
 * thread's one file viewer (`onOpenFile`, told the file's space).
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
}) {
  const conversationId = run.conversation_id
  const workspaceCtx = useMemo(
    () => ({ conversationId, taskId, onOpenFile }),
    [conversationId, taskId, onOpenFile],
  )
  const finished = activity === null && !isActiveRunStatus(run.status)
  const runPath = taskRunPath(taskId, conversationId)

  return (
    <MarkdownWorkspaceContext.Provider value={workspaceCtx}>
      <article className="task-run" data-run-id={run.id} aria-label={`Run ${run.seq}`}>
        <div className="task-run-meta">
          <span className="task-run-seq">Run {run.seq}</span>
          <span className="task-run-time">{formatTimestamp(run.started_at)}</span>
          {finished && <TaskRunMenu seq={run.seq} onDelete={() => onDelete(run)} />}
        </div>

        <TaskRunPrompt
          prompt={run.prompt}
          timestamp={run.started_at}
          conversationId={conversationId}
        />

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
            openRunPath={runPath}
          />
        ) : (
          <TaskRunOutcome run={run} runPath={runPath} />
        )}
      </article>
    </MarkdownWorkspaceContext.Provider>
  )
})

/** A finished run's result, notes and state line, with the quiet Open run link. */
function TaskRunOutcome({ run, runPath }: { run: TaskRun; runPath: string }) {
  const result = run.result?.trim() ? run.result : null
  const notes = run.notes?.trim() ? run.notes : null
  const finishedAt = run.finished_at ?? run.started_at

  return (
    <div className="task-run-outcome">
      {run.status === 'failed' && <TaskRunError error={run.error} className="task-run-outcome-error" />}
      {run.status === 'stopped' && <p className="task-run-state">Stopped.</p>}
      {run.handoff_missing && (
        <p className="task-run-state">
          This run ended without a handoff; its final text is shown as the result.
        </p>
      )}
      {result && (
        <Message role="assistant" content={result} timestamp={finishedAt} model={run.model ?? undefined} />
      )}
      {!result && run.status === 'completed' && (
        <p className="task-run-state">This run finished without a result.</p>
      )}
      {notes && <TaskRunNotes notes={notes} />}
      <Link to={runPath} className="task-run-open">
        Open run
      </Link>
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
 * The run's prompt as a user bubble. A long prompt (isLongPrompt) is
 * clamped to a few lines with a fade (CSS, `.task-run-prompt.clamped`) and
 * a "Show full prompt" / "Show less" toggle under the bubble. Whether the
 * clamp actually hides anything is measured (the bubble's width changes
 * with the viewport): when the whole prompt fits, no fade and no toggle.
 */
function TaskRunPrompt({
  prompt,
  timestamp,
  conversationId,
}: {
  prompt: string
  timestamp: string
  conversationId: string
}) {
  const long = isLongPrompt(prompt)
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(true)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const contentId = useId()

  // Measure while clamped; an expanded prompt keeps the last answer.
  useLayoutEffect(() => {
    const wrapper = wrapperRef.current
    if (!long || expanded || !wrapper) return
    const content = wrapper.querySelector<HTMLElement>('.message-content')
    if (!content) return
    const check = () => setOverflows(content.scrollHeight > content.clientHeight + 1)
    check()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(check)
    observer.observe(content)
    return () => observer.disconnect()
  }, [long, expanded, prompt])

  const clampable = long && overflows
  const clamped = clampable && !expanded
  return (
    <div
      ref={wrapperRef}
      id={contentId}
      className={`task-run-prompt${long && !expanded ? ' clamp' : ''}${clamped ? ' clamped' : ''}${clampable ? ' clampable' : ''}`}
    >
      <Message role="user" content={prompt} timestamp={timestamp} conversationId={conversationId} />
      {clampable && (
        <button
          type="button"
          className="task-run-prompt-toggle"
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
        aria-label={`Run ${seq} options`}
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
            <span>Delete entry</span>
          </button>
        </div>
      )}
    </div>
  )
}
