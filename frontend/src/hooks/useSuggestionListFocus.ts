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
 * - Focus was in a row that is gone: the first row that followed it and is
 *   still listed (the next one, even when several left at once), else the
 *   last row (the previous one), else the fallback. A row that left
 *   without going busy first (a re-read dropped it, another tab resolved
 *   it) while its text field had focus sends focus to the fallback, so the
 *   user's typing never lands in another suggestion's draft.
 * - Focus was in a row that is still there (a failed accept or dismiss):
 *   while nothing in it is enabled (busy), wait; then back to the element
 *   that had focus if it is enabled again (unless it is the text field and
 *   the last input was a tap), else the row's text field.
 * - Focus was on another control of the surface (Accept all, Dismiss all,
 *   a close button): back to it while it is still there (waiting while it
 *   is disabled), else the first row, else the fallback.
 *
 * In a row the target is its text field (`.memory-suggestion-content`)
 * when enabled, else its first enabled action button
 * (`.memory-suggestion-actions button`); a row with neither is busy, and
 * the rule waits for it. After a touch or pen tap the target is the action
 * button instead: focusing a text field from script opens the on-screen
 * keyboard. A target that does not take focus (hidden, e.g. a phone-only
 * fallback on a wide screen) passes to the next one; when none does, a
 * later commit tries again. Rows are found by `data-suggestion-id` (set by
 * MemorySuggestionRow) and ordered by `rowIds`.
 *
 * The hook runs from the calling component's layout effect, so the commits
 * that disable a row (busy) and re-enable it must re-render that component:
 * keep the rows' busy state in the caller, not inside the row.
 *
 * Focus is never taken back from elsewhere: focus landing on, or a pointer
 * pressed on, anything outside the container (the user tabbed away, typed
 * in the composer, clicked or selected text elsewhere) forgets the
 * surface, and focus lost while the element that had it is still there
 * and enabled (a click on a blank area of the surface) stays lost. Only an
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
  /**
   * The ids of the rendered rows, in display order. The calling component
   * must re-render when a row goes busy and when it is enabled again (keep
   * the busy state there), as the hook acts from its layout effect.
   */
  rowIds: readonly string[];
  /**
   * Where focus goes when no row is left (a heading needs `tabIndex={-1}`).
   * It may sit outside the container; one that is hidden is skipped.
   */
  fallbackRef?: RefObject<HTMLElement | null>;
}

interface FocusRecord {
  /** The element that had focus last. */
  element: HTMLElement;
  /** The row it is in (null: another control of the surface). */
  rowId: string | null;
  /** The ids listed after that row, as of the last commit that listed it. */
  following: readonly string[];
  /** That row was seen busy (its focused control disabled) while listed. */
  rowWentBusy: boolean;
  /** The element left or was disabled since it had focus. */
  displaced: boolean;
}

const ROW_SELECTOR = '[data-suggestion-id]';
const TEXT_FIELD_SELECTOR = '.memory-suggestion-content';

function isFocusLost(active: Element | null): boolean {
  return !active || active === active.ownerDocument.body || !active.isConnected;
}

function isEnabled(el: Element): boolean {
  return !el.matches(':disabled');
}

/** A tap rather than a mouse click: pen counts as touch. */
export function isTouchPointer(e: Event): boolean {
  const type = (e as PointerEvent).pointerType;
  return type === 'touch' || type === 'pen';
}

function findRow(container: HTMLElement, id: string): HTMLElement | null {
  for (const row of container.querySelectorAll<HTMLElement>(ROW_SELECTOR)) {
    if (row.dataset.suggestionId === id) return row;
  }
  return null;
}

/**
 * A row's text field when enabled (its first enabled action after a tap),
 * else its first enabled action; null while busy.
 */
function rowTarget(row: HTMLElement, touch: boolean): HTMLElement | null {
  const action = row.querySelector<HTMLElement>('.memory-suggestion-actions button:not(:disabled)');
  const field = row.querySelector<HTMLElement>(TEXT_FIELD_SELECTOR);
  const enabledField = field && isEnabled(field) ? field : null;
  return touch ? action ?? enabledField : enabledField ?? action;
}

/**
 * Where focus goes now, in order of preference (the fallback last), or
 * ``'wait'`` while the element or row it belongs to is still there but
 * disabled.
 */
function focusTargets(
  container: HTMLElement,
  record: FocusRecord,
  rowIds: readonly string[],
  fallback: HTMLElement | null,
  touch: boolean,
): HTMLElement[] | 'wait' {
  const { element } = record;
  const withFallback = (first: HTMLElement | null) =>
    [first, fallback].filter((el): el is HTMLElement => el !== null && el.isConnected);
  const rowById = (id: string | undefined): HTMLElement | null | 'wait' => {
    const row = id === undefined ? null : findRow(container, id);
    return row ? rowTarget(row, touch) ?? 'wait' : null;
  };

  if (record.rowId !== null) {
    const row = findRow(container, record.rowId);
    if (row) {
      const inRow = rowTarget(row, touch);
      if (!inRow) return 'wait';
      // A tap on Accept leaves focus on the text field in iOS Safari; the
      // field is not focused again from script after a tap either.
      const keep = row.contains(element) && isEnabled(element)
        && !(touch && element.matches(TEXT_FIELD_SELECTOR));
      return [keep ? element : inRow];
    }
    if (!record.rowWentBusy && element.matches(TEXT_FIELD_SELECTOR)) return withFallback(null);
    const listed = new Set(rowIds);
    const next = rowById(record.following.find((id) => listed.has(id)) ?? rowIds[rowIds.length - 1]);
    return next === 'wait' ? next : withFallback(next);
  }
  if (element.isConnected && container.contains(element)) {
    return isEnabled(element) ? [element] : 'wait';
  }
  const first = rowById(rowIds[0]);
  return first === 'wait' ? first : withFallback(first);
}

export function useSuggestionListFocus({
  containerRef,
  rowIds,
  fallbackRef,
}: SuggestionListFocusOptions): void {
  const recordRef = useRef<FocusRecord | null>(null);
  const rowIdsRef = useRef(rowIds);
  const containerSeenRef = useRef<HTMLElement | null>(null);
  // The last input was a touch or pen tap (not a mouse click or a key).
  const touchRef = useRef(false);

  useEffect(() => {
    // Every focus move in the document: inside the container it becomes
    // the record, anywhere else it forgets the surface.
    const onFocusIn = (e: FocusEvent) => {
      const container = containerRef.current;
      const el = e.target;
      if (!container || !(el instanceof HTMLElement) || !container.contains(el)) {
        recordRef.current = null;
        return;
      }
      const row = el.closest<HTMLElement>(ROW_SELECTOR);
      const rowId = row && container.contains(row) ? row.dataset.suggestionId ?? null : null;
      const rowIds = rowIdsRef.current;
      recordRef.current = {
        element: el,
        rowId,
        following: rowId === null ? [] : rowIds.slice(rowIds.indexOf(rowId) + 1),
        rowWentBusy: false,
        displaced: false,
      };
    };
    // A press outside the container (clicking or selecting text elsewhere
    // moves focus to <body> without a focusin) forgets the surface too.
    const onPointerDown = (e: PointerEvent) => {
      touchRef.current = isTouchPointer(e);
      const container = containerRef.current;
      if (!container || !(e.target instanceof Node) || !container.contains(e.target)) {
        recordRef.current = null;
      }
    };
    const onKeyDown = () => {
      touchRef.current = false;
    };
    document.addEventListener('focusin', onFocusIn);
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('focusin', onFocusIn);
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown, true);
    };
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

    const { element } = record;
    const available = element.isConnected && container.contains(element) && isEnabled(element);
    if (record.rowId !== null) {
      const index = rowIds.indexOf(record.rowId);
      if (index >= 0) {
        record.following = rowIds.slice(index + 1);
        if (!available && findRow(container, record.rowId)?.contains(element)) {
          record.rowWentBusy = true;
        }
      }
    }
    if (!available) record.displaced = true;
    if (!isFocusLost(document.activeElement)) {
      if (available) record.displaced = false;
      return;
    }
    if (!record.displaced) return;

    const targets = focusTargets(
      container,
      record,
      rowIds,
      fallbackRef?.current ?? null,
      touchRef.current,
    );
    if (targets === 'wait') return;
    if (targets.length === 0) {
      recordRef.current = null;
      return;
    }
    // The first that takes focus wins (a hidden one does not); focusing a
    // target inside the container records it again (focusin). When none
    // does, the record stays for a later commit to retry.
    for (const target of targets) {
      target.focus();
      if (document.activeElement === target) return;
    }
  });
}
