/**
 * Arrival animation of the AutoProject page's updates: what the reader saw
 * happen live opens in instead of popping, so the eye can follow it
 * (AutoProjectRunEntry, AutoProjectUpdates). Plain Web Animations on the
 * element, no library.
 *
 * - Height opens from `fromHeight` (0 for a new row; the activity line's
 *   height when a result replaces it, so nothing jumps) to the element's
 *   natural height, with the content rising and fading in. The duration
 *   follows the distance, between ARRIVAL_MIN_MS and ARRIVAL_MAX_MS, on the
 *   standard decelerate curve; content below slides down with it. Past
 *   ARRIVAL_MAX_HEIGHT_PX (a very long result) the height is not animated,
 *   only the fade, so it never crawls.
 * - `highlight` then tints the element's ground (the accent's soft tint)
 *   and lets it fade out over about 1.6 s (`.autoproject-arrived`,
 *   AutoProjectUpdates.css), so the new result is easy to find.
 * - Under `prefers-reduced-motion: reduce` nothing moves: only the
 *   highlight's colour fade plays.
 *
 * Which arrivals play is decided by the page (AutoProjectMainColumn): only
 * transitions it observed live, each claimed once, so a remount (sort
 * switch) or a refetch never replays one.
 */

export const ARRIVAL_MIN_MS = 260
export const ARRIVAL_MAX_MS = 400
export const ARRIVAL_MAX_HEIGHT_PX = 720
const ARRIVAL_EASING = 'cubic-bezier(0.2, 0, 0, 1)'
const HIGHLIGHT_CLASS = 'autoproject-arrived'

/** The viewer asked for reduced motion. */
export function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}

/** Duration (ms) for a height change of `distance` px. */
export function arrivalDuration(distance: number): number {
  return Math.round(Math.min(ARRIVAL_MAX_MS, Math.max(ARRIVAL_MIN_MS, ARRIVAL_MIN_MS + Math.abs(distance) * 0.3)))
}

function addHighlight(el: HTMLElement): void {
  el.classList.remove(HIGHLIGHT_CLASS)
  // Restart the keyframes if a highlight was still running.
  void el.offsetWidth
  el.classList.add(HIGHLIGHT_CLASS)
  el.addEventListener('animationend', () => el.classList.remove(HIGHLIGHT_CLASS), { once: true })
}

export function playArrival(
  el: HTMLElement,
  { fromHeight = 0, highlight = false }: { fromHeight?: number; highlight?: boolean } = {},
): void {
  if (prefersReducedMotion() || typeof el.animate !== 'function') {
    if (highlight) addHighlight(el)
    return
  }
  const height = el.getBoundingClientRect().height
  const distance = height - fromHeight
  const duration = arrivalDuration(distance)
  if (Math.abs(distance) > 1 && height <= ARRIVAL_MAX_HEIGHT_PX) {
    const overflow = el.style.overflow
    el.style.overflow = 'hidden'
    const grow = el.animate(
      [{ height: `${Math.max(0, fromHeight)}px` }, { height: `${height}px` }],
      { duration, easing: ARRIVAL_EASING },
    )
    const restore = () => {
      el.style.overflow = overflow
    }
    grow.addEventListener('finish', restore)
    grow.addEventListener('cancel', restore)
  }
  el.animate(
    [
      { opacity: 0, transform: 'translateY(6px)' },
      { opacity: 1, transform: 'none' },
    ],
    { duration: duration + 60, easing: ARRIVAL_EASING },
  )
  if (highlight) addHighlight(el)
}
