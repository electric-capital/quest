import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import './TaskPage.css'
import { taskSource, type FileSource } from '../../api/fileApi'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useTask } from '../../hooks/useTask'
import { useTaskRuns } from '../../hooks/useTaskRuns'
import { useMarkTaskViewed } from '../../hooks/useTaskUnread'
import { overwritePromptText, useWorkspaceCopy } from '../../hooks/useWorkspaceCopy'
import { useWorkspaceDownload } from '../../hooks/useWorkspaceDownload'
import { taskRunPath, taskThreadPath } from '../../utils/tasksRoute'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { FileBrowser, isCsvFile, isImageFile, isJsonFile, isPdfFile, isViewableFile, type FileBrowserListing } from '../FileBrowser'
import { LazyFileViewerModal } from '../LazyFileViewerModal'
import { TaskBrief } from './TaskBrief'
import { TaskBriefHistory } from './TaskBriefHistory'
import { TaskDetailPanel } from './TaskDetailPanel'
import { TaskInstructionsPanel } from './TaskInstructionsPanel'
import { TaskInstructionsRow } from './TaskInstructionsRow'
import { TaskMainColumn } from './TaskMainColumn'
import { TaskNotice, readTaskNotice } from './TaskNotice'
import { TaskRunPanel } from './TaskRunPanel'
import { TaskLoadError, TaskNotFound, TaskViewLoading } from './TaskViewState'

/** The detail views kept in component state (a run's lives in the URL). */
type LocalPanel = { kind: 'history' } | { kind: 'instructions'; startEditing: boolean }

/**
 * Router state on a run URL pushed by this page ("View run"): closing the
 * panel then goes back in history instead of pushing the task URL, so
 * Back and the close button agree.
 */
const RUN_PANEL_FROM_PAGE = 'taskRunPanelFromPage'

function openedFromPage(state: unknown): boolean {
  return typeof state === 'object' && state !== null && (state as Record<string, unknown>)[RUN_PANEL_FROM_PAGE] === true
}

/** A workspace file open in the page's viewer: of some run, or of the task. */
interface ViewerFile {
  source: FileSource
  path: string
  filename: string
}

/**
 * The task page (/tasks/<id>, and /tasks/<id>/runs/<conversation id> with
 * that run open in the detail panel; TasksMain keys it by task id, so
 * moving between the two keeps the page).
 *
 * The main column (TaskMainColumn) holds, top to bottom: the header, the
 * instructions row (TaskInstructionsRow), the brief as the page's main
 * content (TaskBrief, edited in place), the Task files card (a FileBrowser
 * over the task workspace), the Updates (one per run, oldest first) and the
 * composer pinned at the bottom. The page opens at the top. Without a
 * detail view the column takes the whole width at the chat's reading
 * measure, centred.
 *
 * The detail panel (TaskDetailPanel) is closed by default and shows one of
 * three views at a time, beside the main column on desktop and as a
 * full-screen view with "Back to task" on phones:
 *
 * - a run's transcript (TaskRunPanel): "View run" on an update. The run is
 *   in the URL (`runConversationId`), so run links, the inbox, the
 *   `/chats/<run id>` redirect, reload and Back all work; "View run" pushes
 *   the run URL (switching from another run replaces it), and closing goes
 *   back when this page pushed it, else replaces it with the task URL. A
 *   run that is not this task's shows the panel's not-found state.
 * - the brief History (TaskBriefHistory): the brief's History button.
 * - the instructions (TaskInstructionsPanel): the instructions row.
 *
 * Closing or switching views is guarded: while the instructions editor
 * holds a changed draft the change waits (`pendingChange`) and the editor
 * asks to discard first.
 *
 * Copy / Move between a run's Chat Files (in the run panel) and the Task
 * files card (in the main column) come from one useWorkspaceCopy over the
 * run open in the panel, once it resolved; with no run open the Task files
 * card has no copy actions.
 *
 * Read state: the task counts as seen, and the unread store acknowledges
 * it (useMarkTaskViewed), only while its newest update is actually on
 * screen (TaskMainColumn's `onNewestVisibleChange`; not while the phone's
 * full-screen panel covers it) or the panel shows the newest run; a task
 * without runs counts as seen. Opening at the top therefore never marks a
 * new update read that was never shown.
 *
 * One file viewer serves the brief's and the results' workspace links
 * (`task://` and run files); the Task files card keeps its own.
 *
 * A notice carried in the router state (TaskPageLocationState, e.g. the
 * New Task page's "created, but the run did not start") shows under the
 * header until dismissed; dismissing replaces the history entry's state so
 * a reload does not bring it back.
 */
export function TaskPage({ taskId, runConversationId }: { taskId: string; runConversationId: string | null }) {
  const view = useTask(taskId)
  const runs = useTaskRuns(taskId)
  const { task, loading, error, notFound, refresh } = view
  const isMobile = useIsMobile()
  const filesHeadingId = useId()

  const location = useLocation()
  const navigate = useNavigate()
  const notice = readTaskNotice(location.state)
  const dismissNotice = useCallback(() => {
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
  }, [navigate, location.pathname, location.search])

  // --- Detail panel ------------------------------------------------------------

  const [localPanel, setLocalPanel] = useState<LocalPanel | null>(null)
  // A run in the URL takes the panel.
  useEffect(() => {
    if (runConversationId) setLocalPanel(null)
  }, [runConversationId])

  const [instructionsDirty, setInstructionsDirty] = useState(false)
  const [pendingChange, setPendingChange] = useState<{ run: () => void } | null>(null)
  const guardRef = useRef({ dirty: false, instructionsOpen: false })
  guardRef.current = {
    dirty: instructionsDirty,
    instructionsOpen: runConversationId === null && localPanel?.kind === 'instructions',
  }
  /** Run a panel change now, or after the instructions editor's discard confirm. */
  const guarded = useCallback((change: () => void) => {
    const { dirty, instructionsOpen } = guardRef.current
    if (dirty && instructionsOpen) setPendingChange({ run: change })
    else change()
  }, [])

  const fromPage = openedFromPage(location.state)
  const routeState = location.state
  const closeRunRoute = useCallback(() => {
    if (fromPage) navigate(-1)
    else navigate(taskThreadPath(taskId), { replace: true })
  }, [fromPage, navigate, taskId])

  const closePanel = useCallback(() => {
    guarded(() => {
      if (runConversationId) closeRunRoute()
      else setLocalPanel(null)
    })
  }, [guarded, runConversationId, closeRunRoute])

  const viewRun = useCallback(
    (conversationId: string) => {
      guarded(() => {
        if (runConversationId === conversationId) {
          closeRunRoute()
          return
        }
        setLocalPanel(null)
        if (runConversationId) {
          navigate(taskRunPath(taskId, conversationId), { replace: true, state: routeState })
        } else {
          navigate(taskRunPath(taskId, conversationId), { state: { [RUN_PANEL_FROM_PAGE]: true } })
        }
      })
    },
    [guarded, runConversationId, closeRunRoute, navigate, taskId, routeState],
  )

  const openLocal = useCallback(
    (next: LocalPanel) => {
      guarded(() => {
        if (runConversationId) closeRunRoute()
        // The same view again closes it (History and the row toggle).
        setLocalPanel((current) =>
          !runConversationId && current?.kind === next.kind && !(next.kind === 'instructions' && next.startEditing)
            ? null
            : next,
        )
      })
    },
    [guarded, runConversationId, closeRunRoute],
  )
  const openHistory = useCallback(() => openLocal({ kind: 'history' }), [openLocal])
  const openInstructions = useCallback(
    (startEditing: boolean) => openLocal({ kind: 'instructions', startEditing }),
    [openLocal],
  )

  const confirmPendingChange = useCallback(() => {
    const change = pendingChange
    setPendingChange(null)
    guardRef.current.dirty = false
    setInstructionsDirty(false)
    change?.run()
  }, [pendingChange])
  const cancelPendingChange = useCallback(() => setPendingChange(null), [])

  // --- Run panel resolution and the files copy -----------------------------------

  // The run panel's conversation once it resolved as a run of this task.
  const [resolvedRun, setResolvedRun] = useState<string | null>(null)
  const handleRunResolved = useCallback((conversationId: string, resolved: boolean) => {
    setResolvedRun((prev) => (resolved ? conversationId : prev === conversationId ? null : prev))
  }, [])
  const panelRun = runConversationId !== null && resolvedRun === runConversationId ? runConversationId : null

  const filesSource = useMemo(() => taskSource(taskId), [taskId])
  const copy = useWorkspaceCopy(panelRun, filesSource)
  const prompt = copy.overwritePrompt
  const promptText = prompt ? overwritePromptText(prompt) : null

  // Entries at the task workspace root, from the card's own listing; null
  // until it listed the root (it may restore a subfolder it was left in).
  const [rootEntryCount, setRootEntryCount] = useState<number | null>(null)
  const handleFilesListing = useCallback((listing: FileBrowserListing) => {
    if (listing.path === '/') setRootEntryCount(listing.count)
  }, [])

  // --- Read state ------------------------------------------------------------------

  const [newestVisible, setNewestVisible] = useState(false)
  const runList = runs.runs
  const newestRun = runList.length > 0 ? runList[runList.length - 1] : null
  const panelCoversColumn = isMobile && (runConversationId !== null || localPanel !== null)
  const newestSeen =
    task !== null &&
    !runs.loading &&
    (newestRun === null
      ? runs.error === null
      : (newestVisible && !panelCoversColumn) || panelRun === newestRun.conversation_id)
  useMarkTaskViewed(taskId, newestSeen)

  // --- File viewer for the brief's and the results' links --------------------------

  const downloadWorkspaceFile = useWorkspaceDownload()
  const [viewer, setViewer] = useState<ViewerFile | null>(null)
  const handleOpenFile = useCallback(
    (path: string, filename: string, source: FileSource) => {
      if (isViewableFile(filename)) {
        setViewer({ source, path, filename })
      } else {
        void downloadWorkspaceFile(source, path).catch(() => {
          // Swallow; the link stays clickable and the user can retry.
        })
      }
    },
    [downloadWorkspaceFile],
  )
  const handleDownloadViewerFile = useCallback(async () => {
    if (!viewer) return
    try {
      await downloadWorkspaceFile(viewer.source, viewer.path)
    } catch {
      // Swallow; the modal stays open and the user can retry.
    }
  }, [downloadWorkspaceFile, viewer])

  // --- Render ------------------------------------------------------------------------

  if (loading) {
    return <TaskViewLoading text="Loading task..." />
  }

  if (notFound) {
    return <TaskNotFound />
  }

  if (!task) {
    return <TaskLoadError message={error?.message || 'Failed to load the task.'} onRetry={refresh} />
  }

  const panelKind = runConversationId ? 'run' : localPanel?.kind ?? null

  const lead = (
    <>
      <TaskInstructionsRow
        instructions={task.instructions}
        open={panelKind === 'instructions'}
        onOpen={openInstructions}
      />
      <TaskBrief
        task={task}
        onBrief={view.applyBrief}
        onStale={() => void refresh()}
        historyOpen={panelKind === 'history'}
        onOpenHistory={openHistory}
        onOpenFile={handleOpenFile}
      />
      <section className="task-section task-files" aria-labelledby={filesHeadingId}>
        <div className="task-section-head">
          <h2 id={filesHeadingId} className="task-section-heading">
            Task files
          </h2>
          <p className="task-section-meta">{taskFilesSummary(rootEntryCount)}</p>
        </div>
        <div className="task-files-card">
          <FileBrowser
            source={filesSource}
            hideTitle
            onListing={handleFilesListing}
            rowActions={copy.sharedRowActions}
            notice={copy.notices.task}
            onDismissNotice={() => copy.dismissNotice('task')}
          />
        </div>
      </section>
    </>
  )

  let panel = null
  if (runConversationId) {
    panel = (
      <TaskRunPanel
        key={runConversationId}
        task={task}
        changeSeq={view.changeSeq}
        conversationId={runConversationId}
        copy={copy}
        onResolved={handleRunResolved}
        onClose={closePanel}
      />
    )
  } else if (localPanel?.kind === 'history') {
    panel = (
      <TaskDetailPanel key="history" title="Brief history" onClose={closePanel}>
        <TaskBriefHistory task={task} onRestored={view.applyBrief} onStale={() => void refresh()} />
      </TaskDetailPanel>
    )
  } else if (localPanel?.kind === 'instructions') {
    panel = (
      <TaskDetailPanel key="instructions" title="Instructions" onClose={closePanel}>
        <TaskInstructionsPanel
          task={task}
          onSaved={view.applyDetail}
          startEditing={localPanel.startEditing}
          onDirtyChange={setInstructionsDirty}
          closePending={pendingChange !== null}
          onConfirmClose={confirmPendingChange}
          onCancelClose={cancelPendingChange}
        />
      </TaskDetailPanel>
    )
  }

  return (
    <div className={`task-page${panel ? ' has-panel' : ''}`}>
      <TaskMainColumn
        taskId={taskId}
        task={task}
        runs={runs}
        onTaskDetail={view.applyDetail}
        notice={
          notice ? (
            <div className="task-page-notice">
              <TaskNotice message={notice} onDismiss={dismissNotice} />
            </div>
          ) : null
        }
        lead={lead}
        openRunConversationId={runConversationId}
        onViewRun={viewRun}
        onOpenFile={handleOpenFile}
        onNewestVisibleChange={setNewestVisible}
      />
      {panel}

      <DocConfirmDialog
        isOpen={prompt !== null}
        title={promptText?.title ?? ''}
        confirmLabel={promptText?.confirmLabel ?? 'Replace'}
        busyLabel="Replacing..."
        tone="danger"
        busy={copy.overwriteBusy}
        error={copy.overwriteError}
        onConfirm={copy.confirmOverwrite}
        onClose={copy.cancelOverwrite}
      >
        {promptText && <p>{promptText.body}</p>}
      </DocConfirmDialog>

      {viewer && (
        <LazyFileViewerModal
          isOpen
          source={viewer.source}
          filePath={viewer.path}
          fileName={viewer.filename}
          isImage={isImageFile(viewer.filename)}
          isJson={isJsonFile(viewer.filename)}
          isPdf={isPdfFile(viewer.filename)}
          isCsv={isCsvFile(viewer.filename)}
          onClose={() => setViewer(null)}
          onDownload={handleDownloadViewerFile}
        />
      )}
    </div>
  )
}

/** The Task files heading's meta: what the workspace root holds. */
function taskFilesSummary(rootEntryCount: number | null): string {
  if (rootEntryCount === null || rootEntryCount === 0) return 'Shared by every run'
  return `${rootEntryCount} item${rootEntryCount === 1 ? '' : 's'} · shared by every run`
}
