import { useEffect, useRef, useState, type ReactNode } from 'react'
import { LoaderCircle, MessageCircleQuestionMark } from 'lucide-react'
import { answerTaskQuestion, taskErrorDetail } from '../../api/tasksApi'
import { ApiClientError } from '../../api/request'
import type { TaskQuestion } from '../../api/types'
import { formatTimestamp } from '../../utils/formatters'
import './TaskQuestionCard.css'

export interface TaskQuestionCardProps {
  /** The task to answer through; null shows the question read-only (option buttons disabled). */
  taskId: string | null
  question: TaskQuestion
  /** Locks the option buttons (e.g. the composer is sending an answer). */
  disabled?: boolean
  /** After a successful answer (or a 409 question_resolved): the server's Question when known.
   *  The thread refreshes its runs here. */
  onAnswered?: (question: TaskQuestion | null) => void
  /** Replaces the pending question's hint under the options (by default it
   *  points at the thread composer below the card). */
  hint?: ReactNode
}

/** The outcome this card learned from the server before the thread refetched. */
interface LocalOutcome {
  handleId: string
  status: 'answered' | 'cancelled'
  answer: string | null
  answeredAt: string | null
}

const SEND_FAILED = 'Could not send the answer. Try again.'

/**
 * The outcome carried by a 409 `question_resolved` (the question was
 * answered elsewhere or cancelled by a stop), else null.
 */
function resolvedOutcome(err: unknown, handleId: string): LocalOutcome | null {
  if (!(err instanceof ApiClientError) || err.errorCode !== 'question_resolved') return null
  const detail = taskErrorDetail(err)
  const answer = typeof detail?.answer === 'string' ? detail.answer : null
  return {
    handleId,
    status: detail?.status === 'cancelled' ? 'cancelled' : 'answered',
    answer,
    answeredAt: null,
  }
}

/**
 * A question a task run asked with `ask_task_question`, shown in the task
 * thread under the run's prompt. Pending: the question with one button per
 * suggested answer (free text goes through the thread's composer). A click
 * posts the answer; once the server accepts it the card shows it at once,
 * before the thread's refetch brings the answered question. Answered: the
 * answer and its time. Cancelled: the run was stopped first.
 *
 * The option buttons unmount once the answer lands, so focus would fall to
 * the page: when the answer came from a click here and focus was still in
 * the card, the card itself (tabIndex -1) takes it.
 */
export function TaskQuestionCard({
  taskId,
  question,
  disabled = false,
  onAnswered,
  hint,
}: TaskQuestionCardProps) {
  // Index of the option being sent, or null.
  const [sending, setSending] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<LocalOutcome | null>(null)
  const cardRef = useRef<HTMLElement>(null)
  // Set by an answer sent from this card; the card takes focus once it is resolved.
  const refocusRef = useRef(false)

  const handleId = question.handle_id
  // The prop wins once it is resolved; a local outcome only covers the gap
  // until the thread's refetch, and only for the question it was taken for.
  const local = question.status === 'pending' && outcome?.handleId === handleId ? outcome : null
  const status = local?.status ?? question.status
  const answer = local ? local.answer : question.answer
  const answeredAt = local ? local.answeredAt : question.answered_at

  useEffect(() => {
    if (status === 'pending' || !refocusRef.current) return
    refocusRef.current = false
    const card = cardRef.current
    const active = document.activeElement
    // Only while focus is still here (or already fell to the page).
    if (card && (active === null || active === document.body || card.contains(active))) {
      card.focus({ preventScroll: true })
    }
  }, [status])

  const answerWith = async (index: number) => {
    const option = question.options[index]
    if (taskId === null || sending !== null || option === undefined) return
    setSending(index)
    setError(null)
    refocusRef.current = true
    try {
      const updated = await answerTaskQuestion(taskId, handleId, option)
      setOutcome({
        handleId,
        status: updated.status === 'cancelled' ? 'cancelled' : 'answered',
        answer: updated.answer ?? option,
        answeredAt: updated.answered_at,
      })
      onAnswered?.(updated)
    } catch (err) {
      const resolved = resolvedOutcome(err, handleId)
      if (resolved) {
        setOutcome(resolved)
        onAnswered?.(null)
      } else {
        refocusRef.current = false
        setError(err instanceof Error && err.message ? err.message : SEND_FAILED)
      }
    } finally {
      setSending(null)
    }
  }

  const options = question.options
  const locked = disabled || taskId === null || sending !== null

  return (
    <section
      ref={cardRef}
      tabIndex={-1}
      className={`task-question-card ${status}`}
      aria-label="Question"
    >
      <div className="task-question-header">
        <MessageCircleQuestionMark size={14} aria-hidden="true" />
        <span className="task-question-label">Question</span>
        {question.asked_at && (
          <time className="task-question-time" dateTime={question.asked_at}>
            {formatTimestamp(question.asked_at)}
          </time>
        )}
      </div>

      <p className="task-question-text">{question.question}</p>

      {status === 'pending' && (
        <>
          {options.length > 0 && (
            <div className="task-question-options" role="group" aria-label="Suggested answers">
              {options.map((option, index) => (
                <button
                  key={`${index}:${option}`}
                  type="button"
                  className="task-question-option"
                  onClick={() => void answerWith(index)}
                  disabled={locked}
                  aria-busy={sending === index}
                >
                  {sending === index && (
                    <LoaderCircle size={13} className="task-question-spinner" aria-hidden="true" />
                  )}
                  <span className="task-question-option-text">{option}</span>
                </button>
              ))}
            </div>
          )}
          <p className="task-question-hint">
            {hint ?? (options.length > 0
              ? 'Or type your answer in the box below.'
              : 'Type your answer in the box below.')}
          </p>
          {error && (
            <p className="task-question-error" role="alert">
              {error}
            </p>
          )}
        </>
      )}

      {status === 'answered' && (
        <div className="task-question-answer">
          <span className="task-question-answer-label">Answered:</span>{' '}
          <span className="task-question-answer-text">{answer ?? ''}</span>
          {answeredAt && (
            <time className="task-question-time" dateTime={answeredAt}>
              {formatTimestamp(answeredAt)}
            </time>
          )}
        </div>
      )}

      {status === 'cancelled' && (
        <p className="task-question-cancelled">Cancelled: the update was stopped before you answered.</p>
      )}
    </section>
  )
}
