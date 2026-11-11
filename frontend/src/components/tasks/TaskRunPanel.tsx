import { useEffect, useMemo, useState } from 'react'
import type { Task, TaskRun } from '../../api/types'
import { conversationSource } from '../../api/fileApi'
import type { WorkspaceCopy } from '../../hooks/useWorkspaceCopy'
import { useTaskRun } from '../../hooks/useTaskRun'
import { formatTimestamp } from '../../utils/formatters'
import { ChatPanel } from '../ChatPanel'
import { FileBrowser } from '../FileBrowser'
import { TaskDetailPanel } from './TaskDetailPanel'
import { TaskRunError } from './TaskRunError'
import { TaskRunPanelContext, type TaskRunPanelContextValue } from './TaskRunPanelContext'

/** The run's state in the panel header ("Run 3 · Needs you"). */
function taskRunStatusLabel(run: Pick<TaskRun, 'status' | 'handoff_missing'>): string {
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

type RunPanelTab = 'transcript' | 'files'

/**
 * A run's transcript in the task page's detail panel (TaskDetailPanel),
 * opened by "View run" on its update or by the run URL
 * (/tasks/<id>/runs/<conversation id>, which TaskPage maps to this panel).
 *
 * Header: "Run N" with the run's live status and start time; a failed
 * run's error (TaskRunError, one line with "Show details") or a stopped
 * run's line sits above the transcript. Two tabs under the header:
 *
 * - Transcript: ChatPanel for the run's conversation, read-only through its
 *   `task` origin ("This is one run of a task. Continue in the task."), its
 *   own header suppressed. TaskRunPanelContext hands the transcript's
 *   `task_question` rows the task id and the run's questions as last
 *   listed, so a pending question is answerable here; approval and connect
 *   cards resolve through their own endpoints, as in any chat.
 * - Chat Files: the run's own workspace (FileBrowser), with the page's
 *   Copy / Move row actions to the task files (`copy`, TaskPage's
 *   useWorkspaceCopy, which also gives the main column's Task files card
 *   its actions back into this run while the panel is open).
 *
 * The run is looked up among the task's runs by its conversation
 * (useTaskRun) and re-read on every `task_changed` of the task
 * (`changeSeq`), so the status moves live and a deleted run turns into the
 * not-found state. The transcript mounts only once the conversation is
 * known to be a run of THIS task: anything else (another task's run, a
 * plain chat, a deleted run) shows a not-found note. `onResolved` tells the
 * page whether the run resolved, so the copy actions exist only beside a
 * real run.
 */
export function TaskRunPanel({
  task,
  changeSeq,
  conversationId,
  copy,
  onResolved,
  onClose,
}: {
  task: Task
  /** useTask's count of `task_changed` events: each re-reads the run. */
  changeSeq: number
  conversationId: string
  copy: WorkspaceCopy
  onResolved: (conversationId: string, resolved: boolean) => void
  onClose: () => void
}) {
  const taskId = task.id
  const lookup = useTaskRun(taskId, conversationId, changeSeq)
  const run = lookup.run
  // TaskPage keys the panel by conversation: another run starts on its transcript.
  const [tab, setTab] = useState<RunPanelTab>('transcript')

  const resolved = run !== null
  useEffect(() => {
    onResolved(conversationId, resolved)
  }, [onResolved, conversationId, resolved])

  const ctx = useMemo<TaskRunPanelContextValue | null>(
    () => (run ? { taskId, questions: run.questions } : null),
    [taskId, run],
  )
  const chatSource = useMemo(() => conversationSource(conversationId), [conversationId])

  if (!run || !ctx) {
    let note
    if (lookup.loading) {
      note = <p className="task-detail-panel-note" role="status">Loading run...</p>
    } else if (lookup.notFound) {
      note = (
        <p className="task-detail-panel-note">
          This run does not exist, was deleted, or belongs to another task.
        </p>
      )
    } else {
      note = (
        <div className="task-detail-panel-note" role="alert">
          <p>{lookup.error?.message || 'Failed to load the run.'}</p>
          <button type="button" className="task-ctl-button" onClick={() => void lookup.refresh()}>
            Retry
          </button>
        </div>
      )
    }
    return (
      <TaskDetailPanel title="Run" onClose={onClose}>
        {note}
      </TaskDetailPanel>
    )
  }

  const ended =
    run.status === 'failed' ? (
      <TaskRunError error={run.error} className="task-run-panel-error" />
    ) : run.status === 'stopped' ? (
      <p className="task-run-panel-ended">Stopped by you.</p>
    ) : null

  return (
    <TaskDetailPanel
      title={`Run ${run.seq}`}
      meta={
        <div className="task-run-panel-meta">
          <span className={`task-run-panel-status ${run.status}`} role="status">
            {taskRunStatusLabel(run)}
          </span>
          <time className="task-run-panel-time" dateTime={run.started_at}>
            {formatTimestamp(run.started_at)}
          </time>
        </div>
      }
      toolbar={
        <div className="task-run-panel-tabs" role="tablist" aria-label={`Run ${run.seq}`}>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'transcript'}
            className="task-run-panel-tab"
            onClick={() => setTab('transcript')}
          >
            Transcript
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'files'}
            className="task-run-panel-tab"
            onClick={() => setTab('files')}
          >
            Chat Files
          </button>
        </div>
      }
      onClose={onClose}
      body="fill"
    >
      {tab === 'transcript' ? (
        <TaskRunPanelContext.Provider value={ctx}>
          <div className="task-run-panel-transcript" role="tabpanel" aria-label="Transcript">
            <ChatPanel conversationId={conversationId} header={ended} />
          </div>
        </TaskRunPanelContext.Provider>
      ) : (
        <div className="task-run-panel-files" role="tabpanel" aria-label="Chat Files">
          <FileBrowser
            source={chatSource}
            hideTitle
            rowActions={copy.chatRowActions}
            notice={copy.notices.conversation}
            onDismissNotice={() => copy.dismissNotice('conversation')}
          />
        </div>
      )}
    </TaskDetailPanel>
  )
}
