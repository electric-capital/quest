import { useContext, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { AutoProjectQuestion, AutoProjectQuestionTranscriptMessage } from '../../api/types'
import { persistentWebSocket } from '../../services/PersistentWebSocket'
import { autoprojectThreadPath } from '../../utils/autoprojectsRoute'
import { AutoProjectQuestionCard } from './AutoProjectQuestionCard'
import { AutoProjectRunPanelContext } from './AutoProjectRunPanelContext'

/** An outcome learned from `wait_handle_resolved` after the row was loaded. */
interface LiveOutcome {
  handleId: string
  status: 'answered' | 'cancelled'
  answer: string | null
}

/** The transcript row as the card's AutoProjectQuestion (the row has no answer time). */
function questionFromRow(row: AutoProjectQuestionTranscriptMessage): AutoProjectQuestion {
  return {
    handle_id: row.wait_handle_id,
    question: row.question,
    options: Array.isArray(row.options) ? row.options : [],
    answer: row.answer ?? null,
    status: row.status,
    asked_at: row.timestamp ?? null,
    answered_at: null,
  }
}

/**
 * An `autoproject_question` row of an AutoProject run's transcript (ChatPanel, through
 * MessageContentRenderer), shown with the updates' AutoProjectQuestionCard.
 *
 * The state shown is the freshest known: the run's own question from the
 * run panel's runs page (AutoProjectRunPanelContext, refetched on `autoproject_changed`),
 * else the transcript row; a `wait_handle_resolved` for the question's
 * handle (an answer from the AutoProject page or another tab, a stop) flips a still
 * pending one at once, since the conversation store keeps the row as it was
 * loaded while the server rewrites it in place.
 *
 * Inside the run panel the option buttons answer through the AutoProject; a free
 * text answer goes through the AutoProject page's composer (the run's composer
 * is read-only), which the hint links to. Anywhere else the card is
 * read-only with an "Answer in the AutoProject" hint.
 */
export function AutoProjectQuestionMessage({ message }: { message: AutoProjectQuestionTranscriptMessage }) {
  const runView = useContext(AutoProjectRunPanelContext)
  const handleId = message.wait_handle_id
  const [live, setLive] = useState<LiveOutcome | null>(null)

  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'wait_handle_resolved') return
      if (event.kind !== 'autoproject_question' || event.handle_id !== handleId) return
      if (event.status === 'accepted') {
        const response = event.response as { answer?: unknown } | undefined
        setLive({
          handleId,
          status: 'answered',
          answer: typeof response?.answer === 'string' ? response.answer : null,
        })
      } else if (typeof event.status === 'string') {
        setLive({ handleId, status: 'cancelled', answer: null })
      }
    })
  }, [handleId])

  const fromRun = runView?.questions.find((q) => q.handle_id === handleId) ?? null
  let question = fromRun ?? questionFromRow(message)
  if (question.status === 'pending' && live?.handleId === handleId) {
    question = { ...question, status: live.status, answer: live.answer }
  }

  const autoprojectId = runView?.autoprojectId ?? null
  const hint = autoprojectId ? (
    <>
      {question.options.length > 0 ? 'Or answer in your own words ' : 'Answer '}
      <Link to={autoprojectThreadPath(autoprojectId)} className="autoproject-question-hint-link">
        in the AutoProject
      </Link>
      .
    </>
  ) : (
    'Answer in the AutoProject.'
  )

  return <AutoProjectQuestionCard autoprojectId={autoprojectId} question={question} hint={hint} />
}
