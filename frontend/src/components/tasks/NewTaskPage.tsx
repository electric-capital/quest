import { useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import './NewTaskPage.css'
import { ApiClientError } from '../../api/request'
import { createTask } from '../../api/tasksApi'
import type { Task } from '../../api/types'
import { TASK_DRAFT_KEY } from '../../constants/drafts'
import { useAppConfig } from '../../contexts/AppConfigContext'
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


/** Create refusals about the model alone: retried without it, so they never block the task. */
const MODEL_REFUSALS = new Set(['model_not_allowed', 'invalid_model', 'invalid_thinking_effort'])

/**
 * POST /tasks for a first Send. A model the user picked in the composer
 * becomes the task's default; when the server refuses that model the task
 * is created without one (the run still carries the pick, and the send path
 * reports a model it refuses).
 */
async function createTaskForSubmission(
  submission: TaskComposerSubmission,
  modelPicked: boolean,
): Promise<Task> {
  if (!modelPicked || !submission.model) return createTask({ prompt: submission.text })
  try {
    return await createTask({
      prompt: submission.text,
      model: submission.model,
      thinking_effort: submission.thinkingEffort,
    })
  } catch (err) {
    if (!(err instanceof ApiClientError) || !MODEL_REFUSALS.has(err.errorCode ?? '')) throw err
    console.warn('The task was created without its default model', err)
    return createTask({ prompt: submission.text })
  }
}

/**
 * The New Task page (/tasks/new): an empty thread whose composer creates
 * the task on the first Send (spec 8.1), the tasks twin of the home
 * screen's deferred-create flow (HomeComposer):
 *
 * 1. POST /tasks with the prompt: the server cuts the title from it and
 *    keeps `title_is_default`, so the first run may rename the task. When
 *    the user picked a model in the composer, it (with its thinking level)
 *    also becomes the task's default model, as later picks in the thread
 *    do; a refused model is dropped from the create, never blocking it
 *    (createTaskForSubmission). Without a pick the task keeps no default and
 *    the run takes the composer's seeded model. A refusal (e.g. 409
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
  const { appName } = useAppConfig()
  const navigate = useNavigate()
  const { setQueuedSkillsForConversation } = useConversationSkills()
  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false)
  const [error, setError] = useState<string | null>(null)
  const [uploadStatus, setUploadStatus] = useState<UploadStatus | null>(null)
  // Set once the user picks a model in the selector (the seed does not count).
  const modelPickedRef = useRef(false)
  const onModelChange = useCallback(() => {
    modelPickedRef.current = true
  }, [])

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
        task = await createTaskForSubmission(submission, modelPickedRef.current)
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
        notice = `The task was created, but its first prompt was not sent: ${taskRunStartErrorMessage(err)}`
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
          Tasks are a new way to work with an agent across many iterations on very large pieces
          of work. Notes and files about the task are accumulated automatically.
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
            placeholder={`Describe the goal of the task and what ${appName} should do first`}
            onModelChange={onModelChange}
          />
        </div>
      </div>
    </div>
  )
}
