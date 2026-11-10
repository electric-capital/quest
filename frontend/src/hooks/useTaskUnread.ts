/**
 * React bindings for the task entries of the unread store
 * (store/unreadStore.ts), the task counterparts of
 * hooks/useConversationUnread.ts:
 *
 * - useTaskUnread: whether a task row shows the unread dot.
 * - useMarkTaskViewed: called by the task thread to say which task it is
 *   showing; while it is on screen in a focused window it is acknowledged
 *   as read.
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
 * Report the task the main view is showing, once its detail has loaded (so
 * the store knows its seqs).
 */
export function useMarkTaskViewed(taskId: string | null, isLoaded: boolean): void {
  useEffect(() => {
    if (!taskId || !isLoaded) return;
    unreadStore.setViewedTask(taskId);
    return () => unreadStore.clearViewedTask(taskId);
  }, [taskId, isLoaded]);
}
