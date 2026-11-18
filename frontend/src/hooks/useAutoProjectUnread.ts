/**
 * React bindings for the AutoProject entries of the unread store
 * (store/unreadStore.ts), the AutoProject counterparts of
 * hooks/useConversationUnread.ts:
 *
 * - useAutoProjectUnread: whether an AutoProject row shows the unread dot.
 * - useMarkAutoProjectViewed: called by the AutoProject page while the AutoProject's newest
 *   update is on screen; then, in a focused window, the AutoProject is
 *   acknowledged as read.
 *
 * The focus / visibility re-check needs no second listener:
 * useUnreadTracking already calls `unreadStore.acknowledgeViewed()`, which
 * covers the viewed AutoProject too.
 */

import { useEffect, useSyncExternalStore } from 'react';
import type { AutoProjectListRow } from '../api/types';
import { unreadStore } from '../store/unreadStore';

export function useAutoProjectUnread(row: Pick<AutoProjectListRow, 'id' | 'unread_seq' | 'read_seq'>): boolean {
  const { id, unread_seq: unreadSeq, read_seq: readSeq } = row;
  return useSyncExternalStore(
    unreadStore.subscribe,
    () => unreadStore.isAutoProjectUnread(id, unreadSeq, readSeq),
  );
}

/**
 * Report the AutoProject whose newest update the main view is showing. `seen` is
 * the page's rule (AutoProjectPage): the detail has loaded (so the store knows its
 * seqs) and the newest update is on screen, or the run panel shows the
 * newest run. While it is false the AutoProject is not acknowledged, so a page
 * opened at the brief never marks an update read that was never shown.
 */
export function useMarkAutoProjectViewed(autoprojectId: string | null, seen: boolean): void {
  useEffect(() => {
    if (!autoprojectId || !seen) return;
    unreadStore.setViewedAutoProject(autoprojectId);
    return () => unreadStore.clearViewedAutoProject(autoprojectId);
  }, [autoprojectId, seen]);
}
