import { memo, useEffect, useId, useMemo, useRef, useState } from 'react'
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
 * - the prompt, rendered as Message.tsx renders a user message;
 * - its questions (TaskQuestionCard, oldest first; answered / cancelled
 *   ones keep showing on finished runs);
 * - the approval and connect cards the thread keeps for it (`cards`: the
 *   open ones plus, while the run is active, those resolved since they were
 *   first seen, which flip themselves to their resolved state);
 * - for the active run the activity line, else the outcome: the `result` as
 *   the assistant reply (with a "Handoff notes" disclosure for the notes),
 *   the handoff-missing, failed or stopped line, and a quiet "Open run".
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

        <Message
          role="user"
          content={run.prompt}
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
      {run.status === 'failed' && (
        <p className="task-run-state failed" role="alert">
          Run failed: {run.error?.trim() || 'unknown error'}
        </p>
      )}
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
