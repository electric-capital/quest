import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import '../ChatPanel.css'
import './TaskThread.css'
import { ApiClientError } from '../../api/request'
import { answerTaskQuestion, deleteTaskRun, unarchiveTask } from '../../api/tasksApi'
import type { Task, TaskRun } from '../../api/types'
import { taskDraftKey } from '../../constants/drafts'
import { useConversationSkills } from '../../contexts/ConversationSkillsContext'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useTaskRunActivity } from '../../hooks/useTaskRunActivity'
import type { TaskRunsView } from '../../hooks/useTaskRuns'
import { useWorkspaceDownload } from '../../hooks/useWorkspaceDownload'
import { persistentWebSocket } from '../../services/PersistentWebSocket'
import { stopTaskRun, taskRunStartErrorMessage, TaskRunStartError } from '../../services/taskRuns'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { isCsvFile, isImageFile, isJsonFile, isPdfFile, isViewableFile } from '../FileBrowser'
import { LazyFileViewerModal } from '../LazyFileViewerModal'
import { Message } from '../Message'
import { UploadProgress, type UploadStatus } from '../UploadProgress'
import { TaskActivityLine } from './TaskActivityLine'
import { TaskComposer } from './TaskComposer'
import { TaskNotice } from './TaskNotice'
import {
  TaskRunEntry,
  taskRunCardKey,
  type TaskRunActivityView,
  type TaskRunCard,
} from './TaskRunEntry'
import {
  initialUploadStatus,
  startTaskRunFromComposer,
  type TaskComposerSubmission,
} from './taskComposerSend'
import {
  deriveTaskThreadComposer,
  findActiveRun,
  isActiveRunStatus,
  oldestPendingQuestion,
} from './taskThreadComposer'

/** Distance (px) from the bottom within which the thread counts as pinned there. */
const NEAR_BOTTOM_PX = 80

/** A run being started from the composer, shown until its run is in the runs list. */
interface PendingStart {
  key: number
  prompt: string
  startedAt: string
  /** Known once the start resolved. */
  conversationId: string | null
}

/** A workspace file of some run open in the thread's viewer. */
interface ViewerFile {
  conversationId: string
  path: string
  filename: string
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

function errorCode(err: unknown): string | null {
  return err instanceof ApiClientError ? err.errorCode ?? null : null
}

/**
 * Cards kept for one active run: those seen before stay (they flip
 * themselves once resolved), new open ones are appended. Returns `kept`
 * itself when nothing is new.
 */
function mergeCards(kept: TaskRunCard[], open: TaskRunCard[]): TaskRunCard[] {
  const seen = new Set(kept.map(taskRunCardKey))
  const added = open.filter((card) => !seen.has(taskRunCardKey(card)))
  return added.length === 0 ? kept : [...kept, ...added]
}

// --- Scroll helpers --------------------------------------------------------
// The thread scrolls in its own scroller on desktop; on phones the scroller
// is not scrollable and the whole pane (.docs-main) scrolls instead, with the
// composer sticky at the bottom of the viewport. The helpers below measure
// against whichever element actually scrolls.

function isDocumentScroller(el: Element): boolean {
  return el === document.scrollingElement || el === document.documentElement
}

/** The nearest ancestor (or `el` itself) that scrolls vertically. */
function scrollParentOf(el: HTMLElement): HTMLElement {
  let node: HTMLElement | null = el
  while (node) {
    const overflowY = window.getComputedStyle(node).overflowY
    if (overflowY === 'auto' || overflowY === 'scroll') return node
    node = node.parentElement
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement
}

/** Top edge of the scroller's visible area and its bottom edge, in viewport coordinates. */
function viewportOf(parent: HTMLElement): { top: number; bottom: number } {
  if (isDocumentScroller(parent)) return { top: 0, bottom: window.innerHeight }
  const rect = parent.getBoundingClientRect()
  return { top: rect.top, bottom: rect.bottom }
}

/** `el`'s top in the scroller's content coordinates (independent of the scroll position). */
function contentTop(el: Element, parent: HTMLElement): number {
  return el.getBoundingClientRect().top - viewportOf(parent).top + parent.scrollTop
}

/**
 * The task thread (spec 8.2): the runs oldest first in a scroller, older
 * pages loading as the top comes near, and the composer pinned under it.
 *
 * Runs (TaskRunEntry) show the prompt, questions, the approval and connect
 * cards, then the activity line (the active run, fed by builder A's
 * useTaskRunActivity, called once here for the active run only) or the
 * outcome. A run started from the composer shows at once as a pending entry
 * (the prompt plus a "Starting" line) until the runs list holds a run with
 * its conversation id.
 *
 * Cards: `open_requests` lists only OPEN cards, so a card resolved here
 * would vanish on the next refresh. The thread keeps every card it has seen
 * for a run while that run is active (the card component flips itself to
 * its resolved state) and drops them once the run finished. Resolving a
 * card publishes `wait_handle_resolved`; when it names the active run's
 * conversation the runs are refreshed (task_changed follows the status
 * change as well).
 *
 * Scroll anchoring, as in the chat: the first load lands at the bottom; new
 * content keeps the view at the bottom while the user was there (a
 * ResizeObserver also catches late growth such as images and code
 * highlighting); a prepended older page keeps the visible position by
 * measuring the previously first run before and after the commit, in a
 * layout effect. The browser's own scroll anchoring is off on the list
 * (CSS) so the two never add up.
 *
 * The composer's state comes from deriveTaskThreadComposer
 * (taskThreadComposer.ts): start a run, answer the pending question, or
 * locked with Stop; an archived task gets an Unarchive notice instead.
 */
export function TaskThread({
  taskId,
  task,
  runs,
  onTaskDetail,
}: {
  taskId: string
  task: Task
  runs: TaskRunsView
  /** Merge a Task returned by unarchive into the page's task view. */
  onTaskDetail?: (task: Task) => void
}) {
  const draftKey = taskDraftKey(taskId)
  const { setQueuedSkillsForConversation } = useConversationSkills()
  const downloadWorkspaceFile = useWorkspaceDownload()
  const isMobile = useIsMobile()

  const [pendingStart, setPendingStart] = useState<PendingStart | null>(null)
  const pendingKeyRef = useRef(0)
  const submittingRef = useRef(false)
  const [answeringHandleId, setAnsweringHandleId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [uploadStatus, setUploadStatus] = useState<UploadStatus | null>(null)
  const [stopping, setStopping] = useState<string | null>(null)
  const [stopError, setStopError] = useState<string | null>(null)
  const [keptCards, setKeptCards] = useState<Record<string, TaskRunCard[]>>({})
  const [viewer, setViewer] = useState<ViewerFile | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<TaskRun | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [unarchiving, setUnarchiving] = useState(false)
  const [unarchiveError, setUnarchiveError] = useState<string | null>(null)

  const runList = runs.runs
  const { refresh, removeRun, loadOlder } = runs

  // The pending entry shows until its run is in the list.
  const pendingVisible =
    pendingStart !== null &&
    !(
      pendingStart.conversationId !== null &&
      runList.some((run) => run.conversation_id === pendingStart.conversationId)
    )

  const composer = deriveTaskThreadComposer({
    archived: task.archived,
    runs: runList,
    taskActiveRun: task.active_run,
    starting: pendingVisible,
    answering: answeringHandleId !== null,
  })
  const composerRef = useRef(composer)
  composerRef.current = composer

  const activeRun = findActiveRun(runList)
  const activeConversationId = activeRun?.conversation_id ?? null
  const activity = useTaskRunActivity(activeConversationId)
  const activeConversationRef = useRef(activeConversationId)
  activeConversationRef.current = activeConversationId

  // --- Pending start, stop and kept cards bookkeeping ----------------------

  useEffect(() => {
    if (pendingStart && !pendingVisible) setPendingStart(null)
  }, [pendingStart, pendingVisible])

  // "Stopping..." holds until the run leaves the active state.
  useEffect(() => {
    if (stopping !== null && stopping !== activeConversationId) setStopping(null)
    if (activeConversationId === null) setStopError(null)
  }, [stopping, activeConversationId])

  useEffect(() => {
    setKeptCards((prev) => {
      const next: Record<string, TaskRunCard[]> = {}
      let changed = false
      for (const run of runList) {
        if (!isActiveRunStatus(run.status)) continue
        const before = prev[run.id] ?? []
        const merged = mergeCards(before, run.open_requests)
        next[run.id] = merged
        if (merged !== before || !(run.id in prev)) changed = true
      }
      if (Object.keys(prev).some((id) => !(id in next))) changed = true
      return changed ? next : prev
    })
  }, [runList])

  // A card of the active run was resolved (approved, denied, connected...):
  // refresh so the run's status and cards follow.
  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'wait_handle_resolved') return
      const conversationId = event.conversation_id
      if (typeof conversationId === 'string' && conversationId === activeConversationRef.current) {
        void refresh()
      }
    })
  }, [refresh])

  // --- Scrolling ------------------------------------------------------------

  const sectionRef = useRef<HTMLElement>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const footerRef = useRef<HTMLDivElement>(null)
  const sentinelRef = useRef<HTMLDivElement>(null)
  const parentRef = useRef<HTMLElement | null>(null)
  const pinnedRef = useRef(true)
  const anchorRef = useRef<{ runId: string; top: number } | null>(null)
  const [parentEl, setParentEl] = useState<HTMLElement | null>(null)

  /** How far the end of the runs sits below the visible bottom (above the composer). */
  const measure = useCallback((): { distance: number; nearBottom: boolean } | null => {
    const parent = parentRef.current
    const end = endRef.current
    if (!parent || !end) return null
    const view = viewportOf(parent)
    let limit = view.bottom
    let footerAtBottom = true
    const footer = footerRef.current
    if (footer) {
      const rect = footer.getBoundingClientRect()
      limit = Math.min(rect.top, view.bottom)
      // On phones the sticky composer leaves the viewport bottom once the
      // user scrolled on into the panel below the thread.
      footerAtBottom = rect.bottom >= view.bottom - 2
    }
    const distance = end.getBoundingClientRect().bottom - limit
    return { distance, nearBottom: footerAtBottom && distance < NEAR_BOTTOM_PX }
  }, [])

  const scrollParentBy = useCallback((delta: number) => {
    const parent = parentRef.current
    if (!parent || delta === 0) return
    if (isDocumentScroller(parent)) window.scrollBy(0, delta)
    else parent.scrollTop += delta
  }, [])

  const recordAnchor = useCallback(() => {
    const parent = parentRef.current
    const list = listRef.current
    const first = list?.querySelector<HTMLElement>('[data-run-id]')
    const runId = first?.dataset.runId
    anchorRef.current = parent && first && runId ? { runId, top: contentTop(first, parent) } : null
  }, [])

  /** Bring the end of the runs to the bottom (a few passes: a sticky composer moves as we scroll). */
  const scrollToBottom = useCallback(() => {
    for (let i = 0; i < 3; i += 1) {
      const m = measure()
      if (!m || Math.abs(m.distance) < 1) break
      scrollParentBy(m.distance)
    }
    pinnedRef.current = true
  }, [measure, scrollParentBy])

  // Resolve the element that scrolls (it changes at the phone breakpoint).
  useLayoutEffect(() => {
    const section = sectionRef.current
    const scroller = scrollerRef.current
    if (!section || !scroller) return
    const parent = scrollParentOf(scroller)
    parentRef.current = parent
    setParentEl(parent)
  }, [isMobile])

  // Track whether the user is at the bottom, and the first run's position.
  useEffect(() => {
    if (!parentEl) return
    const target: HTMLElement | Window = isDocumentScroller(parentEl) ? window : parentEl
    const onScroll = () => {
      const m = measure()
      if (m) pinnedRef.current = m.nearBottom
      recordAnchor()
    }
    target.addEventListener('scroll', onScroll, { passive: true })
    return () => target.removeEventListener('scroll', onScroll)
  }, [parentEl, measure, recordAnchor])

  // After every commit: keep the position across a prepended older page,
  // or stay at the bottom when the user was there.
  useLayoutEffect(() => {
    const parent = parentRef.current
    const list = listRef.current
    const prev = anchorRef.current
    if (parent && list && prev && !pinnedRef.current) {
      const firstId = list.querySelector<HTMLElement>('[data-run-id]')?.dataset.runId
      if (firstId && firstId !== prev.runId) {
        const old = list.querySelector(`[data-run-id="${CSS.escape(prev.runId)}"]`)
        if (old) scrollParentBy(contentTop(old, parent) - prev.top)
      }
    }
    if (pinnedRef.current) scrollToBottom()
    recordAnchor()
  })

  // Late growth (images, code highlighting, a card expanding) while pinned.
  useEffect(() => {
    const list = listRef.current
    if (!list || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (pinnedRef.current) scrollToBottom()
      recordAnchor()
    })
    observer.observe(list)
    return () => observer.disconnect()
  }, [scrollToBottom, recordAnchor])

  // Older pages: load when the sentinel above the first run comes near.
  // The observer is (re)created whenever no page is loading, and an
  // observer reports the sentinel's state once on creation, so a sentinel
  // still in view after a page landed (a short thread) loads the next one.
  const hasOlder = runs.hasOlder
  const loadingOlder = runs.loadingOlder
  const hasRuns = runList.length > 0
  const loadOlderRef = useRef(loadOlder)
  loadOlderRef.current = loadOlder
  useEffect(() => {
    const sentinel = sentinelRef.current
    if (!sentinel || !parentEl || !hasOlder || loadingOlder) return
    if (typeof IntersectionObserver === 'undefined') return
    let requested = false
    const observer = new IntersectionObserver(
      (entries) => {
        if (requested || !entries.some((entry) => entry.isIntersecting)) return
        requested = true
        loadOlderRef.current()
      },
      {
        root: isDocumentScroller(parentEl) ? null : parentEl,
        rootMargin: '200px 0px 0px 0px',
      },
    )
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [parentEl, hasOlder, hasRuns, loadingOlder])

  // --- Actions ----------------------------------------------------------------

  const restoreSkills = useCallback(
    (skillIds: string[]) => {
      if (skillIds.length > 0) setQueuedSkillsForConversation(draftKey, skillIds)
    },
    [draftKey, setQueuedSkillsForConversation],
  )

  const startRun = useCallback(
    async (submission: TaskComposerSubmission) => {
      if (submittingRef.current) return
      if (!submission.text) {
        setNotice('Write a prompt to start a run.')
        return
      }
      submittingRef.current = true
      const key = ++pendingKeyRef.current
      setNotice(null)
      setPendingStart({
        key,
        prompt: submission.text,
        startedAt: new Date().toISOString(),
        conversationId: null,
      })
      setUploadStatus(initialUploadStatus(submission))
      pinnedRef.current = true

      const warnings: string[] = []
      try {
        const started = await startTaskRunFromComposer(taskId, submission, {
          onUploadStatus: setUploadStatus,
          onWarning: (message) => warnings.push(message),
        })
        setPendingStart((p) =>
          p && p.key === key ? { ...p, conversationId: started.conversationId } : p,
        )
        if (warnings.length > 0) setNotice(warnings.join(' '))
        void refresh()
      } catch (err) {
        setPendingStart((p) => (p && p.key === key ? null : p))
        setNotice(taskRunStartErrorMessage(err))
        restoreSkills(submission.skillIds)
        // Someone else's start won the race: show that run.
        if (err instanceof TaskRunStartError && err.reason === 'task_run_active') void refresh()
      } finally {
        submittingRef.current = false
        setUploadStatus(null)
      }
    },
    [taskId, refresh, restoreSkills],
  )

  const answer = useCallback(
    async (submission: TaskComposerSubmission, handleId: string) => {
      // Skills cannot ride an answer; put them back for the next run.
      restoreSkills(submission.skillIds)
      if (submittingRef.current) return
      const hasAttachments = submission.files.length > 0 || submission.imageFiles.length > 0
      if (!submission.text) {
        if (hasAttachments) setNotice('Attachments are not sent with an answer.')
        return
      }
      submittingRef.current = true
      setNotice(hasAttachments ? 'Attachments are not sent with an answer.' : null)
      setAnsweringHandleId(handleId)
      pinnedRef.current = true
      try {
        await answerTaskQuestion(taskId, handleId, submission.text)
        await refresh()
      } catch (err) {
        if (errorCode(err) === 'question_resolved') {
          setNotice('This question was already answered or cancelled.')
          void refresh()
        } else {
          setNotice(errorText(err, 'The answer could not be sent.'))
        }
      } finally {
        submittingRef.current = false
        setAnsweringHandleId(null)
      }
    },
    [taskId, refresh, restoreSkills],
  )

  const handleSubmit = useCallback(
    (submission: TaskComposerSubmission) => {
      const current = composerRef.current
      if (current.mode === 'answer' && current.question) {
        void answer(submission, current.question.handle_id)
      } else if (current.mode === 'start' && !current.busy) {
        void startRun(submission)
      }
    },
    [answer, startRun],
  )

  const handleStop = useCallback((conversationId: string) => {
    if (!stopTaskRun(conversationId)) {
      setStopError('Not connected')
      return
    }
    setStopError(null)
    setStopping(conversationId)
  }, [])

  const stopConversationId = composer.stopConversationId
  const handleComposerStop = useCallback(() => {
    if (stopConversationId) handleStop(stopConversationId)
  }, [stopConversationId, handleStop])

  const handleUnarchive = useCallback(async () => {
    setUnarchiving(true)
    setUnarchiveError(null)
    try {
      const updated = await unarchiveTask(taskId)
      onTaskDetail?.(updated)
    } catch (err) {
      setUnarchiveError(errorText(err, 'The task could not be unarchived.'))
    } finally {
      setUnarchiving(false)
    }
  }, [taskId, onTaskDetail])

  const openDelete = useCallback((run: TaskRun) => {
    setDeleteError(null)
    setDeleteTarget(run)
  }, [])

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return
    const run = deleteTarget
    setDeleting(true)
    setDeleteError(null)
    try {
      await deleteTaskRun(taskId, run.id)
      removeRun(run.id)
      setDeleteTarget(null)
    } catch (err) {
      const code = errorCode(err)
      if (code === 'task_run_not_found') {
        // Already gone (another tab): drop it here too.
        removeRun(run.id)
        setDeleteTarget(null)
      } else if (code === 'task_run_active') {
        setDeleteError('This run is still active. Stop it before deleting its entry.')
      } else {
        setDeleteError(errorText(err, 'The entry could not be deleted.'))
      }
    } finally {
      setDeleting(false)
    }
  }, [deleteTarget, taskId, removeRun])

  // One viewer for the whole thread; each run's markdown opens files of
  // its own conversation workspace (same rule as ChatPanel).
  const handleOpenFile = useCallback(
    (conversationId: string, path: string, filename: string) => {
      if (isViewableFile(filename)) {
        setViewer({ conversationId, path, filename })
      } else {
        void downloadWorkspaceFile(conversationId, path).catch(() => {
          // Swallow; the link stays clickable and the user can retry.
        })
      }
    },
    [downloadWorkspaceFile],
  )

  const handleDownloadViewerFile = useCallback(async () => {
    if (!viewer) return
    try {
      await downloadWorkspaceFile(viewer.conversationId, viewer.path)
    } catch {
      // Swallow; the modal stays open and the user can retry.
    }
  }, [downloadWorkspaceFile, viewer])

  const handleQuestionAnswered = useCallback(() => {
    void refresh()
  }, [refresh])

  // --- Render -----------------------------------------------------------------

  const activeActivity = useMemo((): TaskRunActivityView | null => {
    if (!activeRun) return null
    const waiting = activeRun.status === 'waiting'
    const label = waiting
      ? oldestPendingQuestion(activeRun)
        ? 'Waiting for your answer'
        : 'Waiting for your approval'
      : activity.label
    return {
      label,
      waiting,
      stopping: stopping === activeRun.conversation_id,
      stopError,
    }
  }, [activeRun, activity.label, stopping, stopError])

  let body
  if (runs.loading && runList.length === 0) {
    body = <p className="task-thread-note">Loading runs...</p>
  } else if (runs.error && runList.length === 0) {
    body = (
      <div className="task-thread-note" role="alert">
        <p className="task-thread-note-text">{runs.error.message || 'Failed to load the runs.'}</p>
        <button type="button" className="task-thread-pill-button" onClick={() => void refresh()}>
          Retry
        </button>
      </div>
    )
  } else if (runList.length === 0 && !pendingVisible) {
    body = (
      <p className="task-thread-note">
        {task.archived ? 'No runs yet.' : 'No runs yet. Send a prompt to start the first run.'}
      </p>
    )
  } else {
    body = (
      <>
        {runList.map((run) => (
          <TaskRunEntry
            key={run.id}
            taskId={taskId}
            run={run}
            cards={
              isActiveRunStatus(run.status)
                ? keptCards[run.id] ?? run.open_requests
                : run.open_requests
            }
            activity={run.id === activeRun?.id ? activeActivity : null}
            answeringHandleId={answeringHandleId}
            onStop={handleStop}
            onQuestionAnswered={handleQuestionAnswered}
            onOpenFile={handleOpenFile}
            onDelete={openDelete}
          />
        ))}
        {pendingVisible && pendingStart && (
          <article className="task-run pending" aria-label="New run">
            <div className="task-run-meta">
              <span className="task-run-seq">New run</span>
            </div>
            <Message role="user" content={pendingStart.prompt} timestamp={pendingStart.startedAt} />
            <TaskActivityLine label="Starting" startedAt={pendingStart.startedAt} />
          </article>
        )}
      </>
    )
  }

  return (
    <section className="task-thread" aria-label="Runs" ref={sectionRef}>
      <div className="task-thread-scroller" ref={scrollerRef}>
        <div className="task-thread-list" ref={listRef}>
          {hasOlder && hasRuns && (
            <div className="task-thread-older" ref={sentinelRef}>
              <span className={loadingOlder ? '' : 'task-thread-older-idle'}>
                Loading older runs...
              </span>
            </div>
          )}
          {body}
          <div className="task-thread-end" ref={endRef} />
        </div>
      </div>

      <div className="task-thread-footer" ref={footerRef}>
        {composer.mode === 'archived' ? (
          <div className="task-thread-archived">
            <span className="task-thread-archived-text">This task is archived.</span>
            <button
              type="button"
              className="task-thread-pill-button"
              onClick={() => void handleUnarchive()}
              disabled={unarchiving}
            >
              {unarchiving ? 'Unarchiving...' : 'Unarchive'}
            </button>
            {unarchiveError && (
              <div className="task-thread-archived-error">
                <TaskNotice message={unarchiveError} onDismiss={() => setUnarchiveError(null)} />
              </div>
            )}
          </div>
        ) : (
          <>
            {composer.label && (
              <div className="task-thread-composer-label">
                <span className="task-thread-composer-label-main">{composer.label}</span>
                {composer.question && (
                  <span className="task-thread-composer-label-question" title={composer.question.question}>
                    {composer.question.question}
                  </span>
                )}
              </div>
            )}
            {notice && (
              <div className="task-thread-notice">
                <TaskNotice message={notice} onDismiss={() => setNotice(null)} />
              </div>
            )}
            {uploadStatus && (
              <div className="task-thread-upload">
                <UploadProgress status={uploadStatus} />
              </div>
            )}
            <TaskComposer
              draftKey={draftKey}
              onSubmit={handleSubmit}
              busy={composer.busy}
              disabled={composer.disabled}
              onStop={composer.disabled && stopConversationId ? handleComposerStop : undefined}
              placeholder={composer.placeholder}
              initialModel={task.model}
              initialThinkingEffort={task.thinking_effort}
            />
          </>
        )}
      </div>

      <DocConfirmDialog
        isOpen={deleteTarget !== null}
        title={`Delete run ${deleteTarget?.seq ?? ''}?`}
        confirmLabel="Delete"
        busyLabel="Deleting..."
        tone="danger"
        busy={deleting}
        error={deleteError}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      >
        <p>Its log entry and transcript are deleted. Later runs no longer see its notes.</p>
      </DocConfirmDialog>

      {viewer && (
        <LazyFileViewerModal
          isOpen
          conversationId={viewer.conversationId}
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
    </section>
  )
}
