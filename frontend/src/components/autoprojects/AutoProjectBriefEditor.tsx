import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { AUTOPROJECT_BRIEF_MAX_CHARS, putAutoProjectBrief, staleBriefCurrent } from '../../api/autoprojectsApi'
import type { AutoProject, AutoProjectBriefState } from '../../api/types'
import { useAppConfig } from '../../contexts/AppConfigContext'
import { briefWriterPhrase } from './autoprojectBriefSource'
import { strippedCodePointLength } from '../../utils/textLength'
import { AutoProjectCharCounter, AutoProjectDiscardConfirm, autoprojectSaveErrorMessage } from './AutoProjectBriefControls'

/** The brief version a save is based on, and its text as the user was shown it. */
interface BriefBase {
  version: number
  text: string
}

/** A newer brief shown read-only above the draft. */
interface MergeText {
  brief: string
  brief_version: number
}

/**
 * The brief editor (AutoProjectBrief's edit mode, in place in the main
 * column): a monospace markdown textarea (focused on open) with an
 * `n / 12,000` counter (AutoProjectCharCounter: no truncation, Save disabled
 * over the cap), Save and Cancel.
 *
 * Concurrency. Save sends `putAutoProjectBrief(draft, base.version)`, where the
 * base is the version the edit started from, or the newer version the user
 * has since been shown in the merge box; it never sends a version whose
 * text the user has not seen. The draft itself is never touched from
 * outside:
 * - The brief moving underneath (a run's write or another tab, seen as
 *   `autoproject.brief_version > base.version` after useAutoProject's refetch) shows a
 *   notice naming who wrote it, with "Show the new version", which opens
 *   the merge box from `autoproject.brief` and moves the base to that version.
 * - A 409 `stale_brief` keeps the editor open with the draft untouched and
 *   opens the merge box ABOVE the draft with the server's current text
 *   (raw markdown, read-only, scrollable), moves the base to it so the next
 *   Save goes through, and calls `onStale` (useAutoProject's refresh) so the page
 *   catches up. A further conflict replaces the box's text with the newer
 *   one.
 * - The merge box offers "Replace my draft with this" (with an Undo until
 *   the next keystroke) and "Keep my draft" (closes the box; the next Save
 *   overwrites that version with the draft).
 * While a run is active a one-line hint says the run will merge the edit
 * if it rewrites the brief too (the backend's brief_conflict path).
 *
 * Errors other than the conflict (400 `brief_too_long` with its extras
 * mapped to a sentence, network, ...) show inline; the editor stays open.
 * Cancel discards; Escape cancels at once only while the draft equals the
 * base text and no merge box is open, otherwise it asks first
 * (AutoProjectDiscardConfirm in place of the Cancel / Save row): Discard cancels
 * as Cancel does, Keep editing (or Escape again) goes back to the textarea
 * with the draft intact. A successful save hands the new brief state to
 * `onSaved` (useAutoProject's applyBrief), and the parent closes the editor.
 */
export function AutoProjectBriefEditor({
  autoproject,
  onSaved,
  onCancel,
  onStale,
}: {
  autoproject: AutoProject
  onSaved: (brief: AutoProjectBriefState) => void
  onCancel: () => void
  /** A save conflicted: re-fetch the AutoProject so the page shows the newer brief. */
  onStale: () => void
}) {
  // The editor mounts when Edit is pressed: the AutoProject as of then is the start.
  const { appName } = useAppConfig()
  const [draft, setDraft] = useState(autoproject.brief)
  const [base, setBase] = useState<BriefBase>({ version: autoproject.brief_version, text: autoproject.brief })
  const [merge, setMerge] = useState<MergeText | null>(null)
  // The draft "Replace my draft with this" threw away, for Undo.
  const [replaced, setReplaced] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const ids = useId()
  const textareaId = `${ids}-textarea`
  const hintId = `${ids}-hint`
  const counterId = `${ids}-counter`
  const overId = `${ids}-over`
  const errorId = `${ids}-error`

  // Open with the caret after the text, ready to add to it.
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [])

  const length = strippedCodePointLength(draft)
  const over = length > AUTOPROJECT_BRIEF_MAX_CHARS
  const unchanged = draft.trim() === base.text.trim()
  const canSave = !saving && !over && !unchanged

  // A newer brief than the one the user has seen. Not while saving: our own
  // write's autoproject_changed may refetch the AutoProject before the PUT answers.
  const newer = !saving && autoproject.brief_version > base.version

  const save = async () => {
    if (!canSave) return
    setSaving(true)
    setError(null)
    try {
      const result = await putAutoProjectBrief(autoproject.id, draft, base.version)
      setSaving(false)
      onSaved(result)
    } catch (err) {
      setSaving(false)
      const current = staleBriefCurrent(err)
      if (current) {
        setMerge({ brief: current.brief, brief_version: current.brief_version })
        setBase({ version: current.brief_version, text: current.brief })
        setReplaced(null)
        onStale()
        return
      }
      setError(autoprojectSaveErrorMessage(err, 'Failed to save the brief.'))
    }
  }

  const showNewer = () => {
    setMerge({ brief: autoproject.brief, brief_version: autoproject.brief_version })
    setBase({ version: autoproject.brief_version, text: autoproject.brief })
  }

  const replaceDraft = () => {
    if (!merge) return
    setReplaced(draft)
    setDraft(merge.brief)
    setMerge(null)
    textareaRef.current?.focus()
  }

  const undoReplace = () => {
    if (replaced === null) return
    setDraft(replaced)
    setReplaced(null)
    textareaRef.current?.focus()
  }

  const keepDraft = () => {
    setMerge(null)
    textareaRef.current?.focus()
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Escape' || saving) return
    e.preventDefault()
    if (unchanged && merge === null) onCancel()
    else setConfirmDiscard(true)
  }

  const keepEditing = () => {
    setConfirmDiscard(false)
    textareaRef.current?.focus()
  }

  const activeRun = autoproject.active_run

  return (
    <div className="autoproject-ctl-editor">
      {activeRun && (
        <p className="autoproject-ctl-hint">
          An update is in progress; if it also rewrites the brief, it will merge your edit.
        </p>
      )}
      <div aria-live="polite">
        {newer && (
          <div className="autoproject-ctl-note">
            <span>
              This brief was updated {briefWriterPhrase(autoproject.brief_source, autoproject.brief_run_seq)} while
              you were editing. Saving will show you the new version so you can merge.
            </span>
            <button type="button" className="autoproject-ctl-link-button" onClick={showNewer}>
              Show the new version
            </button>
          </div>
        )}
        {replaced !== null && (
          <div className="autoproject-ctl-note">
            <span>Your draft was replaced with the newer brief.</span>
            <button type="button" className="autoproject-ctl-link-button" onClick={undoReplace}>
              Undo
            </button>
          </div>
        )}
      </div>
      {merge && (
        <section className="autoproject-brief-merge" aria-label="Newer brief">
          <p className="autoproject-brief-merge-title" role="status">
            The brief changed while you were editing (now version {merge.brief_version})
          </p>
          <pre className="autoproject-brief-merge-text" tabIndex={0} aria-label={`Brief version ${merge.brief_version}`}>
            {merge.brief || '(empty)'}
          </pre>
          <p className="autoproject-brief-merge-hint">
            Merge what you need into your draft below. Saving now replaces version {merge.brief_version}{' '}
            with your draft.
          </p>
          <div className="autoproject-ctl-editor-actions">
            <button type="button" className="autoproject-ctl-button" onClick={keepDraft}>
              Keep my draft
            </button>
            <button type="button" className="autoproject-ctl-button" onClick={replaceDraft}>
              Replace my draft with this
            </button>
          </div>
        </section>
      )}
      <label htmlFor={textareaId} className="autoproject-ctl-sr-only">
        Edit brief (markdown)
      </label>
      <textarea
        ref={textareaRef}
        id={textareaId}
        className="autoproject-ctl-textarea autoproject-ctl-textarea--mono"
        value={draft}
        rows={14}
        spellCheck
        placeholder={`What ${appName} should know before each update: the goal, where things stand, open threads...`}
        readOnly={saving}
        aria-busy={saving}
        aria-invalid={over || error !== null}
        aria-describedby={`${hintId} ${counterId} ${overId}${error ? ` ${errorId}` : ''}`}
        onChange={(e) => {
          setDraft(e.target.value)
          setReplaced(null)
          setConfirmDiscard(false)
        }}
        onKeyDown={onKeyDown}
      />
      <p id={hintId} className="autoproject-ctl-sr-only">
        Markdown. Each update starts from the brief and may rewrite it.
      </p>
      <AutoProjectCharCounter id={counterId} overId={overId} length={length} cap={AUTOPROJECT_BRIEF_MAX_CHARS} />
      {error && (
        <p id={errorId} className="autoproject-ctl-error" role="alert">
          {error}
        </p>
      )}
      {confirmDiscard ? (
        <AutoProjectDiscardConfirm onDiscard={onCancel} onKeepEditing={keepEditing} />
      ) : (
        <div className="autoproject-ctl-editor-actions">
          <button type="button" className="autoproject-ctl-button" onClick={onCancel} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="autoproject-ctl-button autoproject-ctl-button--primary"
            onClick={() => void save()}
            disabled={!canSave}
          >
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      )}
    </div>
  )
}
