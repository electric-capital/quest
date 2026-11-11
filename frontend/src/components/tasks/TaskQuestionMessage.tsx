import { useContext, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { TaskQuestion, TaskQuestionTranscriptMessage } from '../../api/types'
import { persistentWebSocket } from '../../services/PersistentWebSocket'
import { taskThreadPath } from '../../utils/tasksRoute'
import { TaskQuestionCard } from './TaskQuestionCard'
import { TaskRunViewContext } from './TaskRunViewContext'

/** An outcome learned from `wait_handle_resolved` after the row was loaded. */
interface LiveOutcome {
  handleId: string
  status: 'answered' | 'cancelled'
  answer: string | null
}

/** The transcript row as the card's TaskQuestion (the row has no answer time). */
function questionFromRow(row: TaskQuestionTranscriptMessage): TaskQuestion {
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
 * A `task_question` row of a task run's transcript (ChatPanel, through
 * MessageContentRenderer), shown with the thread's TaskQuestionCard.
 *
 * The state shown is the freshest known: the run's own question from the
 * run view's runs page (TaskRunViewContext, refetched on `task_changed`),
 * else the transcript row; a `wait_handle_resolved` for the question's
 * handle (an answer from the thread or another tab, a stop) flips a still
 * pending one at once, since the conversation store keeps the row as it was
 * loaded while the server rewrites it in place.
 *
 * Inside the run view the option buttons answer through the task; a free
 * text answer goes through the thread's composer (the run view's composer
 * is read-only), which the hint links to. Anywhere else the card is
 * read-only with an "Answer in the task" hint.
 */
export function TaskQuestionMessage({ message }: { message: TaskQuestionTranscriptMessage }) {
  const runView = useContext(TaskRunViewContext)
  const handleId = message.wait_handle_id
  const [live, setLive] = useState<LiveOutcome | null>(null)

  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'wait_handle_resolved') return
      if (event.kind !== 'task_question' || event.handle_id !== handleId) return
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

  const taskId = runView?.taskId ?? null
  const hint = taskId ? (
    <>
      {question.options.length > 0 ? 'Or answer in your own words ' : 'Answer '}
      <Link to={taskThreadPath(taskId)} className="task-question-hint-link">
        in the task
      </Link>
      .
    </>
  ) : (
    'Answer in the task.'
  )

  return <TaskQuestionCard taskId={taskId} question={question} hint={hint} />
}
