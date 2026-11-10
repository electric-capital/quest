import { useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import './NewTaskPage.css'
import { createTask } from '../../api/tasksApi'
import type { Task } from '../../api/types'
import { TASK_DRAFT_KEY } from '../../constants/drafts'
import { useConversationSkills } from '../../contexts/ConversationSkillsContext'
import { taskRunStartErrorMessage } from '../../services/taskRuns'
import { taskThreadPath } from '../../utils/tasksRoute'
import { UploadProgress, type UploadStatus } from '../UploadProgress'
import { TaskComposer } from './TaskComposer'
import { TaskNotice, taskNoticeState } from './TaskNotice'
import {
  initialUploadStatus,
  startTaskRunFromComposer,
  type TaskComposerSubmission,
} from './taskComposerSend'

const PLACEHOLDER = 'What should this task do?'

/**
 * The New Task page (/tasks/new): an empty thread whose composer creates
 * the task on the first Send (spec 8.1), the tasks twin of the home
 * screen's deferred-create flow (HomeComposer):
 *
 * 1. POST /tasks with the prompt: the server cuts the title from it and
 *    keeps `title_is_default`, so the first run may rename the task. The
 *    composer's model goes to the run, not to the task. A refusal (e.g. 409
 *    `tasks_cap_reached`) shows inline and nothing else happens; the
 *    composer has already cleared the text (it cannot be re-seeded, as on
 *    the home screen), but the queued skills are put back.
 * 2. startTaskRunFromComposer: the run conversation, the attachment uploads
 *    (UploadProgress strip meanwhile) and the send.
 * 3. Navigate to the thread. When the task exists but its run did not
 *    start, or some attachments failed to upload, the message rides the
 *    router state to the task page's TaskNotice.
 *
 * The composer is bound to TASK_DRAFT_KEY (TaskComposer's header covers the
 * draft-key rules). Send is guarded against a double submit.
 */
export function NewTaskPage() {
  const navigate = useNavigate()
  const { setQueuedSkillsForConversation } = useConversationSkills()
  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false)
  const [error, setError] = useState<string | null>(null)
  const [uploadStatus, setUploadStatus] = useState<UploadStatus | null>(null)

  const handleSubmit = useCallback(
    async (submission: TaskComposerSubmission) => {
      if (submittingRef.current) return
      if (!submission.text) {
        // Only pasted images: a task needs a prompt (its title is cut from it).
        setError('Write a prompt to start the task.')
        return
      }
      submittingRef.current = true
      setSubmitting(true)
      setError(null)
      setUploadStatus(initialUploadStatus(submission))

      let task: Task
      try {
        task = await createTask({ prompt: submission.text })
      } catch (err) {
        setError(err instanceof Error && err.message ? err.message : 'The task could not be created.')
        // The composer cleared its queued skills on Send (after this call
        // returned, so the restore below lands last); nothing was sent.
        if (submission.skillIds.length > 0) {
          setQueuedSkillsForConversation(TASK_DRAFT_KEY, submission.skillIds)
        }
        submittingRef.current = false
        setSubmitting(false)
        setUploadStatus(null)
        return
      }

      const warnings: string[] = []
      let notice: string | null = null
      try {
        await startTaskRunFromComposer(task.id, submission, {
          onUploadStatus: setUploadStatus,
          onWarning: (message) => warnings.push(message),
        })
        if (warnings.length > 0) notice = warnings.join(' ')
      } catch (err) {
        notice = `The task was created, but its run did not start: ${taskRunStartErrorMessage(err)}`
      }
      // The page unmounts on navigation; the reset only matters if it does not.
      submittingRef.current = false
      setSubmitting(false)
      setUploadStatus(null)
      navigate(taskThreadPath(task.id), notice ? { state: taskNoticeState(notice) } : undefined)
    },
    [navigate, setQueuedSkillsForConversation],
  )

  const onSubmit = useCallback(
    (submission: TaskComposerSubmission) => {
      void handleSubmit(submission)
    },
    [handleSubmit],
  )

  return (
    <div className="task-new-page">
      <div className="task-new-page-inner">
        <h1 className="task-new-page-title">New task</h1>
        <p className="task-new-page-note">
          Each prompt is answered by a run with a fresh context; the task keeps a brief and a
          log between runs. You can edit the title and instructions afterwards.
        </p>
        {error && (
          <div className="task-new-page-error">
            <TaskNotice message={error} onDismiss={() => setError(null)} />
          </div>
        )}
        {uploadStatus && <UploadProgress status={uploadStatus} />}
        <div className="task-new-page-composer">
          <TaskComposer
            draftKey={TASK_DRAFT_KEY}
            onSubmit={onSubmit}
            busy={submitting}
            placeholder={PLACEHOLDER}
          />
        </div>
      </div>
    </div>
  )
}
