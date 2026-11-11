import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import '../ChatPanel.css'
import type { FileSource } from '../../api/fileApi'
import { ApiClientError } from '../../api/request'
import { answerTaskQuestion, deleteTaskRun, unarchiveTask, updateTask } from '../../api/tasksApi'
import type { Task, TaskRun } from '../../api/types'
import { taskDraftKey } from '../../constants/drafts'
import { useConversationSkills } from '../../contexts/ConversationSkillsContext'
import { useTaskRunActivity } from '../../hooks/useTaskRunActivity'
import type { TaskRunsView } from '../../hooks/useTaskRuns'
import { useTaskUnread } from '../../hooks/useTaskUnread'
import { persistentWebSocket } from '../../services/PersistentWebSocket'
import { stopTaskRun, taskRunStartErrorMessage, TaskRunStartError } from '../../services/taskRuns'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { UploadProgress, type UploadStatus } from '../UploadProgress'
import { TaskComposer } from './TaskComposer'
import { TaskHeader, type TaskHeaderJump, type TaskHeaderJumpTarget } from './TaskHeader'
import { TaskNotice } from './TaskNotice'
import { taskRunCardKey, type TaskRunActivityView, type TaskRunCard } from './TaskRunEntry'
import { TaskUpdates, type PendingStart } from './TaskUpdates'
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
  waitingOnPhrase,
} from './taskThreadComposer'

/** Distance (px) from the bottom within which the column counts as pinned there. */
const NEAR_BOTTOM_PX = 80
/** How far (px) an update must reach into the view to count as on screen. */
const VISIBLE_SLACK_PX = 24
/** Scrolled further than this (px) from the top, the header offers "Brief". */
const BRIEF_JUMP_AFTER_PX = 160
/** How much (px) of the panel's update must show for it not to be scrolled into view. */
const REVEAL_MIN_VISIBLE_PX = 160
/** Gap (px) left above an update scrolled to the top of the view. */
const REVEAL_MARGIN_PX = 12

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

function errorCode(err: unknown): string | null {
  return err instanceof ApiClientError ? err.errorCode ?? null : null
}

function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
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

/** `el`'s top in the scroller's content coordinates (independent of the scroll position). */
function contentTop(el: Element, scroller: HTMLElement): number {
  return el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
}

/** The updates in the section, oldest first (the last one is the newest). */
function updateElements(section: HTMLElement | null): HTMLElement[] {
  return section ? Array.from(section.querySelectorAll<HTMLElement>('.task-updates-list > .task-run')) : []
}

/**
 * The task page's main column (TaskPage), on every screen size: the
 * header, the page's notice, one scroller holding the `lead` content (the
 * instructions row, the brief and the task files, from TaskPage) and the
 * Updates section (TaskUpdates), and under the scroller the composer,
 * pinned at the bottom of the column. The page opens at the TOP, on the
 * brief.
 *
 * It owns the run side of the page: the composer's state
 * (deriveTaskThreadComposer: start a run, answer the pending question, or
 * locked with Stop; an archived task gets an Unarchive notice instead), the
 * optimistic entry of a run being started, Stop, the delete-entry confirm,
 * the activity of the active run (useTaskRunActivity, once, for that run
 * only) and the approval / connect cards kept for it.
 *
 * Cards: `open_requests` lists only OPEN cards, so a card resolved here
 * would vanish on the next refresh. The column keeps every card it has seen
 * for a run while that run is active (the card component flips itself to
 * its resolved state) and drops them once the run finished. Resolving a
 * card publishes `wait_handle_resolved`; when it names the active run's
 * conversation the runs are refreshed (task_changed follows the status
 * change as well).
 *
 * Scrolling (all in the one scroller):
 * - "Latest" / "Brief" in the header (TaskHeader `jump`): "Latest" while
 *   the newest update is below the visible area, scrolling its top into
 *   view, with a working mark while a run is active and an unread mark
 *   while the task is unread; otherwise "Brief" once scrolled down,
 *   scrolling back to the top; no button with both in view.
 * - `onNewestVisibleChange` reports whether the newest update reaches into
 *   the view, for the page's mark-as-read rule.
 * - Sending a prompt or an answer scrolls to the end, and the column then
 *   follows growth at the bottom (the run's activity, its result) for as
 *   long as the user stays there. Opening at the top never pins it.
 * - "Show earlier updates" keeps the previously first update where it was
 *   on screen once the older page is prepended (measured before and after
 *   in a layout effect; the browser's own scroll anchoring is off on the
 *   scroller, CSS, so the two never add up).
 * - `openRunConversationId` (the run in the detail panel, e.g. from a deep
 *   link) is scrolled into view when it is off screen or only peeks in at
 *   an edge, loading older pages until it is found or the pages end.
 *
 * The composer's model selector persists per task, as a conversation's
 * selector persists per conversation: it is seeded with the task's default
 * (`tasks.model` / `thinking_effort`), and a user's pick is PATCHed back as
 * the default for later runs (skipped when it equals the current one; the
 * returned Task goes to `onTaskDetail`). A refused PATCH (e.g. 400
 * `model_not_allowed`) is only logged: the run still uses the composer's
 * pick, and the send path reports a model it refuses.
 */
export function TaskMainColumn({
  taskId,
  task,
  runs,
  onTaskDetail,
  notice: pageNotice,
  lead,
  openRunConversationId,
  onViewRun,
  onOpenFile,
  onNewestVisibleChange,
}: {
  taskId: string
  task: Task
  runs: TaskRunsView
  /** Merge a Task returned by unarchive, the default-model PATCH or a header action into the page's view. */
  onTaskDetail: (task: Task) => void
  /** The page's router-state notice, shown under the header. */
  notice?: ReactNode
  /** The content above the updates: the instructions row, the brief, the task files. */
  lead: ReactNode
  /** The run whose transcript the detail panel shows, or null. */
  openRunConversationId: string | null
  /** "View run" / "Close run" on an update. */
  onViewRun: (conversationId: string) => void
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
  onNewestVisibleChange: (visible: boolean) => void
}) {
  const draftKey = taskDraftKey(taskId)
  const { setQueuedSkillsForConversation } = useConversationSkills()

  const [pendingStart, setPendingStart] = useState<PendingStart | null>(null)
  const pendingKeyRef = useRef(0)
  const submittingRef = useRef(false)
  const [answeringHandleId, setAnsweringHandleId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [uploadStatus, setUploadStatus] = useState<UploadStatus | null>(null)
  const [stopping, setStopping] = useState<string | null>(null)
  const [stopError, setStopError] = useState<string | null>(null)
  const [keptCards, setKeptCards] = useState<Record<string, TaskRunCard[]>>({})
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

  const scrollerRef = useRef<HTMLDivElement>(null)
  const columnRef = useRef<HTMLDivElement>(null)
  const updatesRef = useRef<HTMLElement>(null)
  // Following the end: only after the user scrolled there or sent something.
  const pinnedRef = useRef(false)
  // The first update before an older page is prepended, to keep it in place.
  const anchorRef = useRef<{ runId: string; top: number } | null>(null)
  // A run to scroll into view once its update is loaded (the panel's run).
  const revealRef = useRef<string | null>(openRunConversationId)
  const [scrollEndSeq, setScrollEndSeq] = useState(0)
  const [jumpTarget, setJumpTarget] = useState<TaskHeaderJumpTarget | null>(null)
  const newestVisibleRef = useRef<boolean | null>(null)
  const onNewestVisibleRef = useRef(onNewestVisibleChange)
  onNewestVisibleRef.current = onNewestVisibleChange

  const scrollToEnd = useCallback(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    scroller.scrollTop = scroller.scrollHeight
    pinnedRef.current = true
  }, [])

  // Where the newest update is relative to the view, and what follows from it.
  const sync = useCallback(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const view = scroller.getBoundingClientRect()
    const updates = updateElements(updatesRef.current)
    const newest = updates.length > 0 ? updates[updates.length - 1].getBoundingClientRect() : null
    const newestVisible =
      newest !== null && newest.top < view.bottom - VISIBLE_SLACK_PX && newest.bottom > view.top + VISIBLE_SLACK_PX
    const newestBelow = newest !== null && newest.top >= view.bottom - VISIBLE_SLACK_PX
    setJumpTarget(newestBelow ? 'latest' : scroller.scrollTop > BRIEF_JUMP_AFTER_PX ? 'brief' : null)
    if (newestVisibleRef.current !== newestVisible) {
      newestVisibleRef.current = newestVisible
      onNewestVisibleRef.current(newestVisible)
    }
  }, [])

  useEffect(() => {
    const scroller = scrollerRef.current
    const column = columnRef.current
    if (!scroller || !column) return
    let frame: number | null = null
    const schedule = () => {
      if (frame === null) {
        frame = window.requestAnimationFrame(() => {
          frame = null
          sync()
        })
      }
    }
    const onScroll = () => {
      const nearBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - NEAR_BOTTOM_PX
      pinnedRef.current = nearBottom && scroller.scrollTop > 0
      schedule()
    }
    sync()
    scroller.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', schedule)
    // Content growth (a run landing, a result streaming in, images) and the
    // scroller's own size (the detail panel opening beside it).
    const observer =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver(() => {
            if (pinnedRef.current) scrollToEnd()
            schedule()
          })
    observer?.observe(column)
    observer?.observe(scroller)
    return () => {
      scroller.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', schedule)
      observer?.disconnect()
      if (frame !== null) window.cancelAnimationFrame(frame)
    }
  }, [sync, scrollToEnd])

  // A send asked for the end.
  useLayoutEffect(() => {
    if (scrollEndSeq > 0) scrollToEnd()
  }, [scrollEndSeq, scrollToEnd])

  // Keep the previously first update in place across a prepended older page.
  useLayoutEffect(() => {
    const scroller = scrollerRef.current
    const prev = anchorRef.current
    if (!scroller || !prev) return
    const updates = updateElements(updatesRef.current)
    if (updates[0]?.dataset.runId === prev.runId) return
    anchorRef.current = null
    const old = updates.find((el) => el.dataset.runId === prev.runId)
    if (old) scroller.scrollTop += contentTop(old, scroller) - prev.top
  }, [runList])

  const handleLoadOlder = useCallback(() => {
    const scroller = scrollerRef.current
    const first = updateElements(updatesRef.current)[0]
    const runId = first?.dataset.runId
    anchorRef.current = scroller && first && runId ? { runId, top: contentTop(first, scroller) } : null
    loadOlder()
  }, [loadOlder])

  // The detail panel's run: scroll its update into view when it is off
  // screen (a deep link, the inbox), loading older pages until found.
  // Armed once per newly opened run, checked in the same commit (a run
  // opened from its own update is on screen, so nothing moves).
  const revealForRef = useRef<string | null>(openRunConversationId)
  const hasOlder = runs.hasOlder
  const loadingOlder = runs.loadingOlder
  const runsLoading = runs.loading
  useLayoutEffect(() => {
    if (openRunConversationId !== revealForRef.current) {
      revealForRef.current = openRunConversationId
      revealRef.current = openRunConversationId
    }
    const target = revealRef.current
    const scroller = scrollerRef.current
    if (!target || !scroller || runsLoading) return
    const el = updateElements(updatesRef.current).find((u) => u.dataset.conversationId === target)
    if (el) {
      revealRef.current = null
      const view = scroller.getBoundingClientRect()
      const rect = el.getBoundingClientRect()
      // Barely peeking in at an edge counts as off screen.
      if (rect.bottom <= view.top + REVEAL_MIN_VISIBLE_PX || rect.top >= view.bottom - REVEAL_MIN_VISIBLE_PX) {
        anchorRef.current = null
        scroller.scrollTop = Math.max(0, contentTop(el, scroller) - REVEAL_MARGIN_PX)
      }
      return
    }
    if (hasOlder) {
      if (!loadingOlder) loadOlder()
      return
    }
    revealRef.current = null
  }, [runList, runsLoading, hasOlder, loadingOlder, loadOlder, openRunConversationId])

  const handleJump = useCallback((target: TaskHeaderJumpTarget) => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const behavior: ScrollBehavior = prefersReducedMotion() ? 'auto' : 'smooth'
    if (target === 'brief') {
      scroller.scrollTo({ top: 0, behavior })
      return
    }
    const updates = updateElements(updatesRef.current)
    const newest = updates[updates.length - 1]
    if (!newest) return
    const max = scroller.scrollHeight - scroller.clientHeight
    scroller.scrollTo({ top: Math.min(max, contentTop(newest, scroller) - REVEAL_MARGIN_PX), behavior })
  }, [])

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
      setScrollEndSeq((n) => n + 1)

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
      setScrollEndSeq((n) => n + 1)
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
      onTaskDetail(updated)
    } catch (err) {
      setUnarchiveError(errorText(err, 'The task could not be unarchived.'))
    } finally {
      setUnarchiving(false)
    }
  }, [taskId, onTaskDetail])

  // The task's default model follows the composer's picks. The ref holds the
  // latest default so a pick compares against it, not a stale closure; the
  // seq drops a response overtaken by a later pick.
  const taskModelRef = useRef({ model: task.model, thinkingEffort: task.thinking_effort })
  taskModelRef.current = { model: task.model, thinkingEffort: task.thinking_effort }
  const modelPatchSeqRef = useRef(0)
  const handleModelChange = useCallback(
    (model: string, thinkingEffort: string | null) => {
      const current = taskModelRef.current
      if (current.model === model && (current.thinkingEffort ?? null) === thinkingEffort) return
      const seq = ++modelPatchSeqRef.current
      updateTask(taskId, { model, thinking_effort: thinkingEffort }).then(
        (updated) => {
          if (seq === modelPatchSeqRef.current) onTaskDetail(updated)
        },
        (err: unknown) => console.warn('Could not save the task default model', err),
      )
    },
    [taskId, onTaskDetail],
  )

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
        : `Waiting for ${waitingOnPhrase(activeRun)}`
      : activity.label
    return {
      label,
      waiting,
      stopping: stopping === activeRun.conversation_id,
      stopError,
    }
  }, [activeRun, activity.label, stopping, stopError])

  const unread = useTaskUnread(task)
  const working = task.active_run !== null || pendingVisible
  const jump: TaskHeaderJump | undefined = jumpTarget
    ? {
        target: jumpTarget,
        onJump: handleJump,
        mark: jumpTarget !== 'latest' ? null : working ? 'working' : unread ? 'unread' : null,
      }
    : undefined

  return (
    <div className="task-page-main">
      <TaskHeader task={task} onTaskDetail={onTaskDetail} jump={jump} />
      {pageNotice}
      <div className="task-page-scroller" ref={scrollerRef}>
        <div className="task-page-column" ref={columnRef}>
          {lead}
          <TaskUpdates
            ref={updatesRef}
            taskId={taskId}
            archived={task.archived}
            runs={runs}
            pendingStart={pendingVisible ? pendingStart : null}
            keptCards={keptCards}
            activeRunId={activeRun?.id ?? null}
            activity={activeActivity}
            answeringHandleId={answeringHandleId}
            openRunConversationId={openRunConversationId}
            onLoadOlder={handleLoadOlder}
            onStop={handleStop}
            onQuestionAnswered={handleQuestionAnswered}
            onOpenFile={onOpenFile}
            onDelete={openDelete}
            onView={onViewRun}
          />
        </div>
      </div>

      <div className="task-page-footer">
        <div className="task-page-footer-column">
          {composer.mode === 'archived' ? (
            <div className="task-composer-archived">
              <span className="task-composer-archived-text">This task is archived.</span>
              <button
                type="button"
                className="task-ctl-button"
                onClick={() => void handleUnarchive()}
                disabled={unarchiving}
              >
                {unarchiving ? 'Unarchiving...' : 'Unarchive'}
              </button>
              {unarchiveError && (
                <div className="task-composer-archived-error">
                  <TaskNotice message={unarchiveError} onDismiss={() => setUnarchiveError(null)} />
                </div>
              )}
            </div>
          ) : (
            <>
              {composer.label && (
                <div className="task-composer-label">
                  <span className="task-composer-label-main">{composer.label}</span>
                  {composer.question && (
                    <span className="task-composer-label-question" title={composer.question.question}>
                      {composer.question.question}
                    </span>
                  )}
                </div>
              )}
              {notice && (
                <div className="task-composer-notice">
                  <TaskNotice message={notice} onDismiss={() => setNotice(null)} />
                </div>
              )}
              {uploadStatus && (
                <div className="task-composer-upload">
                  <UploadProgress status={uploadStatus} />
                </div>
              )}
            </>
          )}
        </div>
        {composer.mode !== 'archived' && (
          <TaskComposer
            draftKey={draftKey}
            onSubmit={handleSubmit}
            busy={composer.busy}
            disabled={composer.disabled}
            onStop={composer.disabled && stopConversationId ? handleComposerStop : undefined}
            placeholder={composer.placeholder}
            initialModel={task.model}
            initialThinkingEffort={task.thinking_effort}
            onModelChange={handleModelChange}
          />
        )}
      </div>

      <DocConfirmDialog
        isOpen={deleteTarget !== null}
        title={`Delete the entry of run ${deleteTarget?.seq ?? ''}?`}
        confirmLabel="Delete entry"
        busyLabel="Deleting..."
        tone="danger"
        busy={deleting}
        error={deleteError}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      >
        <p>Its log entry and transcript are deleted. Later runs no longer see its notes.</p>
      </DocConfirmDialog>
    </div>
  )
}
