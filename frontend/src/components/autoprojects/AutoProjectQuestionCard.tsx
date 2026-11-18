import { useEffect, useRef, useState, type ReactNode } from 'react'
import { LoaderCircle, MessageCircleQuestionMark } from 'lucide-react'
import { answerAutoProjectQuestion, autoprojectErrorDetail } from '../../api/autoprojectsApi'
import { ApiClientError } from '../../api/request'
import type { AutoProjectQuestion } from '../../api/types'
import { formatTimestamp } from '../../utils/formatters'
import './AutoProjectQuestionCard.css'

export interface AutoProjectQuestionCardProps {
  /** The AutoProject to answer through; null shows the question read-only (option buttons disabled). */
  autoprojectId: string | null
  question: AutoProjectQuestion
  /** Locks the option buttons (e.g. the composer is sending an answer). */
  disabled?: boolean
  /** After a successful answer (or a 409 question_resolved): the server's Question when known.
   *  The page refreshes its runs here. */
  onAnswered?: (question: AutoProjectQuestion | null) => void
  /** Replaces the pending question's hint under the options (by default it
   *  points at the page's composer, above the Updates card). */
  hint?: ReactNode
}

/** The outcome this card learned from the server before the page refetched. */
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
  const detail = autoprojectErrorDetail(err)
  const answer = typeof detail?.answer === 'string' ? detail.answer : null
  return {
    handleId,
    status: detail?.status === 'cancelled' ? 'cancelled' : 'answered',
    answer,
    answeredAt: null,
  }
}

/**
 * A question an AutoProject run asked with `ask_autoproject_question`, shown on the
 * AutoProject page under the run's prompt. Pending: the question with one button per
 * suggested answer (free text goes through the page's composer). A click
 * posts the answer; once the server accepts it the card shows it at once,
 * before the page's refetch brings the answered question. Answered: the
 * answer and its time. Cancelled: the run was stopped first.
 *
 * The option buttons unmount once the answer lands, so focus would fall to
 * the page: when the answer came from a click here and focus was still in
 * the card, the card itself (tabIndex -1) takes it.
 */
export function AutoProjectQuestionCard({
  autoprojectId,
  question,
  disabled = false,
  onAnswered,
  hint,
}: AutoProjectQuestionCardProps) {
  // Index of the option being sent, or null.
  const [sending, setSending] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<LocalOutcome | null>(null)
  const cardRef = useRef<HTMLElement>(null)
  // Set by an answer sent from this card; the card takes focus once it is resolved.
  const refocusRef = useRef(false)

  const handleId = question.handle_id
  // The prop wins once it is resolved; a local outcome only covers the gap
  // until the page's refetch, and only for the question it was taken for.
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
    if (autoprojectId === null || sending !== null || option === undefined) return
    setSending(index)
    setError(null)
    refocusRef.current = true
    try {
      const updated = await answerAutoProjectQuestion(autoprojectId, handleId, option)
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
  const locked = disabled || autoprojectId === null || sending !== null

  return (
    <section
      ref={cardRef}
      tabIndex={-1}
      className={`autoproject-question-card ${status}`}
      aria-label="Question"
    >
      <div className="autoproject-question-header">
        <MessageCircleQuestionMark size={14} aria-hidden="true" />
        <span className="autoproject-question-label">Question</span>
        {question.asked_at && (
          <time className="autoproject-question-time" dateTime={question.asked_at}>
            {formatTimestamp(question.asked_at)}
          </time>
        )}
      </div>

      <p className="autoproject-question-text">{question.question}</p>

      {status === 'pending' && (
        <>
          {options.length > 0 && (
            <div className="autoproject-question-options" role="group" aria-label="Suggested answers">
              {options.map((option, index) => (
                <button
                  key={`${index}:${option}`}
                  type="button"
                  className="autoproject-question-option"
                  onClick={() => void answerWith(index)}
                  disabled={locked}
                  aria-busy={sending === index}
                >
                  {sending === index && (
                    <LoaderCircle size={13} className="autoproject-question-spinner" aria-hidden="true" />
                  )}
                  <span className="autoproject-question-option-text">{option}</span>
                </button>
              ))}
            </div>
          )}
          <p className="autoproject-question-hint">
            {hint ?? (options.length > 0
              ? 'Or type your answer in the box above.'
              : 'Type your answer in the box above.')}
          </p>
          {error && (
            <p className="autoproject-question-error" role="alert">
              {error}
            </p>
          )}
        </>
      )}

      {status === 'answered' && (
        <div className="autoproject-question-answer">
          <span className="autoproject-question-answer-label">Answered:</span>{' '}
          <span className="autoproject-question-answer-text">{answer ?? ''}</span>
          {answeredAt && (
            <time className="autoproject-question-time" dateTime={answeredAt}>
              {formatTimestamp(answeredAt)}
            </time>
          )}
        </div>
      )}

      {status === 'cancelled' && (
        <p className="autoproject-question-cancelled">Cancelled: the update was stopped before you answered.</p>
      )}
    </section>
  )
}
