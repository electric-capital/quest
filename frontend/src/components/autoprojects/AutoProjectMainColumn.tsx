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
import { answerAutoProjectQuestion, deleteAutoProjectRun, unarchiveAutoProject, updateAutoProject } from '../../api/autoprojectsApi'
import type { AutoProject, AutoProjectRun } from '../../api/types'
import { autoprojectDraftKey } from '../../constants/drafts'
import { useAppConfig } from '../../contexts/AppConfigContext'
import { useConversationSkills } from '../../contexts/ConversationSkillsContext'
import { useAutoProjectRunActivity } from '../../hooks/useAutoProjectRunActivity'
import type { AutoProjectRunsView } from '../../hooks/useAutoProjectRuns'
import { useAutoProjectUnread } from '../../hooks/useAutoProjectUnread'
import { persistentWebSocket } from '../../services/PersistentWebSocket'
import { stopAutoProjectRun, autoprojectRunStartErrorMessage, AutoProjectRunStartError } from '../../services/autoprojectRuns'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { UploadProgress, type UploadStatus } from '../UploadProgress'
import { AutoProjectComposer } from './AutoProjectComposer'
import { AutoProjectHeader, type AutoProjectHeaderJump, type AutoProjectHeaderJumpTarget } from './AutoProjectHeader'
import { AutoProjectNotice } from './AutoProjectNotice'
import { autoprojectRunCardKey, type AutoProjectRunActivityView, type AutoProjectRunCard } from './AutoProjectRunEntry'
import { AutoProjectUpdates, type PendingStart } from './AutoProjectUpdates'
import { readAutoProjectUpdatesOrder, writeAutoProjectUpdatesOrder, type AutoProjectUpdatesOrder } from './autoprojectUpdatesOrder'
import {
  initialUploadStatus,
  startAutoProjectRunFromComposer,
  type AutoProjectComposerSubmission,
} from './autoprojectComposerSend'
import {
  deriveAutoProjectThreadComposer,
  findActiveRun,
  isActiveRunStatus,
  oldestPendingQuestion,
  waitingOnPhrase,
} from './autoprojectThreadComposer'

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
function mergeCards(kept: AutoProjectRunCard[], open: AutoProjectRunCard[]): AutoProjectRunCard[] {
  const seen = new Set(kept.map(autoprojectRunCardKey))
  const added = open.filter((card) => !seen.has(autoprojectRunCardKey(card)))
  return added.length === 0 ? kept : [...kept, ...added]
}

/** `el`'s top in the scroller's content coordinates (independent of the scroll position). */
function contentTop(el: Element, scroller: HTMLElement): number {
  return el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
}

/** The updates in the section, in DOM (display) order. */
function updateElements(section: HTMLElement | null): HTMLElement[] {
  return section ? Array.from(section.querySelectorAll<HTMLElement>('.autoproject-updates-list > .autoproject-run')) : []
}

/** The newest update's element: the last one oldest first, the first one recent first. */
function newestElement(section: HTMLElement | null, order: AutoProjectUpdatesOrder): HTMLElement | null {
  const updates = updateElements(section)
  if (updates.length === 0) return null
  return order === 'oldest' ? updates[updates.length - 1] : updates[0]
}

/**
 * The AutoProject page's main column (AutoProjectPage), on every screen size: the
 * header, the page's notice, and one scroller holding the `lead` content
 * (the Brief and AutoProject files cards, from AutoProjectPage), the composer in the page
 * flow, and the Updates card (AutoProjectUpdates) under it. Nothing is pinned to
 * the bottom. The page opens at the TOP, on the brief.
 *
 * It owns the run side of the page: the composer's state
 * (deriveAutoProjectThreadComposer: start a run, answer the pending question, or
 * locked with Stop; an archived AutoProject gets an Unarchive notice instead), the
 * optimistic entry of a run being started, Stop, the delete-entry confirm,
 * the activity of the active run (useAutoProjectRunActivity, once, for that run
 * only) and the approval / connect cards kept for it.
 *
 * Cards: `open_requests` lists only OPEN cards, so a card resolved here
 * would vanish on the next refresh. The column keeps every card it has seen
 * for a run while that run is active (the card component flips itself to
 * its resolved state) and drops them once the run finished. Resolving a
 * card publishes `wait_handle_resolved`; when it names the active run's
 * conversation the runs are refreshed (autoproject_changed follows the status
 * change as well).
 *
 * The updates' sort order (`order`: oldest or most recent first, the
 * Updates card's control) is the viewer's one stored preference
 * (autoprojectUpdatesOrder.ts); the newest update is the last one oldest first and
 * the first one recent first, and everything below follows it.
 *
 * Scrolling (all in the one scroller):
 * - "Latest" / "Brief" in the header (AutoProjectHeader `jump`): "Latest" while
 *   the newest update is out of view (pointing down or up to it; recent
 *   first, it is above once the reader has scrolled into older updates),
 *   scrolling its top into view, with a working mark while a run is active
 *   and an unread mark while the AutoProject is unread; otherwise "Brief" once
 *   scrolled down, scrolling back to the top; no button with both in view.
 * - `onNewestVisibleChange` reports whether the newest update reaches into
 *   the view, for the page's mark-as-read rule.
 * - Sending a prompt or an answer shows the newest update. Oldest first
 *   that is the end, and the column then follows growth at the bottom (the
 *   run's activity, its result) for as long as the user stays there; recent
 *   first it is the top of the Updates card, right under the composer, so
 *   the view moves only to keep the composer and the update together.
 *   Opening at the top never pins it.
 * - While a question waits, the composer's "Answering the question" label
 *   quotes it and offers "Show question", which scrolls its card into view
 *   (oldest first it can be far below the composer).
 * - "Show earlier updates" sits at the old end. Oldest first it keeps the
 *   previously first update where it was on screen once the older page is
 *   prepended (measured before and after in a layout effect; the browser's
 *   own scroll anchoring is off on the scroller, CSS, so the two never add
 *   up); recent first the page lands below everything already shown.
 * - `openRunConversationId` (the run in the detail panel, e.g. from a deep
 *   link) is scrolled into view when it is off screen or only peeks in at
 *   an edge, loading older pages until it is found or the pages end.
 *
 * The composer's model selector persists per AutoProject, as a conversation's
 * selector persists per conversation: it is seeded with the AutoProject's default
 * (`autoprojects.model` / `thinking_effort`), and a user's pick is PATCHed back as
 * the default for later runs (skipped when it equals the current one; the
 * returned AutoProject goes to `onAutoProjectDetail`). A refused PATCH (e.g. 400
 * `model_not_allowed`) is only logged: the run still uses the composer's
 * pick, and the send path reports a model it refuses.
 */
export function AutoProjectMainColumn({
  autoprojectId,
  autoproject,
  runs,
  onAutoProjectDetail,
  notice: pageNotice,
  lead,
  openRunConversationId,
  onViewRun,
  onOpenFile,
  onNewestVisibleChange,
}: {
  autoprojectId: string
  autoproject: AutoProject
  runs: AutoProjectRunsView
  /** Merge an AutoProject returned by unarchive, the default-model PATCH or a header action into the page's view. */
  onAutoProjectDetail: (autoproject: AutoProject) => void
  /** The page's router-state notice, shown under the header. */
  notice?: ReactNode
  /** The content above the updates: the brief and the AutoProject files. */
  lead: ReactNode
  /** The run whose transcript the detail panel shows, or null. */
  openRunConversationId: string | null
  /** "View details" / "Close details" on an update. */
  onViewRun: (conversationId: string) => void
  onOpenFile: (workspacePath: string, filename: string, source: FileSource) => void
  onNewestVisibleChange: (visible: boolean) => void
}) {
  const draftKey = autoprojectDraftKey(autoprojectId)
  const { setQueuedSkillsForConversation } = useConversationSkills()
  const { appName } = useAppConfig()

  const [pendingStart, setPendingStart] = useState<PendingStart | null>(null)
  const pendingKeyRef = useRef(0)
  const submittingRef = useRef(false)
  const [answeringHandleId, setAnsweringHandleId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [uploadStatus, setUploadStatus] = useState<UploadStatus | null>(null)
  const [stopping, setStopping] = useState<string | null>(null)
  const [stopError, setStopError] = useState<string | null>(null)
  const [keptCards, setKeptCards] = useState<Record<string, AutoProjectRunCard[]>>({})
  const [deleteTarget, setDeleteTarget] = useState<AutoProjectRun | null>(null)
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

  // --- Live arrivals (autoprojectArrival.ts) ---------------------------------
  // Only what this page saw happen plays an arrival: a run it saw active
  // (running or waiting) that is now finished gets `result:<id>`; a new
  // active run that shows up after the first load and is not the one this
  // page is starting gets `row:<id>`; a send gets `pending:<key>` (in
  // startRun). Nothing on the first load, for older pages (finished runs
  // never seen active), for runs that finished unseen (a refetch after a
  // reconnect), or across AutoProjects (the page is keyed by id). Statuses
  // are folded in during render so the entries' layout effects of the same
  // commit can claim them; each key is claimed once.
  const seenStatusRef = useRef(new Map<string, AutoProjectRun['status']>())
  const arrivalsRef = useRef(new Set<string>())
  const firstLoadSeenRef = useRef(false)
  if (!runs.loading) {
    const seen = seenStatusRef.current
    for (const run of runList) {
      const prev = seen.get(run.id)
      if (prev === undefined) {
        if (
          firstLoadSeenRef.current &&
          isActiveRunStatus(run.status) &&
          pendingStart === null
        ) {
          arrivalsRef.current.add(`row:${run.id}`)
        }
      } else if (isActiveRunStatus(prev) && !isActiveRunStatus(run.status)) {
        arrivalsRef.current.add(`result:${run.id}`)
      }
      seen.set(run.id, run.status)
    }
    firstLoadSeenRef.current = true
  }
  const claimArrival = useCallback((key: string) => arrivalsRef.current.delete(key), [])

  const composer = deriveAutoProjectThreadComposer({
    archived: autoproject.archived,
    runs: runList,
    autoprojectActiveRun: autoproject.active_run,
    starting: pendingVisible,
    answering: answeringHandleId !== null,
    appName,
  })
  const composerRef = useRef(composer)
  composerRef.current = composer

  const activeRun = findActiveRun(runList)
  const activeConversationId = activeRun?.conversation_id ?? null
  const activity = useAutoProjectRunActivity(activeConversationId)
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
      const next: Record<string, AutoProjectRunCard[]> = {}
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
  const composerBlockRef = useRef<HTMLDivElement>(null)
  // Following the end: only after the user scrolled there or sent something.
  const pinnedRef = useRef(false)
  // The first update before an older page is prepended, to keep it in place.
  const anchorRef = useRef<{ runId: string; top: number } | null>(null)
  // A run to scroll into view once its update is loaded (the panel's run).
  const revealRef = useRef<string | null>(openRunConversationId)
  const [scrollEndSeq, setScrollEndSeq] = useState(0)
  const [order, setOrder] = useState<AutoProjectUpdatesOrder>(readAutoProjectUpdatesOrder)
  const orderRef = useRef(order)
  orderRef.current = order
  const handleOrderChange = useCallback((next: AutoProjectUpdatesOrder) => {
    setOrder(next)
    writeAutoProjectUpdatesOrder(next)
    // Following the end only means something oldest first.
    if (next !== 'oldest') pinnedRef.current = false
  }, [])
  const [jumpTarget, setJumpTarget] = useState<AutoProjectHeaderJumpTarget | null>(null)
  const [jumpUp, setJumpUp] = useState(false)
  const newestVisibleRef = useRef<boolean | null>(null)
  const onNewestVisibleRef = useRef(onNewestVisibleChange)
  onNewestVisibleRef.current = onNewestVisibleChange

  const scrollToEnd = useCallback(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    scroller.scrollTop = scroller.scrollHeight
    pinnedRef.current = true
  }, [])

  /**
   * After a send: oldest first, to the end (and follow it). Recent first the
   * new update sits right under the composer: keep both in view, moving
   * only when the update's top would be under the bottom edge (then the
   * composer goes to the top of the view).
   */
  const scrollToNewest = useCallback(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    if (orderRef.current === 'oldest') {
      scrollToEnd()
      return
    }
    pinnedRef.current = false
    const newest = newestElement(updatesRef.current, 'recent')
    const block = composerBlockRef.current
    if (!newest || !block) return
    const view = scroller.getBoundingClientRect()
    if (newest.getBoundingClientRect().top < view.bottom - REVEAL_MIN_VISIBLE_PX) return
    scroller.scrollTop = Math.max(0, contentTop(block, scroller) - REVEAL_MARGIN_PX)
  }, [scrollToEnd])

  /** "Show question": the pending question's card on the active update. */
  const showQuestion = useCallback(() => {
    const id = composerRef.current.activeRun?.id
    const article = id
      ? updatesRef.current?.querySelector<HTMLElement>(`.autoproject-run[data-run-id="${CSS.escape(id)}"]`)
      : null
    const card = article?.querySelector<HTMLElement>('section[aria-label="Question"]') ?? article
    card?.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
  }, [])

  // Where the newest update is relative to the view, and what follows from it.
  const sync = useCallback(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const view = scroller.getBoundingClientRect()
    const newestEl = newestElement(updatesRef.current, orderRef.current)
    const newest = newestEl ? newestEl.getBoundingClientRect() : null
    const newestVisible =
      newest !== null && newest.top < view.bottom - VISIBLE_SLACK_PX && newest.bottom > view.top + VISIBLE_SLACK_PX
    const newestBelow = newest !== null && newest.top >= view.bottom - VISIBLE_SLACK_PX
    const newestAbove = newest !== null && newest.bottom <= view.top + VISIBLE_SLACK_PX
    setJumpTarget(
      newestBelow || newestAbove ? 'latest' : scroller.scrollTop > BRIEF_JUMP_AFTER_PX ? 'brief' : null,
    )
    setJumpUp(newestAbove)
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
      pinnedRef.current = orderRef.current === 'oldest' && nearBottom && scroller.scrollTop > 0
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
    if (scrollEndSeq > 0) scrollToNewest()
  }, [scrollEndSeq, scrollToNewest])

  // A new order moves the newest update: re-check the jump and read state.
  useLayoutEffect(() => {
    sync()
  }, [order, sync])

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
    // Recent first the page lands below the shown updates: nothing moves.
    const first = orderRef.current === 'oldest' ? updateElements(updatesRef.current)[0] : undefined
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

  const handleJump = useCallback((target: AutoProjectHeaderJumpTarget) => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const behavior: ScrollBehavior = prefersReducedMotion() ? 'auto' : 'smooth'
    if (target === 'brief') {
      scroller.scrollTo({ top: 0, behavior })
      return
    }
    const newest = newestElement(updatesRef.current, orderRef.current)
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
    async (submission: AutoProjectComposerSubmission) => {
      if (submittingRef.current) return
      if (!submission.text) {
        setNotice('Write a prompt first.')
        return
      }
      submittingRef.current = true
      const key = ++pendingKeyRef.current
      arrivalsRef.current.add(`pending:${key}`)
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
        const started = await startAutoProjectRunFromComposer(autoprojectId, submission, {
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
        setNotice(autoprojectRunStartErrorMessage(err))
        restoreSkills(submission.skillIds)
        // Someone else's start won the race: show that run.
        if (err instanceof AutoProjectRunStartError && err.reason === 'autoproject_run_active') void refresh()
      } finally {
        submittingRef.current = false
        setUploadStatus(null)
      }
    },
    [autoprojectId, refresh, restoreSkills],
  )

  const answer = useCallback(
    async (submission: AutoProjectComposerSubmission, handleId: string) => {
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
        await answerAutoProjectQuestion(autoprojectId, handleId, submission.text)
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
    [autoprojectId, refresh, restoreSkills],
  )

  const handleSubmit = useCallback(
    (submission: AutoProjectComposerSubmission) => {
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
    if (!stopAutoProjectRun(conversationId)) {
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
      const updated = await unarchiveAutoProject(autoprojectId)
      onAutoProjectDetail(updated)
    } catch (err) {
      setUnarchiveError(errorText(err, 'The AutoProject could not be unarchived.'))
    } finally {
      setUnarchiving(false)
    }
  }, [autoprojectId, onAutoProjectDetail])

  // The AutoProject's default model follows the composer's picks. The ref holds the
  // latest default so a pick compares against it, not a stale closure; the
  // seq drops a response overtaken by a later pick.
  const autoprojectModelRef = useRef({ model: autoproject.model, thinkingEffort: autoproject.thinking_effort })
  autoprojectModelRef.current = { model: autoproject.model, thinkingEffort: autoproject.thinking_effort }
  const modelPatchSeqRef = useRef(0)
  const handleModelChange = useCallback(
    (model: string, thinkingEffort: string | null) => {
      const current = autoprojectModelRef.current
      if (current.model === model && (current.thinkingEffort ?? null) === thinkingEffort) return
      const seq = ++modelPatchSeqRef.current
      updateAutoProject(autoprojectId, { model, thinking_effort: thinkingEffort }).then(
        (updated) => {
          if (seq === modelPatchSeqRef.current) onAutoProjectDetail(updated)
        },
        (err: unknown) => console.warn('Could not save the AutoProject default model', err),
      )
    },
    [autoprojectId, onAutoProjectDetail],
  )

  const openDelete = useCallback((run: AutoProjectRun) => {
    setDeleteError(null)
    setDeleteTarget(run)
  }, [])

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return
    const run = deleteTarget
    setDeleting(true)
    setDeleteError(null)
    try {
      await deleteAutoProjectRun(autoprojectId, run.id)
      removeRun(run.id)
      setDeleteTarget(null)
    } catch (err) {
      const code = errorCode(err)
      if (code === 'autoproject_run_not_found') {
        // Already gone (another tab): drop it here too.
        removeRun(run.id)
        setDeleteTarget(null)
      } else if (code === 'autoproject_run_active') {
        setDeleteError('This update is still in progress. Stop it before deleting it.')
      } else {
        setDeleteError(errorText(err, 'The update could not be deleted.'))
      }
    } finally {
      setDeleting(false)
    }
  }, [deleteTarget, autoprojectId, removeRun])

  const handleQuestionAnswered = useCallback(() => {
    void refresh()
  }, [refresh])

  // --- Render -----------------------------------------------------------------

  const activeActivity = useMemo((): AutoProjectRunActivityView | null => {
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

  const unread = useAutoProjectUnread(autoproject)
  const working = autoproject.active_run !== null || pendingVisible
  const jump: AutoProjectHeaderJump | undefined = jumpTarget
    ? {
        target: jumpTarget,
        onJump: handleJump,
        mark: jumpTarget !== 'latest' ? null : working ? 'working' : unread ? 'unread' : null,
        up: jumpTarget === 'brief' || jumpUp,
      }
    : undefined

  return (
    <div className="autoproject-page-main">
      <AutoProjectHeader autoproject={autoproject} onAutoProjectDetail={onAutoProjectDetail} jump={jump} />
      {pageNotice}
      <div className="autoproject-page-scroller" ref={scrollerRef}>
        <div className="autoproject-page-column" ref={columnRef}>
          {lead}
          <div className="autoproject-composer-block" ref={composerBlockRef}>
            {composer.mode === 'archived' ? (
              <div className="autoproject-composer-archived">
                <span className="autoproject-composer-archived-text">This AutoProject is archived.</span>
                <button
                  type="button"
                  className="autoproject-ctl-button"
                  onClick={() => void handleUnarchive()}
                  disabled={unarchiving}
                >
                  {unarchiving ? 'Unarchiving...' : 'Unarchive'}
                </button>
                {unarchiveError && (
                  <div className="autoproject-composer-archived-error">
                    <AutoProjectNotice message={unarchiveError} onDismiss={() => setUnarchiveError(null)} />
                  </div>
                )}
              </div>
            ) : (
              <>
                {composer.label && (
                  <div className="autoproject-composer-label">
                    <span className="autoproject-composer-label-main">{composer.label}</span>
                    {composer.question && (
                      <span className="autoproject-composer-label-question" title={composer.question.question}>
                        {composer.question.question}
                      </span>
                    )}
                    {composer.question && (
                      <button type="button" className="autoproject-composer-label-show" onClick={showQuestion}>
                        Show question
                      </button>
                    )}
                  </div>
                )}
                {notice && (
                  <div className="autoproject-composer-notice">
                    <AutoProjectNotice message={notice} onDismiss={() => setNotice(null)} />
                  </div>
                )}
                {uploadStatus && (
                  <div className="autoproject-composer-upload">
                    <UploadProgress status={uploadStatus} />
                  </div>
                )}
                <AutoProjectComposer
                  draftKey={draftKey}
                  onSubmit={handleSubmit}
                  busy={composer.busy}
                  disabled={composer.disabled}
                  onStop={composer.disabled && stopConversationId ? handleComposerStop : undefined}
                  placeholder={composer.placeholder}
                  initialModel={autoproject.model}
                  initialThinkingEffort={autoproject.thinking_effort}
                  onModelChange={handleModelChange}
                />
              </>
            )}
          </div>
          <AutoProjectUpdates
            ref={updatesRef}
            autoprojectId={autoprojectId}
            archived={autoproject.archived}
            runs={runs}
            pendingStart={pendingVisible ? pendingStart : null}
            keptCards={keptCards}
            activeRunId={activeRun?.id ?? null}
            activity={activeActivity}
            answeringHandleId={answeringHandleId}
            openRunConversationId={openRunConversationId}
            count={autoproject.run_count}
            order={order}
            onOrderChange={handleOrderChange}
            onLoadOlder={handleLoadOlder}
            onStop={handleStop}
            onQuestionAnswered={handleQuestionAnswered}
            onOpenFile={onOpenFile}
            onDelete={openDelete}
            onView={onViewRun}
            claimArrival={claimArrival}
          />
        </div>
      </div>

      <DocConfirmDialog
        isOpen={deleteTarget !== null}
        title={`Delete update ${deleteTarget?.seq ?? ''}?`}
        confirmLabel="Delete update"
        busyLabel="Deleting..."
        tone="danger"
        busy={deleting}
        error={deleteError}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      >
        <p>The update and the work behind it are deleted. Later updates no longer see its notes.</p>
      </DocConfirmDialog>
    </div>
  )
}
