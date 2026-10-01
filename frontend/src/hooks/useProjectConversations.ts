/**
 * Data layer for the Sidebar's per-project conversation lists (keyed by
 * project id): on-demand loading, the 30s visibility-aware poll while drilled
 * into a project, realtime refreshes, and optimistic local edits. Rendering
 * lives in components/sidebar/ProjectPanel.tsx.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchProjectConversations } from '../api/client';
import type { Conversation } from '../api/types';
import { webSocketManager } from '../services/WebSocketManager';
import { persistentWebSocket } from '../services/PersistentWebSocket';

/** How often to poll for new project conversations when drilled down (ms). */
export const PROJECT_CONVERSATIONS_POLL_INTERVAL_MS = 30_000;

export interface ProjectConversations {
  byProject: Record<string, Conversation[]>;
  loadingByProject: Record<string, boolean>;
  // Fetch (or re-fetch) one project's list. `silent` skips the loading flag
  // so the existing rows stay rendered (stale-while-revalidate) -- used by
  // the new-chat / run-routine flows to avoid a "Loading..." flash after
  // creating a conversation. Resolves to the fetched rows ([] on failure).
  load: (projectId: string, silent?: boolean) => Promise<Conversation[]>;
  // Optimistic local edit of one project's rows (archive / rename).
  update: (projectId: string, updater: (prev: Conversation[]) => Conversation[]) => void;
  // Register a just-created project as having no conversations yet, so
  // drilling into it doesn't fetch.
  seedEmpty: (projectId: string) => void;
}

export function useProjectConversations(
  drilledProjectId: string | null,
  showArchived: boolean,
): ProjectConversations {
  const [byProject, setByProject] = useState<Record<string, Conversation[]>>({});
  const [loadingByProject, setLoadingByProject] = useState<Record<string, boolean>>({});
  // Guard to prevent overlapping poll fetches
  const pollInFlightRef = useRef(false);

  const load = useCallback(async (projectId: string, silent: boolean = false): Promise<Conversation[]> => {
    if (!silent) {
      setLoadingByProject((prev) => ({ ...prev, [projectId]: true }));
    }
    try {
      const response = await fetchProjectConversations(projectId, showArchived);
      setByProject((prev) => ({ ...prev, [projectId]: response.conversations }));
      return response.conversations;
    } catch (err) {
      console.error('Failed to load project conversations:', err);
      return [];
    } finally {
      if (!silent) {
        setLoadingByProject((prev) => ({ ...prev, [projectId]: false }));
      }
    }
  }, [showArchived]);

  // Realtime: refresh the drilled list when an LLM response finishes and
  // whenever the server advertises a conversation-list mutation. Both
  // stale-while-revalidate: the drilled project list must NOT flip its
  // loading flag here, or the whole drill-down panel (routines + rows) is
  // replaced by "Loading..." and re-mounted on every event instead of the
  // new row simply appearing. Renames are applied in place across every
  // cached project list.
  useEffect(() => {
    const refreshDrilled = () => {
      if (drilledProjectId) {
        void load(drilledProjectId, /* silent */ true);
      }
    };
    const unsubStreamComplete = webSocketManager.onStreamComplete(refreshDrilled);
    const unsubRenamed = webSocketManager.onConversationRenamed((conversationId, customName) => {
      setByProject((prev) => {
        const updated: Record<string, Conversation[]> = {};
        for (const [pid, convos] of Object.entries(prev)) {
          updated[pid] = convos.map((c) =>
            c.id === conversationId ? { ...c, custom_name: customName, title: customName } : c
          );
        }
        return updated;
      });
    });
    const unsubListChanged = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'conversation_list_changed') return;
      refreshDrilled();
    });
    return () => {
      unsubStreamComplete();
      unsubRenamed();
      unsubListChanged();
    };
  }, [drilledProjectId, load]);

  // Reload the drilled project's conversations when its archive filter
  // changes. Deliberately NOT keyed on drilledProjectId: drilling in is
  // handled by the Sidebar (cache hit or an explicit load), and re-running
  // here would flash "Loading..." over a cached list on every drill-down.
  const drilledProjectIdRef = useRef(drilledProjectId);
  useEffect(() => {
    drilledProjectIdRef.current = drilledProjectId;
  }, [drilledProjectId]);
  useEffect(() => {
    const projectId = drilledProjectIdRef.current;
    if (projectId) {
      void load(projectId);
    }
  }, [showArchived, load]);

  // Poll for new project conversations every 30s while drilled down and the
  // tab is visible; also re-fetch when the tab becomes visible again (the
  // user returns after being away).
  useEffect(() => {
    if (!drilledProjectId) return;

    const pollProjectConversations = async () => {
      // Skip if a fetch is already in-flight or the tab is hidden
      if (pollInFlightRef.current || document.hidden) return;

      pollInFlightRef.current = true;
      try {
        const response = await fetchProjectConversations(drilledProjectId, showArchived);
        setByProject((prev) => ({ ...prev, [drilledProjectId]: response.conversations }));
      } catch (err) {
        // Silently ignore poll errors -- the user didn't initiate this request.
        // Network errors or auth failures will surface when the user interacts next.
        console.debug('Poll for project conversations failed:', err);
      } finally {
        pollInFlightRef.current = false;
      }
    };

    const intervalId = setInterval(pollProjectConversations, PROJECT_CONVERSATIONS_POLL_INTERVAL_MS);

    const handleVisibilityChange = () => {
      if (!document.hidden) {
        pollProjectConversations();
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);

    return () => {
      clearInterval(intervalId);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [drilledProjectId, showArchived]);

  const update = useCallback((projectId: string, updater: (prev: Conversation[]) => Conversation[]) => {
    setByProject((prev) => ({ ...prev, [projectId]: updater(prev[projectId] || []) }));
  }, []);

  const seedEmpty = useCallback((projectId: string) => {
    setByProject((prev) => ({ ...prev, [projectId]: [] }));
  }, []);

  return { byProject, loadingByProject, load, update, seedEmpty };
}
