/**
 * useFlyoutGrace -- the "safe triangle" that keeps a menu flyout open while
 * the pointer travels from its row to it.
 *
 * The path from a row to the flyout beside it is rarely straight or
 * uninterrupted: it crosses neighbouring rows (whose hover would switch or
 * close the flyout) and strips that belong to neither element (a gap, the
 * list's own scrollbar). When the pointer leaves the owning row toward the
 * flyout, `begin` records the triangle between the exit point and the
 * flyout's near edge (plus the flyout itself). While the pointer stays in
 * that area, `isHeadingToFlyout` tells the other rows to ignore their hover.
 *
 * The grace ends without effect when the pointer reaches the flyout (the
 * owner calls `end` from its mouseenter). It ends with `onLeave(element
 * under the pointer)` -- so the caller can apply the hover the grace held
 * back -- when the pointer moves out of the area, or rests for GRACE_MS
 * anywhere before reaching the flyout.
 *
 * Flyouts open to the right of their rows (the only layout the model menu
 * uses).
 */

import { useCallback, useEffect, useMemo, useRef } from 'react';

interface Point {
  x: number;
  y: number;
}

interface Grace {
  area: Point[];
  timer: number;
  /** Last element the pointer was over, handed to onLeave. */
  under: Element | null;
  onLeave: (under: Element | null) => void;
  onMove: (event: MouseEvent) => void;
}

/** How long the pointer may rest before the grace gives up on it. */
export const GRACE_MS = 300;
/** The apex sits this far behind the exit point so the first moves after
 *  leaving the row land inside the triangle, not on its tip. */
const APEX_BLEED = 5;

/** Ray casting: whether `p` is inside the polygon `poly`. */
export function isPointInPolygon(p: Point, poly: Point[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i];
    const b = poly[j];
    if ((a.y > p.y) !== (b.y > p.y) && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) {
      inside = !inside;
    }
  }
  return inside;
}

type PointerEventLike = Pick<MouseEvent, 'clientX' | 'clientY'> & { relatedTarget: EventTarget | null };

export function useFlyoutGrace() {
  const graceRef = useRef<Grace | null>(null);

  const finish = useCallback((notify: boolean) => {
    const grace = graceRef.current;
    if (!grace) return;
    graceRef.current = null;
    window.clearTimeout(grace.timer);
    document.removeEventListener('mousemove', grace.onMove, true);
    if (notify) grace.onLeave(grace.under);
  }, []);

  /**
   * From the owning row's mouseleave, while its flyout is open. Starts the
   * grace and returns true when the pointer left toward the flyout (the
   * caller keeps it open); returns false otherwise (the caller handles the
   * leave as a plain one).
   */
  const begin = useCallback((
    event: PointerEventLike,
    flyout: Element | null,
    onLeave: (under: Element | null) => void,
  ): boolean => {
    finish(false);
    if (!flyout) return false;
    const rect = flyout.getBoundingClientRect();
    // Leaving from the flyout itself, or past its near edge, is not a trip
    // toward it.
    if (event.clientX >= rect.left) return false;
    const area: Point[] = [
      { x: event.clientX - APEX_BLEED, y: event.clientY },
      { x: rect.left, y: rect.top },
      { x: rect.right, y: rect.top },
      { x: rect.right, y: rect.bottom },
      { x: rect.left, y: rect.bottom },
    ];
    const grace: Grace = {
      area,
      timer: 0,
      under: event.relatedTarget instanceof Element ? event.relatedTarget : null,
      onLeave,
      // Capture phase on the document: sees every move, including over the
      // gap and the scrollbar, which no row receives.
      onMove: (e) => {
        grace.under = e.target instanceof Element ? e.target : null;
        if (!isPointInPolygon({ x: e.clientX, y: e.clientY }, area)) {
          finish(true);
          return;
        }
        window.clearTimeout(grace.timer);
        grace.timer = window.setTimeout(() => finish(true), GRACE_MS);
      },
    };
    grace.timer = window.setTimeout(() => finish(true), GRACE_MS);
    document.addEventListener('mousemove', grace.onMove, true);
    graceRef.current = grace;
    return true;
  }, [finish]);

  /** For the other rows' mouseenter: true while the pointer is still on its
   *  way to the flyout, so the row leaves the flyout alone. */
  const isHeadingToFlyout = useCallback((event: Pick<MouseEvent, 'clientX' | 'clientY'>): boolean => {
    const grace = graceRef.current;
    return grace !== null && isPointInPolygon({ x: event.clientX, y: event.clientY }, grace.area);
  }, []);

  /** Drop the grace without applying anything (pointer reached the flyout,
   *  menu closed, list scrolled). */
  const end = useCallback(() => finish(false), [finish]);

  useEffect(() => end, [end]);

  // Stable across renders, so callers can list it as a dependency.
  return useMemo(() => ({ begin, isHeadingToFlyout, end }), [begin, isHeadingToFlyout, end]);
}
