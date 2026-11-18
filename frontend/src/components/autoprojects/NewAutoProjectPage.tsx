import { useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import './NewAutoProjectPage.css'
import { ApiClientError } from '../../api/request'
import { createAutoProject } from '../../api/autoprojectsApi'
import type { AutoProject } from '../../api/types'
import { AUTOPROJECT_DRAFT_KEY } from '../../constants/drafts'
import { useAppConfig } from '../../contexts/AppConfigContext'
import { useConversationSkills } from '../../contexts/ConversationSkillsContext'
import { autoprojectRunStartErrorMessage } from '../../services/autoprojectRuns'
import { autoprojectPath } from '../../utils/autoprojectsRoute'
import { UploadProgress, type UploadStatus } from '../UploadProgress'
import { AutoProjectComposer } from './AutoProjectComposer'
import { AutoProjectNotice, autoprojectNoticeState } from './AutoProjectNotice'
import {
  initialUploadStatus,
  startAutoProjectRunFromComposer,
  type AutoProjectComposerSubmission,
} from './autoprojectComposerSend'


/** Create refusals about the model alone: retried without it, so they never block the AutoProject. */
const MODEL_REFUSALS = new Set(['model_not_allowed', 'invalid_model', 'invalid_thinking_effort'])

/**
 * POST /autoprojects for a first Send. A model the user picked in the composer
 * becomes the AutoProject's default; when the server refuses that model the AutoProject
 * is created without one (the run still carries the pick, and the send path
 * reports a model it refuses).
 */
async function createAutoProjectForSubmission(
  submission: AutoProjectComposerSubmission,
  modelPicked: boolean,
): Promise<AutoProject> {
  if (!modelPicked || !submission.model) return createAutoProject({ prompt: submission.text })
  try {
    return await createAutoProject({
      prompt: submission.text,
      model: submission.model,
      thinking_effort: submission.thinkingEffort,
    })
  } catch (err) {
    if (!(err instanceof ApiClientError) || !MODEL_REFUSALS.has(err.errorCode ?? '')) throw err
    console.warn('The AutoProject was created without its default model', err)
    return createAutoProject({ prompt: submission.text })
  }
}

/**
 * The New AutoProject page (/autoprojects/new): an empty page whose composer creates
 * the AutoProject on the first Send, the AutoProjects twin of the home
 * screen's deferred-create flow (HomeComposer):
 *
 * 1. POST /autoprojects with the prompt: the server cuts the title from it and
 *    keeps `title_is_default`, so the first run may rename the AutoProject. When
 *    the user picked a model in the composer, it (with its thinking level)
 *    also becomes the AutoProject's default model, as later picks on its page
 *    do; a refused model is dropped from the create, never blocking it
 *    (createAutoProjectForSubmission). Without a pick the AutoProject keeps no default and
 *    the run takes the composer's seeded model. A refusal (e.g. 409
 *    `autoprojects_cap_reached`) shows inline and nothing else happens; the
 *    composer has already cleared the text (it cannot be re-seeded, as on
 *    the home screen), but the queued skills are put back.
 * 2. startAutoProjectRunFromComposer: the run conversation, the attachment uploads
 *    (UploadProgress strip meanwhile) and the send.
 * 3. Navigate to the AutoProject page. When the AutoProject exists but its run did not
 *    start, or some attachments failed to upload, the message rides the
 *    router state to the AutoProject page's AutoProjectNotice.
 *
 * The composer is bound to AUTOPROJECT_DRAFT_KEY (AutoProjectComposer's header covers the
 * draft-key rules). Send is guarded against a double submit.
 */
export function NewAutoProjectPage() {
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
    async (submission: AutoProjectComposerSubmission) => {
      if (submittingRef.current) return
      if (!submission.text) {
        // Only pasted images: an AutoProject needs a prompt (its title is cut from it).
        setError('Write a prompt to start the AutoProject.')
        return
      }
      submittingRef.current = true
      setSubmitting(true)
      setError(null)
      setUploadStatus(initialUploadStatus(submission))

      let autoproject: AutoProject
      try {
        autoproject = await createAutoProjectForSubmission(submission, modelPickedRef.current)
      } catch (err) {
        setError(err instanceof Error && err.message ? err.message : 'The AutoProject could not be created.')
        // The composer cleared its queued skills on Send (after this call
        // returned, so the restore below lands last); nothing was sent.
        if (submission.skillIds.length > 0) {
          setQueuedSkillsForConversation(AUTOPROJECT_DRAFT_KEY, submission.skillIds)
        }
        submittingRef.current = false
        setSubmitting(false)
        setUploadStatus(null)
        return
      }

      const warnings: string[] = []
      let notice: string | null = null
      try {
        await startAutoProjectRunFromComposer(autoproject.id, submission, {
          onUploadStatus: setUploadStatus,
          onWarning: (message) => warnings.push(message),
        })
        if (warnings.length > 0) notice = warnings.join(' ')
      } catch (err) {
        notice = `The AutoProject was created, but its first prompt was not sent: ${autoprojectRunStartErrorMessage(err)}`
      }
      // The page unmounts on navigation; the reset only matters if it does not.
      submittingRef.current = false
      setSubmitting(false)
      setUploadStatus(null)
      navigate(autoprojectPath(autoproject.id), notice ? { state: autoprojectNoticeState(notice) } : undefined)
    },
    [navigate, setQueuedSkillsForConversation],
  )

  const onSubmit = useCallback(
    (submission: AutoProjectComposerSubmission) => {
      void handleSubmit(submission)
    },
    [handleSubmit],
  )

  return (
    <div className="autoproject-new-page">
      <div className="autoproject-new-page-inner">
        <h1 className="autoproject-new-page-title">New AutoProject</h1>
        <p className="autoproject-new-page-note">
          AutoProjects are a new way to work with an agent across many iterations on very large pieces
          of work. Notes and files about the AutoProject are accumulated automatically.
        </p>
        {error && (
          <div className="autoproject-new-page-error">
            <AutoProjectNotice message={error} onDismiss={() => setError(null)} />
          </div>
        )}
        {uploadStatus && <UploadProgress status={uploadStatus} />}
        <div className="autoproject-new-page-composer">
          <AutoProjectComposer
            draftKey={AUTOPROJECT_DRAFT_KEY}
            onSubmit={onSubmit}
            busy={submitting}
            placeholder={`Describe the goal of the AutoProject and what ${appName} should do first`}
            onModelChange={onModelChange}
          />
        </div>
      </div>
    </div>
  )
}
