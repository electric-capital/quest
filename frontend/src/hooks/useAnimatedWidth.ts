/**
 * Glide an element's width when its label changes.
 *
 * A button whose text swaps ("Approve" -> "Processing...") re-lays out to
 * the new label's width in one frame, which reads as a jolt in a row of
 * buttons. Attach the returned ref to the element and pass the label as
 * `label`: whenever it changes, the element animates from the width it had
 * before the swap to its new natural width instead of snapping.
 *
 * Uses the Web Animations API (element.animate) so no inline width sticks
 * around afterwards, retargets smoothly when the label changes again
 * mid-animation, and respects `prefers-reduced-motion`. The element should
 * carry `white-space: nowrap; overflow: hidden` so the incoming label is
 * clipped while the box catches up rather than wrapping.
 */

import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

const ANIMATION_MS = 200;
const EASING = 'cubic-bezier(0.4, 0, 0.2, 1)';

/**
 * Horizontal padding + border of `el`, which the `width` keyframes must
 * exclude for a content-box element (border-box elements animate the full
 * box directly).
 */
function widthInset(el: HTMLElement): number {
  const cs = getComputedStyle(el);
  if (cs.boxSizing === 'border-box') return 0;
  return (
    parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight)
    + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth)
  );
}

export function useAnimatedWidth<T extends HTMLElement>(label: unknown): RefObject<T | null> {
  const ref = useRef<T | null>(null);
  // Width from the last measurement: the "before" side of the next swap.
  const prevWidthRef = useRef<number | null>(null);
  const animationRef = useRef<Animation | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;

    const running = animationRef.current;
    // Start from where the box visually is right now: mid-flight if a
    // previous swap is still animating, else the last settled width.
    const from = running ? el.getBoundingClientRect().width : prevWidthRef.current;
    if (running) {
      running.cancel();
      animationRef.current = null;
    }
    const next = el.getBoundingClientRect().width;
    prevWidthRef.current = next;

    if (from === null || Math.abs(from - next) < 0.5) return;
    if (typeof el.animate !== 'function') return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    const inset = widthInset(el);
    const anim = el.animate(
      [{ width: `${from - inset}px` }, { width: `${next - inset}px` }],
      { duration: ANIMATION_MS, easing: EASING },
    );
    animationRef.current = anim;
    const clear = () => {
      if (animationRef.current === anim) animationRef.current = null;
    };
    anim.onfinish = clear;
    anim.oncancel = clear;
  }, [label]);

  // Keep the "before" width fresh when the box resizes for reasons other
  // than a label swap (window resize, flex reflow) so the next swap starts
  // from the real current width. Ignored mid-animation: those sizes are
  // transient.
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      if (!animationRef.current) {
        prevWidthRef.current = el.getBoundingClientRect().width;
      }
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      animationRef.current?.cancel();
    };
  }, []);

  return ref;
}
