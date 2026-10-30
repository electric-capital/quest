/**
 * useSuggestionListFocus -- keeps keyboard focus in a list of suggestion
 * rows (MemorySuggestionRow) as rows are accepted or dismissed.
 *
 * Accepting or dismissing a suggestion unmounts its row, and while the
 * action runs the row's controls are disabled; either way the browser drops
 * focus to <body>, and a keyboard or screen-reader user loses their place
 * and starts again from the top of the page. Every surface that reviews
 * suggestions (the composer bubble's panel, the inbox's Memories tab, the
 * doc viewer's Notes panel) follows one rule instead, applied after each
 * commit while focus is lost (`document.activeElement` is null, <body> or
 * no longer in the document) and was last inside the container:
 *
 * - Focus was in a row that is gone: the row now at that row's index (the
 *   next one), else the last row (the previous one), else the fallback.
 * - Focus was in a row that is still there (a failed accept or dismiss):
 *   while nothing in it is enabled (busy), wait; then back to the element
 *   that had focus if it is enabled again, else the row's text field.
 * - Focus was on another control of the surface (Accept all, Dismiss all,
 *   a close button): back to it while it is still there (waiting while it
 *   is disabled), else the first row, else the fallback.
 *
 * In a row the target is its text field (`.memory-suggestion-content`)
 * when enabled, else its first enabled action button
 * (`.memory-suggestion-actions button`); a row with neither is busy, and
 * the rule waits for it. Rows are found by `data-suggestion-id` (set by
 * MemorySuggestionRow) and placed by `rowIds`.
 *
 * Focus is never taken back from elsewhere: focus landing on any element
 * outside the container (the user tabbed away, typed in the composer)
 * forgets the surface, and focus lost while the element that had it is
 * still there and enabled (a click on a blank area) stays lost. Only an
 * element that left, or was disabled, since it was focused hands focus on.
 * Closing a surface that keeps its container (the bubble's panel) unmounts
 * the focused control, so the same rule returns focus to the fallback.
 */

import { useEffect, useLayoutEffect, useRef } from 'react';
import type { RefObject } from 'react';

export interface SuggestionListFocusOptions {
  /**
   * The surface: its rows (each with `data-suggestion-id`) and its other
   * controls. It may mount and unmount; the hook follows the element.
   */
  containerRef: RefObject<HTMLElement | null>;
  /** The ids of the rendered rows, in display order. */
  rowIds: readonly string[];
  /**
   * Where focus goes when no row is left (a heading needs `tabIndex={-1}`).
   * It may sit outside the container.
   */
  fallbackRef?: RefObject<HTMLElement | null>;
}

interface FocusRecord {
  /** The element that had focus last. */
  element: HTMLElement;
  /** The row it is in (null: another control of the surface). */
  rowId: string | null;
  /** That row's index in `rowIds`, kept current while the row is listed. */
  index: number;
  /** The element left or was disabled since it had focus. */
  displaced: boolean;
}

const ROW_SELECTOR = '[data-suggestion-id]';

function isFocusLost(active: Element | null): boolean {
  return !active || active === active.ownerDocument.body || !active.isConnected;
}

function isEnabled(el: Element): boolean {
  return !el.matches(':disabled');
}

function findRow(container: HTMLElement, id: string): HTMLElement | null {
  for (const row of container.querySelectorAll<HTMLElement>(ROW_SELECTOR)) {
    if (row.dataset.suggestionId === id) return row;
  }
  return null;
}

/** A row's text field when enabled, else its first enabled action; null while busy. */
function rowTarget(row: HTMLElement): HTMLElement | null {
  const field = row.querySelector<HTMLElement>('.memory-suggestion-content');
  if (field && isEnabled(field)) return field;
  return row.querySelector<HTMLElement>('.memory-suggestion-actions button:not(:disabled)');
}

/**
 * Where focus goes now, ``null`` to give up, or ``'wait'`` while the
 * element or row it belongs to is still there but disabled.
 */
function focusTarget(
  container: HTMLElement,
  record: FocusRecord,
  rowIds: readonly string[],
  fallback: HTMLElement | null,
): HTMLElement | null | 'wait' {
  const { element } = record;
  const rowAt = (index: number): HTMLElement | null | 'wait' => {
    const row = findRow(container, rowIds[index]);
    return row ? rowTarget(row) ?? 'wait' : null;
  };
  if (record.rowId !== null) {
    const row = findRow(container, record.rowId);
    if (row) {
      const inRow = rowTarget(row);
      if (!inRow) return 'wait';
      return row.contains(element) && isEnabled(element) ? element : inRow;
    }
    if (rowIds.length > 0) {
      const next = rowAt(Math.min(Math.max(record.index, 0), rowIds.length - 1));
      if (next) return next;
    }
  } else {
    if (element.isConnected && container.contains(element)) {
      return isEnabled(element) ? element : 'wait';
    }
    if (rowIds.length > 0) {
      const first = rowAt(0);
      if (first) return first;
    }
  }
  return fallback?.isConnected ? fallback : null;
}

export function useSuggestionListFocus({
  containerRef,
  rowIds,
  fallbackRef,
}: SuggestionListFocusOptions): void {
  const recordRef = useRef<FocusRecord | null>(null);
  const rowIdsRef = useRef(rowIds);
  const containerSeenRef = useRef<HTMLElement | null>(null);

  // Every focus move in the document: inside the container it becomes the
  // record, anywhere else it forgets the surface.
  useEffect(() => {
    const onFocusIn = (e: FocusEvent) => {
      const container = containerRef.current;
      const el = e.target;
      if (!container || !(el instanceof HTMLElement) || !container.contains(el)) {
        recordRef.current = null;
        return;
      }
      const row = el.closest<HTMLElement>(ROW_SELECTOR);
      const rowId = row && container.contains(row) ? row.dataset.suggestionId ?? null : null;
      recordRef.current = {
        element: el,
        rowId,
        index: rowId === null ? -1 : rowIdsRef.current.indexOf(rowId),
        displaced: false,
      };
    };
    document.addEventListener('focusin', onFocusIn);
    return () => document.removeEventListener('focusin', onFocusIn);
  }, [containerRef]);

  // After every commit (cheap: a few checks, DOM queries only once focus
  // has been lost from a control that left).
  useLayoutEffect(() => {
    rowIdsRef.current = rowIds;
    const container = containerRef.current;
    if (container !== containerSeenRef.current) {
      // A new or vanished container: nothing recorded applies to it.
      containerSeenRef.current = container;
      recordRef.current = null;
    }
    const record = recordRef.current;
    if (!container || !record) return;

    if (record.rowId !== null) {
      const index = rowIds.indexOf(record.rowId);
      if (index >= 0) record.index = index;
    }
    const { element } = record;
    const available = element.isConnected && container.contains(element) && isEnabled(element);
    if (!available) record.displaced = true;
    if (!isFocusLost(document.activeElement)) {
      if (available) record.displaced = false;
      return;
    }
    if (!record.displaced) return;

    const target = focusTarget(container, record, rowIds, fallbackRef?.current ?? null);
    if (target === 'wait') return;
    recordRef.current = null;
    // Focusing a target inside the container records it again (focusin).
    target?.focus();
  });
}
