import { useEffect, useMemo, useState } from 'react'
import type { AutoProject, AutoProjectRun } from '../../api/types'
import { conversationSource, type FileSource } from '../../api/fileApi'
import type { WorkspaceCopy } from '../../hooks/useWorkspaceCopy'
import { useAutoProjectRun } from '../../hooks/useAutoProjectRun'
import { formatTimestamp } from '../../utils/formatters'
import { ChatPanel } from '../ChatPanel'
import { MarkdownWorkspaceContext, markdownComponents } from '../Message'
import { RichMarkdown } from '../RichMarkdown'
import { FileBrowser } from '../FileBrowser'
import { AutoProjectDetailPanel } from './AutoProjectDetailPanel'
import { AutoProjectRunError } from './AutoProjectRunError'
import { AutoProjectRunPanelContext, type AutoProjectRunPanelContextValue } from './AutoProjectRunPanelContext'

/** The run's state in the panel header ("Update 3 · Needs you"). */
function autoprojectRunStatusLabel(run: Pick<AutoProjectRun, 'status' | 'handoff_missing'>): string {
  switch (run.status) {
    case 'running':
      return 'Working'
    case 'waiting':
      return 'Needs you'
    case 'completed':
      return run.handoff_missing ? 'Completed without handoff' : 'Completed'
    case 'failed':
      return 'Failed'
    case 'stopped':
      return 'Stopped'
  }
}

type RunPanelTab = 'transcript' | 'notes' | 'files'

/**
 * A run's transcript in the AutoProject page's detail panel (AutoProjectDetailPanel),
 * opened by "View run" on its update or by the run URL
 * (/autoprojects/<id>/runs/<conversation id>, which AutoProjectPage maps to this panel).
 *
 * Header: "Run N" with the run's live status and start time; a failed
 * run's error (AutoProjectRunError, one line with "Show details") or a stopped
 * run's line sits above the transcript. Two tabs under the header:
 *
 * - Transcript: ChatPanel for the run's conversation, read-only through its
 *   `autoproject` origin ("This is one run of an AutoProject. Continue in the AutoProject."), its
 *   own header suppressed. AutoProjectRunPanelContext hands the transcript's
 *   `autoproject_question` rows the AutoProject id and the run's questions as last
 *   listed, so a pending question is answerable here; approval and connect
 *   cards resolve through their own endpoints, as in any chat.
 * - Handoff notes (only for a finished run that left notes): what the run
 *   handed to the next one, as markdown in the run's workspace context
 *   (`autoproject://` links open in the page's viewer, `onOpenFile`). The main
 *   column no longer shows them.
 * - Chat Files: the run's own workspace (FileBrowser), with the page's
 *   Copy / Move row actions to the AutoProject files (`copy`, AutoProjectPage's
 *   useWorkspaceCopy, which also gives the main column's AutoProject files card
 *   its actions back into this run while the panel is open).
 *
 * The run is looked up among the AutoProject's runs by its conversation
 * (useAutoProjectRun) and re-read on every `autoproject_changed` of the AutoProject
 * (`changeSeq`), so the status moves live and a deleted run turns into the
 * not-found state. The transcript mounts only once the conversation is
 * known to be a run of THIS AutoProject: anything else (another AutoProject's run, a
 * plain chat, a deleted run) shows a not-found note. `onResolved` tells the
 * page whether the run resolved, so the copy actions exist only beside a
 * real run.
 */
export function AutoProjectRunPanel({
  autoproject,
  changeSeq,
  conversationId,
  copy,
  onResolved,
  onClose,
  onOpenFile,
}: {
  autoproject: AutoProject
  /** useAutoProject's count of `autoproject_changed` events: each re-reads the run. */
  changeSeq: number
  conversationId: string
  copy: WorkspaceCopy
  /** The run's seq once it resolved as a run of this AutoProject, else null. */
  onResolved: (conversationId: string, seq: number | null) => void
  onClose: () => void
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
}) {
  const autoprojectId = autoproject.id
  const lookup = useAutoProjectRun(autoprojectId, conversationId, changeSeq)
  const run = lookup.run
  // AutoProjectPage keys the panel by conversation: another run starts on its transcript.
  const [tab, setTab] = useState<RunPanelTab>('transcript')

  const resolvedSeq = run?.seq ?? null
  useEffect(() => {
    onResolved(conversationId, resolvedSeq)
  }, [onResolved, conversationId, resolvedSeq])

  const ctx = useMemo<AutoProjectRunPanelContextValue | null>(
    () => (run ? { autoprojectId, questions: run.questions } : null),
    [autoprojectId, run],
  )
  const chatSource = useMemo(() => conversationSource(conversationId), [conversationId])
  const notesWorkspace = useMemo(
    () => ({ conversationId, autoprojectId, onOpenFile }),
    [conversationId, autoprojectId, onOpenFile],
  )

  if (!run || !ctx) {
    let note
    if (lookup.loading) {
      note = <p className="autoproject-detail-panel-note" role="status">Loading update...</p>
    } else if (lookup.notFound) {
      note = (
        <p className="autoproject-detail-panel-note">
          This update does not exist, was deleted, or belongs to another AutoProject.
        </p>
      )
    } else {
      note = (
        <div className="autoproject-detail-panel-note" role="alert">
          <p>{lookup.error?.message || 'Failed to load the update.'}</p>
          <button type="button" className="autoproject-ctl-button" onClick={() => void lookup.refresh()}>
            Retry
          </button>
        </div>
      )
    }
    return (
      <AutoProjectDetailPanel title="Update" onClose={onClose}>
        {note}
      </AutoProjectDetailPanel>
    )
  }

  const notes =
    run.status !== 'running' && run.status !== 'waiting' && run.notes?.trim() ? run.notes : null
  const shownTab = tab === 'notes' && !notes ? 'transcript' : tab

  const ended =
    run.status === 'failed' ? (
      <AutoProjectRunError error={run.error} className="autoproject-run-panel-error" />
    ) : run.status === 'stopped' ? (
      <p className="autoproject-run-panel-ended">Stopped by you.</p>
    ) : null

  return (
    <AutoProjectDetailPanel
      title={`Update ${run.seq}`}
      meta={
        <div className="autoproject-run-panel-meta">
          <span className={`autoproject-run-panel-status ${run.status}`} role="status">
            {autoprojectRunStatusLabel(run)}
          </span>
          <time className="autoproject-run-panel-time" dateTime={run.started_at}>
            {formatTimestamp(run.started_at)}
          </time>
        </div>
      }
      toolbar={
        <div className="autoproject-run-panel-tabs" role="tablist" aria-label={`Update ${run.seq}`}>
          <button
            type="button"
            role="tab"
            aria-selected={shownTab === 'transcript'}
            className="autoproject-run-panel-tab"
            onClick={() => setTab('transcript')}
          >
            Transcript
          </button>
          {notes && (
            <button
              type="button"
              role="tab"
              aria-selected={shownTab === 'notes'}
              className="autoproject-run-panel-tab"
              onClick={() => setTab('notes')}
            >
              Handoff notes
            </button>
          )}
          <button
            type="button"
            role="tab"
            aria-selected={shownTab === 'files'}
            className="autoproject-run-panel-tab"
            onClick={() => setTab('files')}
          >
            Chat Files
          </button>
        </div>
      }
      onClose={onClose}
      body="fill"
    >
      {shownTab === 'notes' && notes ? (
        <div className="autoproject-run-panel-notes" role="tabpanel" aria-label="Handoff notes">
          <p className="autoproject-run-panel-notes-hint">
            What this update left for the next one: findings, dead ends, and where its files and
            artifacts are.
          </p>
          <div className="message-content autoproject-ctl-markdown">
            <MarkdownWorkspaceContext.Provider value={notesWorkspace}>
              <RichMarkdown components={markdownComponents}>{notes}</RichMarkdown>
            </MarkdownWorkspaceContext.Provider>
          </div>
        </div>
      ) : shownTab === 'transcript' ? (
        <AutoProjectRunPanelContext.Provider value={ctx}>
          <div className="autoproject-run-panel-transcript" role="tabpanel" aria-label="Transcript">
            <ChatPanel conversationId={conversationId} header={ended} />
          </div>
        </AutoProjectRunPanelContext.Provider>
      ) : (
        <div className="autoproject-run-panel-files" role="tabpanel" aria-label="Chat Files">
          <FileBrowser
            source={chatSource}
            hideTitle
            compact
            rowActions={copy.chatRowActions}
            notice={copy.notices.conversation}
            onDismissNotice={() => copy.dismissNotice('conversation')}
          />
        </div>
      )}
    </AutoProjectDetailPanel>
  )
}
