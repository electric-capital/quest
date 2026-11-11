import { useEffect, useId, useRef, type ReactNode } from 'react'
import { ChevronLeft, X } from 'lucide-react'
import './TaskDetailPanel.css'
import { useIsMobile } from '../../hooks/useIsMobile'

/**
 * True while some other dialog or menu owns the keyboard: a modal (a
 * confirm, the file viewer), or an open menu (the task header's, a run's
 * row menu, a file row's). Its own Escape handling comes first; the panel
 * must not close underneath it.
 */
function escapeOwnedElsewhere(event: KeyboardEvent): boolean {
  const target = event.target instanceof Element ? event.target : null
  if (target?.closest('[role="dialog"], [role="alertdialog"], [aria-modal="true"], [role="menu"]')) return true
  return document.querySelector('[role="dialog"], [role="alertdialog"], [aria-modal="true"], [role="menu"]') !== null
}

export interface TaskDetailPanelProps {
  /** The panel's heading ("Run 3", "Brief history", "Instructions"); also its accessible name. */
  title: string
  /** A line beside or under the heading (a run's status and time). */
  meta?: ReactNode
  /** Controls under the heading row (the run panel's Transcript / Chat Files tabs). */
  toolbar?: ReactNode
  /**
   * Close the panel: the close (desktop) or back (phone) button, and
   * Escape. The page decides whether it closes at once or first asks to
   * discard an unsaved draft inside (TaskPage).
   */
  onClose: () => void
  /**
   * `scroll`: the body scrolls as a whole (History, Instructions); `fill`:
   * the body is a flex column its child fills, scrolling itself (a run's
   * transcript, which pins its own composer).
   */
  body?: 'scroll' | 'fill'
  children: ReactNode
}

/**
 * The task page's detail panel (TaskPage): one of a run's transcript, the
 * brief History or the instructions, beside the main column. Closed by
 * default; the page renders it only while a detail view is open.
 *
 * Desktop: a column on the right of the main column (TaskDetailPanel.css,
 * about 44% of the page), with the title, the optional meta line and a
 * close button on its header row. Phones (useIsMobile): a full-screen view
 * over the task page with a "Back to task" button in place of the close
 * button.
 *
 * Escape closes it (through `onClose`) unless a modal or a menu is open,
 * or something already handled the key (an editor's own Escape, which asks
 * to discard a changed draft, calls preventDefault). The listener sits on
 * window in the bubble phase, so every document-level handler (menus,
 * ModalShell) runs before it; whether a modal or menu was open is taken
 * when the event starts, since its handler may already have closed it. On open the heading takes focus; on close
 * focus goes back to the control that opened the panel when it is still
 * on the page.
 */
export function TaskDetailPanel({ title, meta, toolbar, onClose, body = 'scroll', children }: TaskDetailPanelProps) {
  const isMobile = useIsMobile()
  const headingId = useId()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    // Whether a modal or menu owned the key is read when the event STARTS
    // (capture on window runs first): the modal's own handler may close it
    // and React commit that before the bubble listener below runs.
    let ownedElsewhere = false
    const onKeyCapture = (event: KeyboardEvent) => {
      if (event.key === 'Escape') ownedElsewhere = escapeOwnedElsewhere(event)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || ownedElsewhere) return
      event.preventDefault()
      onCloseRef.current()
    }
    window.addEventListener('keydown', onKeyCapture, true)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKeyCapture, true)
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  // Focus in on open, back to the opener on close.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    headingRef.current?.focus({ preventScroll: true })
    const panel = headingRef.current?.closest('.task-detail-panel') ?? null
    return () => {
      const active = document.activeElement
      const focusLeft = !active || active === document.body || (panel !== null && panel.contains(active))
      if (focusLeft && opener?.isConnected) opener.focus({ preventScroll: true })
    }
  }, [])

  return (
    <aside className={`task-detail-panel${isMobile ? ' is-phone' : ''}`} aria-labelledby={headingId}>
      <header className="task-detail-panel-header">
        {isMobile && (
          <button type="button" className="task-detail-panel-back" onClick={onClose}>
            <ChevronLeft size={18} aria-hidden="true" />
            Back to task
          </button>
        )}
        <div className="task-detail-panel-titles">
          <h2 id={headingId} ref={headingRef} tabIndex={-1} className="task-detail-panel-title">
            {title}
          </h2>
          {meta}
        </div>
        {!isMobile && (
          <button
            type="button"
            className="task-detail-panel-close"
            onClick={onClose}
            aria-label={`Close ${title.toLowerCase()}`}
            title="Close (Esc)"
          >
            <X size={18} aria-hidden="true" />
          </button>
        )}
      </header>
      {toolbar}
      <div className={`task-detail-panel-body task-detail-panel-body--${body}`}>{children}</div>
    </aside>
  )
}
