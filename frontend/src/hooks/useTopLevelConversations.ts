/**
 * Data layer for the Sidebar's top-level (standalone) conversation list:
 * paged fetching, filter toggles, background refreshes driven by the
 * realtime layer, and optimistic local edits. Rendering (including the
 * auto-paging "Load more" row) lives in
 * components/sidebar/ConversationsSection.tsx.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { fetchConversations, ApiClientError } from '../api/client';
import type { Conversation } from '../api/types';
import { webSocketManager } from '../services/WebSocketManager';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { applyConversationFilters } from '../utils/sidebarItems';

/** Page size for the paged top-level conversation list. */
export const CONVERSATIONS_PAGE_SIZE = 30;

export interface TopLevelConversationFilters {
  archived: boolean;
  slack: boolean;
  inference: boolean;
}

export type ConversationListUpdater = (updater: (prev: Conversation[]) => Conversation[]) => void;

export interface TopLevelConversations {
  // Rows to render: the loaded window with the filter toggles applied
  // client-side (instant hide; the server-side refetch reveals newly
  // included rows).
  conversations: Conversation[];
  loading: boolean;
  loadingMore: boolean;
  // Whether an older page exists (keyset cursor present).
  hasMore: boolean;
  // The sidebar's one error-banner slot. Load failures land here; the
  // Sidebar's own actions (new chat, run routine, duplicate) reuse it.
  error: string | null;
  setError: (message: string | null) => void;
  filters: TopLevelConversationFilters;
  setFilter: (key: keyof TopLevelConversationFilters, value: boolean) => void;
  // Fetch the next (older) page and append it. Identity changes whenever the
  // cursor or the in-flight flag does, so an effect keyed on it re-runs
  // after every page lands.
  loadMore: () => void;
  // Re-fetch the loaded window stale-while-revalidate (no loading flash,
  // errors ignored).
  refreshSilently: () => Promise<void>;
  // Optimistic local edit of the loaded rows (archive / rename / remove).
  updateConversations: ConversationListUpdater;
}

export function useTopLevelConversations(): TopLevelConversations {
  // Standalone conversations (paged: `nextCursor` is the keyset cursor for
  // the next older page, null when everything loaded is all there is)
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [loadingMore, setLoadingMore] = useState<boolean>(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // All three filters default to false (hide archived, hide Slack and
  // Inference API conversations) on every page load; no persistence.
  const [filters, setFilters] = useState<TopLevelConversationFilters>({
    archived: false,
    slack: false,
    inference: false,
  });
  // Number of loaded rows, so a background refresh can re-fetch the window
  // the user has already paged through instead of collapsing to page one.
  // Written only from the fetch callbacks.
  const loadedCountRef = useRef(0);

  // Shared fetch for the top-level list. `reset` collapses back to the first
  // page (filter toggle changed); otherwise the refetch spans the window the
  // user has already loaded so background refreshes don't shrink the list.
  // `silent` keeps the existing list visible while fetching
  // (stale-while-revalidate) for background refreshes after stream
  // completion, so the sidebar doesn't flash "Loading...". Rebuilt whenever
  // a filter changes, so the realtime subscriptions below (which depend on
  // it) always refetch with the current filters.
  const refresh = useCallback(async ({ reset = false, silent = false } = {}) => {
    const limit = reset
      ? CONVERSATIONS_PAGE_SIZE
      : Math.max(loadedCountRef.current, CONVERSATIONS_PAGE_SIZE);
    try {
      if (!silent) {
        setLoading(true);
        setError(null);
      }
      const response = await fetchConversations({
        includeArchived: filters.archived,
        excludeProjects: true,
        includeSlack: filters.slack,
        includeInference: filters.inference,
        limit,
      });
      // Server already excludes project conversations; keep the client-side
      // predicate as a safety net for stale/cached responses.
      const rows = response.conversations.filter((c) => !c.project_id);
      setConversations(rows);
      loadedCountRef.current = rows.length;
      setNextCursor(response.next_cursor ?? null);
    } catch (err) {
      if (silent) return; // Silently ignore errors during background refresh
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to load conversations');
      }
    } finally {
      if (!silent) setLoading(false);
    }
  }, [filters.archived, filters.slack, filters.inference]);

  const refreshSilently = useCallback(() => refresh({ silent: true }), [refresh]);

  // Load on mount and whenever a filter toggle changes (`refresh` is rebuilt
  // on every filter change). Filters are enforced server-side, so toggling
  // Slack/Inference visibility needs a refetch (the instant client-side pass
  // only ever hides rows).
  useEffect(() => {
    void refresh({ reset: true });
  }, [refresh]);

  // Fetch the next (older) page and append it, deduped by id. Keyset
  // pagination means conversations created since the last fetch can't shift
  // rows into this page, but a bumped-to-top conversation could still appear
  // twice without the dedupe.
  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const response = await fetchConversations({
        includeArchived: filters.archived,
        excludeProjects: true,
        includeSlack: filters.slack,
        includeInference: filters.inference,
        limit: CONVERSATIONS_PAGE_SIZE,
        cursor: nextCursor,
      });
      setConversations((prev) => {
        const seen = new Set(prev.map((c) => c.id));
        const merged = [
          ...prev,
          ...response.conversations.filter((c) => !c.project_id && !seen.has(c.id)),
        ];
        loadedCountRef.current = merged.length;
        return merged;
      });
      setNextCursor(response.next_cursor ?? null);
    } catch {
      // Keep the current list; scrolling (or clicking) retries.
    } finally {
      setLoadingMore(false);
    }
  }, [nextCursor, loadingMore, filters.archived, filters.slack, filters.inference]);

  // Realtime: refresh when an LLM response finishes (a conversation's
  // activity re-sorts it) and whenever the server advertises a
  // conversation-list mutation (new chat created on first send, new
  // Slack-bot conversation, archive, rename, model change). Cheaper than a
  // poll and immediate. Renames are applied in place.
  useEffect(() => {
    const unsubStreamComplete = webSocketManager.onStreamComplete(() => {
      void refresh({ silent: true });
    });
    const unsubRenamed = webSocketManager.onConversationRenamed((conversationId, customName) => {
      setConversations((prev) =>
        prev.map((c) =>
          c.id === conversationId ? { ...c, custom_name: customName, title: customName } : c
        )
      );
    });
    const unsubListChanged = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'conversation_list_changed') return;
      void refresh({ silent: true });
    });
    return () => {
      unsubStreamComplete();
      unsubRenamed();
      unsubListChanged();
    };
  }, [refresh]);

  const setFilter = useCallback((key: keyof TopLevelConversationFilters, value: boolean) => {
    setFilters((prev) => (prev[key] === value ? prev : { ...prev, [key]: value }));
  }, []);

  const updateConversations = useCallback<ConversationListUpdater>((updater) => {
    setConversations(updater);
  }, []);

  const visible = useMemo(
    () => applyConversationFilters(conversations, filters.archived, filters.slack, filters.inference),
    [conversations, filters.archived, filters.slack, filters.inference],
  );

  return {
    conversations: visible,
    loading,
    loadingMore,
    hasMore: nextCursor !== null,
    error,
    setError,
    filters,
    setFilter,
    loadMore,
    refreshSilently,
    updateConversations,
  };
}
