/**
 * Unsaved-changes guard for an editor in an app mounted under a plain
 * `BrowserRouter` (no data router, so React Router's `useBlocker` is not
 * available).
 *
 * While `when` is true:
 * - closing / reloading the tab asks the browser's own "Leave site?" prompt
 *   (`beforeunload`);
 * - a left click on a same-origin `<a href>` anywhere in the page (no
 *   modifier keys, no `target`, no `download`) is stopped in the CAPTURE
 *   phase on `document` -- before React Router's `<Link>` handler or the
 *   browser's own navigation -- and handed to `onBlockedNavigation(to)` with
 *   the destination's path + search + hash, so the caller can confirm and
 *   then `navigate(to)` itself.
 *
 * NOT covered: browser back / forward (BrowserRouter pops history without a
 * hook to veto it) and code that calls `navigate()` from a button (e.g. the
 * Sidebar's rows). Callers cover those with a draft backup (see DocEditor).
 */

import { useEffect, useRef } from 'react';

export interface UnsavedChangesGuardOptions {
  /** Guard active (there are unsaved changes). */
  when: boolean;
  /** An intercepted same-origin link click; `to` = path + search + hash. */
  onBlockedNavigation: (to: string) => void;
}

/**
 * The in-app destination of a link click the guard should intercept, or
 * null to let the click through: not a plain left click, not on an
 * `<a href>`, a link that opens elsewhere (`target`, `download`), another
 * origin, or the current page itself (same path + search, e.g. a `#hash`).
 */
export function interceptedLinkTarget(
  event: MouseEvent,
  location: Pick<Location, 'href' | 'origin' | 'pathname' | 'search'> = window.location,
): string | null {
  if (event.defaultPrevented || event.button !== 0) return null;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return null;
  const target = event.target;
  const element =
    target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
  const anchor = element?.closest('a[href]');
  if (!(anchor instanceof HTMLAnchorElement)) return null;
  const linkTarget = anchor.getAttribute('target');
  if (linkTarget && linkTarget !== '_self') return null;
  if (anchor.hasAttribute('download')) return null;
  let url: URL;
  try {
    url = new URL(anchor.getAttribute('href') ?? '', location.href);
  } catch {
    return null;
  }
  if (url.origin !== location.origin) return null;
  if (url.pathname === location.pathname && url.search === location.search) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

export function useUnsavedChangesGuard({ when, onBlockedNavigation }: UnsavedChangesGuardOptions): void {
  // Latest callback, so the listeners need not be re-attached per render.
  const callbackRef = useRef(onBlockedNavigation);
  callbackRef.current = onBlockedNavigation;

  useEffect(() => {
    if (!when) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Older Chromium / Safari only prompt when returnValue is set.
      event.returnValue = '';
    };
    const onClick = (event: MouseEvent) => {
      const to = interceptedLinkTarget(event);
      if (to === null) return;
      event.preventDefault();
      // Keep React (and so a <Link>'s own navigate) from seeing the click.
      event.stopPropagation();
      callbackRef.current(to);
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    document.addEventListener('click', onClick, true);
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload);
      document.removeEventListener('click', onClick, true);
    };
  }, [when]);
}
