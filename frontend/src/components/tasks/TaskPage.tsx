import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import './TaskPage.css'
import { taskSource } from '../../api/fileApi'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useTask } from '../../hooks/useTask'
import { useTaskRuns } from '../../hooks/useTaskRuns'
import { useMarkTaskViewed } from '../../hooks/useTaskUnread'
import { FileBrowser, type FileBrowserListing } from '../FileBrowser'
import { TaskHeader, type TaskHeaderJumpTarget } from './TaskHeader'
import { TaskThread } from './TaskThread'
import { TaskPanel } from './TaskPanel'
import { TaskLoadError, TaskNotFound, TaskViewLoading } from './TaskViewState'
import { TaskNotice, readTaskNotice } from './TaskNotice'

/**
 * The task page (/tasks/<id>): the header over a two-column body, the
 * thread (TaskThread) on the left and the brief and instructions panel
 * (TaskPanel) on the right, in the place the RightPanel takes beside a chat
 * (spec 8.3). On phones (useIsMobile, 768px) the body is one column: the
 * panel (collapsible sections) directly below the header and the notice,
 * then the thread with its sticky composer. The panel comes first in the
 * DOM as well, so the reading order matches, and the composer (sticky
 * inside the thread column, which it cannot leave) can never cover panel
 * content. The two columns are keyed, so crossing the breakpoint moves
 * them instead of remounting them (drafts and thread state survive).
 *
 * Phones open scrolled to the newest run, so the panel starts out of view
 * above. The header is sticky there (TaskHeader.css) and carries a jump
 * button (TaskHeader `jump`): "Brief" while the panel's top is hidden
 * under the header scrolls it back below the header and focuses its first
 * disclosure row; otherwise "Latest", while the pane can still scroll down,
 * scrolls to the end of the thread; with everything on screen there is no
 * button. The scroll position decides (re-checked on scroll, resize and
 * content growth); the header's height is kept in
 * `--task-page-header-height` for scroll-margin-top (TaskPage.css), so
 * nothing that scrolls into view lands under the sticky header.
 *
 * This shell is the lasting page frame: it owns the one useTask and the one
 * useTaskRuns of the page and passes them down, and acknowledges an unread
 * task while it is on screen in a focused window. The header, the thread
 * and the panel own their bodies (TaskHeader, TaskThread, TaskPanel). The
 * page supplies the panel's Task files section: a FileBrowser card over the
 * task workspace (titled by the section, so `hideTitle`), whose own listing
 * of the root also gives the section's phone summary ("3 items"), so the
 * space is never listed twice. App.tsx keys it by task id, so switching
 * tasks starts from a fresh state.
 *
 * A notice carried in the router state (TaskPageLocationState, e.g. the
 * New Task page's "created, but the run did not start") shows above the
 * body until dismissed; dismissing replaces the history entry's state so a
 * reload does not bring it back.
 */
/** Rounding slack (px) for "the panel's top is hidden" and "the pane can scroll further". */
const PANEL_HIDDEN_SLACK_PX = 2

/** The nearest ancestor of `el` that scrolls vertically, or null for the document. */
function scrollParentOf(el: HTMLElement): HTMLElement | null {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const { overflowY } = getComputedStyle(node)
    if (overflowY === 'auto' || overflowY === 'scroll') return node
  }
  return null
}

function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}

export function TaskPage({ taskId }: { taskId: string }) {
  const view = useTask(taskId)
  const runs = useTaskRuns(taskId)
  const { task, loading, error, notFound, refresh } = view
  const isMobile = useIsMobile()

  useMarkTaskViewed(taskId, task !== null)

  const location = useLocation()
  const navigate = useNavigate()
  const notice = readTaskNotice(location.state)
  const dismissNotice = useCallback(() => {
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
  }, [navigate, location.pathname, location.search])

  const filesSource = useMemo(() => taskSource(taskId), [taskId])
  // Entries at the task workspace root, from the card's own listing; null
  // until it listed the root (it may restore a subfolder it was left in).
  const [rootEntryCount, setRootEntryCount] = useState<number | null>(null)
  const handleFilesListing = useCallback((listing: FileBrowserListing) => {
    if (listing.path === '/') setRootEntryCount(listing.count)
  }, [])

  // --- Phone: sticky header, panel visibility and the jump button ----------

  const pageRef = useRef<HTMLDivElement>(null)
  const headerRef = useRef<HTMLElement>(null)
  const panelRef = useRef<HTMLElement>(null)
  const [jumpTarget, setJumpTarget] = useState<TaskHeaderJumpTarget | null>(null)
  const pageShown = task !== null

  useEffect(() => {
    if (!isMobile || !pageShown) return
    const page = pageRef.current
    const header = headerRef.current
    const panel = panelRef.current
    if (!page || !header || !panel) return
    const parent = scrollParentOf(panel)
    const scroller = parent ?? (document.scrollingElement as HTMLElement | null) ?? document.documentElement
    let height = -1
    let frame: number | null = null
    // The header's height (for scroll-margin-top), then which jump makes
    // sense from here: "Brief" while the panel's top is hidden under the
    // header, else "Latest" while the pane can still scroll down, else none
    // (everything is on screen, e.g. a task with no runs).
    const sync = () => {
      frame = null
      const next = header.offsetHeight
      if (next !== height) {
        height = next
        page.style.setProperty('--task-page-header-height', `${height}px`)
      }
      const headerBottom = header.getBoundingClientRect().bottom
      const panelHidden = panel.getBoundingClientRect().top < headerBottom - PANEL_HIDDEN_SLACK_PX
      const moreBelow = scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - PANEL_HIDDEN_SLACK_PX
      setJumpTarget(panelHidden ? 'brief' : moreBelow ? 'latest' : null)
    }
    const schedule = () => {
      if (frame === null) frame = window.requestAnimationFrame(sync)
    }
    sync()
    const scrollTarget: HTMLElement | Window = parent ?? window
    scrollTarget.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    // Content growth (a run landing, a section expanding) changes "more below".
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    resize?.observe(header)
    resize?.observe(page)
    return () => {
      scrollTarget.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      resize?.disconnect()
      if (frame !== null) window.cancelAnimationFrame(frame)
      page.style.removeProperty('--task-page-header-height')
    }
  }, [isMobile, pageShown])

  const handleJump = useCallback((target: TaskHeaderJumpTarget) => {
    const panel = panelRef.current
    if (!panel) return
    const parent = scrollParentOf(panel)
    const scroller = parent ?? (document.scrollingElement as HTMLElement | null) ?? document.documentElement
    const behavior: ScrollBehavior = prefersReducedMotion() ? 'auto' : 'smooth'
    if (target === 'latest') {
      // The thread (and its sticky composer) ends the page.
      scroller.scrollTo({ top: scroller.scrollHeight, behavior })
      return
    }
    const viewTop = parent ? parent.getBoundingClientRect().top : 0
    const headerHeight = headerRef.current?.offsetHeight ?? 0
    const top = panel.getBoundingClientRect().top - viewTop + scroller.scrollTop - headerHeight
    scroller.scrollTo({ top: Math.max(0, top), behavior })
    const first =
      panel.querySelector<HTMLElement>('.task-panel-disclosure') ??
      panel.querySelector<HTMLElement>('button:not(:disabled), a[href], input, textarea, select')
    first?.focus({ preventScroll: true })
  }, [])

  if (loading) {
    return <TaskViewLoading text="Loading task..." />
  }

  if (notFound) {
    return <TaskNotFound />
  }

  if (!task) {
    return <TaskLoadError message={error?.message || 'Failed to load the task.'} onRetry={refresh} />
  }

  const thread = (
    <div key="thread" className="task-page-thread">
      <TaskThread taskId={taskId} task={task} runs={runs} onTaskDetail={view.applyDetail} />
    </div>
  )
  const panel = (
    <aside key="panel" ref={panelRef} className="task-page-panel" aria-label="Task brief and instructions">
      <TaskPanel
        task={view}
        filesSection={
          <div className="task-panel-files">
            <FileBrowser source={filesSource} hideTitle onListing={handleFilesListing} />
          </div>
        }
        filesSummary={taskFilesSummary(rootEntryCount)}
      />
    </aside>
  )

  return (
    <div className="task-page" ref={pageRef}>
      <TaskHeader
        task={task}
        onTaskDetail={view.applyDetail}
        headerRef={headerRef}
        jump={isMobile && jumpTarget ? { target: jumpTarget, onJump: handleJump } : undefined}
      />
      {notice && (
        <div className="task-page-notice">
          <TaskNotice message={notice} onDismiss={dismissNotice} />
        </div>
      )}
      <div className="task-page-body">{isMobile ? [panel, thread] : [thread, panel]}</div>
    </div>
  )
}

/** The collapsed Task files row's summary: what the workspace root holds. */
function taskFilesSummary(rootEntryCount: number | null): string {
  if (rootEntryCount === null) return 'Shared by every run'
  if (rootEntryCount === 0) return 'No files yet'
  return `${rootEntryCount} item${rootEntryCount === 1 ? '' : 's'}`
}
