import { useLayoutEffect, useState, type RefObject } from 'react';

/**
 * Whether the element in `ref` overflows its clamp (its content is taller
 * than the clamped box), for a "Show more" toggle.
 *
 * Measured only while `clamped` is true (and again whenever `content`
 * changes), on mount and on every resize of the element or its first child
 * element; while not clamped the last answer is kept, so expanding never
 * flips the toggle away. `initial` is the answer before the first
 * measurement.
 */
export function useClampOverflow(
  ref: RefObject<HTMLElement | null>,
  clamped: boolean,
  content: unknown,
  initial: boolean,
): boolean {
  const [overflows, setOverflows] = useState(initial);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!clamped || !el) return;
    const check = () => setOverflows(el.scrollHeight > el.clientHeight + 1);
    check();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(check);
    observer.observe(el);
    const inner = el.firstElementChild;
    if (inner) observer.observe(inner);
    return () => observer.disconnect();
  }, [ref, clamped, content]);
  return overflows;
}
