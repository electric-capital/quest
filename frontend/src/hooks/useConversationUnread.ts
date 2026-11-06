/**
 * React bindings for the unread store (store/unreadStore.ts):
 *
 * - useUnreadTracking: mounted once by the app shell; feeds the
 *   `conversation_unread_changed` event into the store and re-checks the
 *   viewed conversation whenever the window gains focus or visibility.
 *   Its argument switches acknowledging off (admin impersonation).
 * - useMarkConversationViewed: called by the main view (ChatPanel) to say
 *   which conversation it is showing.
 * - useConversationUnread: whether a sidebar row shows the unread dot.
 */

import { useEffect, useSyncExternalStore } from 'react';
import type { Conversation } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { unreadStore } from '../store/unreadStore';

export function useUnreadTracking(acknowledge: boolean): void {
  // An impersonating admin watches the owner's unread state without
  // changing it.
  useEffect(() => {
    unreadStore.setAcknowledging(acknowledge);
  }, [acknowledge]);

  useEffect(() => {
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'conversation_unread_changed') return;
      if (typeof event.conversation_id !== 'string') return;
      unreadStore.merge(event.conversation_id, event.unread_seq, event.read_seq);
    });
    const onFocusChange = () => unreadStore.acknowledgeViewed();
    window.addEventListener('focus', onFocusChange);
    document.addEventListener('visibilitychange', onFocusChange);
    return () => {
      unsubscribe();
      window.removeEventListener('focus', onFocusChange);
      document.removeEventListener('visibilitychange', onFocusChange);
    };
  }, []);
}

/**
 * Report the conversation the main view is showing, once it has loaded.
 * While it is on screen in a focused window it is acknowledged as read.
 */
export function useMarkConversationViewed(conversationId: string, isLoaded: boolean): void {
  useEffect(() => {
    if (!conversationId || !isLoaded) return;
    unreadStore.setViewed(conversationId);
    return () => unreadStore.clearViewed(conversationId);
  }, [conversationId, isLoaded]);
}

export function useConversationUnread(
  conversation: Pick<Conversation, 'id' | 'unread_seq' | 'read_seq'>,
): boolean {
  const { id, unread_seq: unreadSeq, read_seq: readSeq } = conversation;
  return useSyncExternalStore(
    unreadStore.subscribe,
    () => unreadStore.isUnread(id, unreadSeq, readSeq),
  );
}
