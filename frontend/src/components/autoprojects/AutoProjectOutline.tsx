import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import './AutoProjectOutline.css'
import { prefersReducedMotion } from './autoprojectArrival'

/** The most updates listed under Updates: the first ones in the shown order. */
export const OUTLINE_MAX_UPDATES = 10
/** Scroller width (px) from which the outline shows beside the column. */
const OUTLINE_MIN_PX = 1000
/** Gap (px) left above a target scrolled to the top of the view. */
const TARGET_MARGIN_PX = 12
/** A target counts as reached once its top is within this (px) of the view's top. */
const CURRENT_SLACK_PX = 48

export interface AutoProjectOutlineItem {
  key: string
  label: string
  /** Shown before the label of an update: its number. */
  prefix?: string
  /** 0 for a card; 1 and deeper for what is inside it. */
  depth: number
  el: HTMLElement
}

/**
 * Whether the outline shows, from the scroller's width, which follows the
 * window, the sidebar and the detail panel: not on phones, in a small
 * window, or with the detail panel open beside a narrow column. Also keeps the scroller's height in
 * `--autoproject-scroller-height` for the sticky outline's own bound.
 * Without ResizeObserver (old browsers) there is no outline.
 */
export function useAutoProjectOutlineShown(scrollerRef: RefObject<HTMLElement | null>): boolean {
  const [shown, setShown] = useState(false)
  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller || typeof ResizeObserver === 'undefined') return
    const measure = () => {
      scroller.style.setProperty('--autoproject-scroller-height', `${scroller.clientHeight}px`)
      setShown(scroller.clientWidth >= OUTLINE_MIN_PX)
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(scroller)
    return () => observer.disconnect()
  }, [scrollerRef])
  return shown
}

function text(el: Element | null | undefined): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

/**
 * The outline of the page column as rendered: every card (its title; the
 * AutoProject files card as "Files"), the
 * rendered brief's h1-h3 under Brief (indented from the shallowest level
 * the brief uses; none while it is being edited), and the first
 * OUTLINE_MAX_UPDATES updates under Updates, in the order shown.
 */
export function scanAutoProjectOutline(column: HTMLElement): AutoProjectOutlineItem[] {
  const items: AutoProjectOutlineItem[] = []
  const cards = column.querySelectorAll<HTMLElement>(':scope > .autoproject-card')
  cards.forEach((card, cardIndex) => {
    const title = text(card.querySelector('.autoproject-card-title'))
    if (!title) return
    // "AutoProject files" is just "Files" in the outline's narrow column.
    const label = card.classList.contains('autoproject-files') ? 'Files' : title
    items.push({ key: `card:${cardIndex}`, label, depth: 0, el: card })

    if (card.classList.contains('autoproject-brief')) {
      const headings = Array.from(
        card.querySelectorAll<HTMLElement>(
          '.autoproject-brief-body :is(h1, h2, h3)',
        ),
      ).filter((h) => text(h) !== '')
      const levels = headings.map((h) => Number(h.tagName.slice(1)))
      const top = Math.min(...levels)
      headings.forEach((h, i) => {
        items.push({ key: `brief:${i}`, label: text(h), depth: 1 + levels[i] - top, el: h })
      })
    } else if (card.classList.contains('autoproject-updates')) {
      const updates = card.querySelectorAll<HTMLElement>('.autoproject-updates-list > .autoproject-run')
      Array.from(updates)
        .slice(0, OUTLINE_MAX_UPDATES)
        .forEach((update, i) => {
          const seq = text(update.querySelector('.autoproject-run-seq'))
          const number = /(\d+)\s*$/.exec(seq)?.[1]
          const prompt = text(update.querySelector('.autoproject-run-request-text'))
          items.push({
            key: `update:${update.dataset.runId ?? `pending-${i}`}`,
            label: prompt || seq || 'Update',
            prefix: prompt && number ? number : undefined,
            depth: 1,
            el: update,
          })
        })
    }
  })
  return items
}

function sameOutline(a: AutoProjectOutlineItem[], b: AutoProjectOutlineItem[]): boolean {
  return (
    a.length === b.length &&
    a.every((item, i) => {
      const other = b[i]
      return (
        item.key === other.key &&
        item.label === other.label &&
        item.prefix === other.prefix &&
        item.depth === other.depth &&
        item.el === other.el
      )
    })
  )
}

/**
 * The AutoProject page's outline (AutoProjectMainColumn renders it inside the
 * scroller, left of the column, while useAutoProjectOutlineShown has room for
 * it): a table of contents of the page (the cards, the brief's headings,
 * the first ten updates) whose entries scroll their target to the top of
 * the view, with the entry the reader is in marked.
 *
 * It is derived from the column's DOM rather than from props
 * (scanAutoProjectOutline), so it lists exactly what is on screen: a card
 * that is absent (Docs and tables behind its gates) has no entry, the brief's
 * headings are the rendered ones, and the updates follow the sort order.
 * A MutationObserver on the column re-scans, one scan per frame; state
 * changes only when the outline did.
 */
export function AutoProjectOutline({
  scrollerRef,
  columnRef,
}: {
  scrollerRef: RefObject<HTMLElement | null>
  columnRef: RefObject<HTMLElement | null>
}) {
  const [items, setItems] = useState<AutoProjectOutlineItem[]>([])
  const [currentKey, setCurrentKey] = useState<string | null>(null)
  const itemsRef = useRef(items)
  itemsRef.current = items

  // The entry the reader is in: the last one whose target reached the top.
  const syncCurrent = useCallback(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const line = scroller.getBoundingClientRect().top + CURRENT_SLACK_PX
    let current: string | null = null
    for (const item of itemsRef.current) {
      if (item.el.getBoundingClientRect().top <= line) current = item.key
    }
    setCurrentKey(current)
  }, [scrollerRef])

  useEffect(() => {
    const scroller = scrollerRef.current
    const column = columnRef.current
    if (!scroller || !column) return
    let frame: number | null = null
    let rescan = true
    const run = () => {
      frame = null
      if (rescan) {
        rescan = false
        const next = scanAutoProjectOutline(column)
        if (!sameOutline(itemsRef.current, next)) {
          itemsRef.current = next
          setItems(next)
        }
      }
      syncCurrent()
    }
    const schedule = () => {
      if (frame === null) frame = window.requestAnimationFrame(run)
    }
    const onMutation = () => {
      rescan = true
      schedule()
    }
    run()
    const observer = new MutationObserver(onMutation)
    observer.observe(column, { childList: true, subtree: true, characterData: true })
    scroller.addEventListener('scroll', schedule, { passive: true })
    return () => {
      observer.disconnect()
      scroller.removeEventListener('scroll', schedule)
      if (frame !== null) window.cancelAnimationFrame(frame)
    }
  }, [scrollerRef, columnRef, syncCurrent])

  const jumpTo = useCallback(
    (item: AutoProjectOutlineItem) => {
      const scroller = scrollerRef.current
      if (!scroller || !item.el.isConnected) return
      const top =
        item.el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
      scroller.scrollTo({
        top: Math.max(0, top - TARGET_MARGIN_PX),
        behavior: prefersReducedMotion() ? 'auto' : 'smooth',
      })
    },
    [scrollerRef],
  )

  if (items.length === 0) return <nav className="autoproject-outline" aria-label="On this page" />

  return (
    <nav className="autoproject-outline" aria-label="On this page">
      <ul className="autoproject-outline-list">
        {items.map((item) => (
          <li key={item.key}>
            <button
              type="button"
              className={`autoproject-outline-item${item.depth === 0 ? ' is-section' : ''}`}
              style={item.depth > 0 ? { paddingLeft: `${0.5 + item.depth * 0.75}rem` } : undefined}
              aria-current={item.key === currentKey ? 'location' : undefined}
              title={item.label}
              onClick={() => jumpTo(item)}
            >
              {item.prefix && <span className="autoproject-outline-prefix">{item.prefix}</span>}
              <span className="autoproject-outline-label">{item.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
}
