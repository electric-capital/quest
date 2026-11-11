/**
 * React bindings for the task entries of the unread store
 * (store/unreadStore.ts), the task counterparts of
 * hooks/useConversationUnread.ts:
 *
 * - useTaskUnread: whether a task row shows the unread dot.
 * - useMarkTaskViewed: called by the task page while the task's newest
 *   update is on screen; then, in a focused window, the task is
 *   acknowledged as read.
 *
 * The focus / visibility re-check needs no second listener:
 * useUnreadTracking already calls `unreadStore.acknowledgeViewed()`, which
 * covers the viewed task too.
 */

import { useEffect, useSyncExternalStore } from 'react';
import type { TaskListRow } from '../api/types';
import { unreadStore } from '../store/unreadStore';

export function useTaskUnread(row: Pick<TaskListRow, 'id' | 'unread_seq' | 'read_seq'>): boolean {
  const { id, unread_seq: unreadSeq, read_seq: readSeq } = row;
  return useSyncExternalStore(
    unreadStore.subscribe,
    () => unreadStore.isTaskUnread(id, unreadSeq, readSeq),
  );
}

/**
 * Report the task whose newest update the main view is showing. `seen` is
 * the page's rule (TaskPage): the detail has loaded (so the store knows its
 * seqs) and the newest update is on screen, or the run panel shows the
 * newest run. While it is false the task is not acknowledged, so a page
 * opened at the brief never marks an update read that was never shown.
 */
export function useMarkTaskViewed(taskId: string | null, seen: boolean): void {
  useEffect(() => {
    if (!taskId || !seen) return;
    unreadStore.setViewedTask(taskId);
    return () => unreadStore.clearViewedTask(taskId);
  }, [taskId, seen]);
}
