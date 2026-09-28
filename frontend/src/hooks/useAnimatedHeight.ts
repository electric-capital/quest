/**
 * Glide a container's height when its content changes size.
 *
 * Built for dialogs whose height follows their content (a settings modal
 * switching sections, a panel whose data arrives later): instead of the
 * box snapping to the new content height, it animates from the height it
 * had to the new natural height. Attach `containerRef` to the box whose
 * height should glide and the returned callback ref to an element inside
 * it whose own size IS the content's natural size (an inner wrapper of the
 * scrollable area, not the scrollable area itself, whose box is
 * container-driven). The content ref is a callback so the observer follows
 * the element through mount/unmount -- a modal that renders null while
 * closed gets a fresh element on every open. The container must keep its
 * layout sane while shorter than its content (e.g. a flex column with a
 * `min-height: 0; overflow: hidden` body).
 *
 * `revision` is any value that changes when the caller itself swapped the
 * content (the active section, a tab): those changes are synced in a
 * layout effect, before the browser paints the new content, so the glide
 * starts from the very first frame. Content changes the caller does not
 * drive (data arriving in a child, a textarea growing) are picked up by a
 * ResizeObserver instead.
 *
 * `hold` freezes the container at its current height (content changes
 * are ignored) and, when released, animates to the then-natural height.
 * Use it while a section shows a small loading placeholder so the box
 * doesn't shrink around the placeholder and grow again once the data
 * lands.
 *
 * Uses the Web Animations API (element.animate), so no inline height
 * persists once an animation finishes (the box follows its CSS again,
 * incl. max-height); an animation interrupted by a further content change
 * retargets from its in-flight height, and one interrupted by a size
 * notification that changed nothing (a scrollbar toggling mid-glide)
 * resumes where it was. Respects `prefers-reduced-motion`.
 */

import { useCallback, useLayoutEffect, useRef, type RefObject } from 'react';

const ANIMATION_MS = 220;
const EASING = 'cubic-bezier(0.4, 0, 0.2, 1)';

export function useAnimatedHeight(
  containerRef: RefObject<HTMLElement | null>,
  { hold = false, revision }: { hold?: boolean; revision?: unknown } = {},
): (el: HTMLElement | null) => void {
  // Mirrors `hold` for the ResizeObserver callback (kept current by the
  // layout effect below, which runs before any observer notification).
  const holdRef = useRef(hold);
  const animationRef = useRef<Animation | null>(null);
  // Keyframe endpoints of the in-flight animation (null when idle).
  const fromRef = useRef<number | null>(null);
  const targetRef = useRef<number | null>(null);
  // Height the box last settled at (or is gliding towards). Null until the
  // first measurement: the initial paint never animates.
  const lastHeightRef = useRef<number | null>(null);

  const sync = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;

    const running = animationRef.current;
    const held = el.style.height !== '';
    // Where the glide starts: the box's visual height right now. Mid-flight
    // or held that is what the box measures; idle, the DOM has already
    // re-laid out to the new content by the time we run, so the previous
    // settled height comes from the last sync.
    const current = running || held
      ? el.offsetHeight
      : (lastHeightRef.current ?? el.offsetHeight);
    const resumeAt = running ? (running.currentTime as number | null) : null;
    if (running) {
      running.cancel();
      animationRef.current = null;
    }

    if (holdRef.current) {
      el.style.height = `${current}px`;
      fromRef.current = null;
      targetRef.current = null;
      lastHeightRef.current = current;
      return;
    }

    // Natural height: neither a hold nor an animation applied.
    el.style.height = '';
    const natural = el.offsetHeight;
    const first = lastHeightRef.current === null;
    lastHeightRef.current = natural;

    const play = (from: number, to: number, startAt: number | null) => {
      const anim = el.animate(
        [{ height: `${from}px` }, { height: `${to}px` }],
        { duration: ANIMATION_MS, easing: EASING },
      );
      if (startAt !== null) anim.currentTime = startAt;
      fromRef.current = from;
      targetRef.current = to;
      animationRef.current = anim;
      const clear = () => {
        if (animationRef.current !== anim) return;
        animationRef.current = null;
        fromRef.current = null;
        targetRef.current = null;
      };
      anim.onfinish = clear;
      anim.oncancel = clear;
    };

    if (first) return;
    if (typeof el.animate !== 'function') return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    if (running && resumeAt !== null && fromRef.current !== null && targetRef.current === natural) {
      // Nothing about the content changed (e.g. only a scrollbar toggled
      // while the box was still growing): pick the glide back up where it
      // was rather than restarting it from the in-flight height.
      play(fromRef.current, natural, resumeAt);
      return;
    }
    if (Math.abs(natural - current) < 0.5) return;
    play(current, natural, null);
  }, [containerRef]);

  // Caller-driven changes, before paint: a hold transition (freeze on
  // entry, glide to the natural height on exit) or a content swap.
  useLayoutEffect(() => {
    holdRef.current = hold;
    sync();
  }, [hold, revision, sync]);

  // Every content size change (section switch, data arriving, a textarea
  // growing) re-syncs. ResizeObserver callbacks run before paint, and an
  // animation started there is applied to that same frame, so the box never
  // paints at the new size before the glide starts. Detaching (content
  // unmounted, e.g. the modal closed) forgets the settled height so the
  // next mount paints at its natural size without gliding from stale state.
  const observerRef = useRef<ResizeObserver | null>(null);
  return useCallback((el: HTMLElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!el) {
      animationRef.current?.cancel();
      animationRef.current = null;
      fromRef.current = null;
      targetRef.current = null;
      lastHeightRef.current = null;
      return;
    }
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => sync());
    observer.observe(el);
    observerRef.current = observer;
  }, [sync]);
}
