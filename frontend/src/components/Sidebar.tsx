/**
 * Sidebar component for displaying conversation list, projects, and creating new conversations
 */

import React, { useEffect, useState, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { MoreVertical, Search, Inbox, SquarePen } from 'lucide-react';
import {
  fetchConversations,
  duplicateConversationWorkspace,
  fetchProjectConversations,
  createProjectConversation,
  fetchProjectRoutines,
  archiveConversation,
  unarchiveConversation,
  renameConversation,
  fetchActionRequestCounts,
  ApiClientError,
} from '../api/client';
import type { Conversation, Project, Routine } from '../api/types';
import { useConversationContext } from '../contexts/ConversationContext';
import { parseUTCTimestamp } from '../utils/formatters';
import { UserInfoBar } from './UserInfoBar';
import { NewProjectModal } from './NewProjectModal';
import { ConvertToProjectModal } from './ConvertToProjectModal';
import { ProjectSettingsModal } from './ProjectSettingsModal';
import { NewRoutineModal } from './NewRoutineModal';
import { RoutineSettingsModal } from './RoutineSettingsModal';
import { DEPRECATED_MODEL_MAP } from '../constants/models';
import { onRequestCountChange } from '../services/requestEvents';
import { webSocketManager } from '../services/WebSocketManager';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { seedNewConversation } from '../utils/newConversation';
import { useFlipListAnimation } from '../hooks/useFlipListAnimation';
import { SearchModal } from './SearchModal';
import { QuestLogo } from './QuestLogo';
import './Sidebar.css';

/** How often to poll for new project conversations when drilled down (ms). */
const PROJECT_CONVERSATIONS_POLL_INTERVAL_MS = 30_000;

/** Page size for the paged top-level conversation list. */
const CONVERSATIONS_PAGE_SIZE = 30;

// Server-global feature gate key for routines in public projects
// (config/feature_gates.py FEATURE_PUBLIC_PROJECT_ROUTINES).
const PUBLIC_PROJECT_ROUTINES_FEATURE = 'public_project_routines';

// --- Routine conversation grouping types and helpers ---

interface RoutineConversationGroup {
  routineId: string;
  routineName: string;
  conversations: Conversation[];
}

type SidebarItem =
  | { type: 'conversation'; conversation: Conversation }
  | { type: 'routine-group'; group: RoutineConversationGroup };

function groupConversationsByRoutine(
  conversations: Conversation[],
  routines: Routine[],
): {
  ungrouped: Conversation[];
  routineGroups: RoutineConversationGroup[];
} {
  const routineMap = new Map(routines.map(r => [r.id, r]));
  const ungrouped: Conversation[] = [];
  const groupMap = new Map<string, Conversation[]>();

  for (const conv of conversations) {
    if (conv.routine_id && routineMap.has(conv.routine_id)) {
      const existing = groupMap.get(conv.routine_id) || [];
      existing.push(conv);
      groupMap.set(conv.routine_id, existing);
    } else {
      ungrouped.push(conv);
    }
  }

  const routineGroups: RoutineConversationGroup[] = [];
  for (const [routineId, convs] of groupMap) {
    const routine = routineMap.get(routineId)!;
    routineGroups.push({
      routineId,
      routineName: routine.name,
      conversations: convs, // already sorted by last_message_at DESC from API
    });
  }

  // Sort groups by the most recent conversation in each group
  routineGroups.sort((a, b) => {
    const aLatest = a.conversations[0]?.last_message_at || '';
    const bLatest = b.conversations[0]?.last_message_at || '';
    return bLatest.localeCompare(aLatest);
  });

  return { ungrouped, routineGroups };
}

// Apply the sidebar filter toggles to a list of conversations. All three
// filters are enforced server-side via query params on GET /conversations
// (archive via `include_archived`, origins via `include_slack` /
// `include_inference`); this client-side pass is kept so a toggle takes
// effect instantly on the rows already in memory (hiding is immediate; the
// refetch that follows restores a full-length list and reveals newly
// included rows).
function applyConversationFilters(
  conversations: Conversation[],
  showArchived: boolean,
  showSlack: boolean,
  showInference: boolean,
): Conversation[] {
  return conversations.filter((c) => {
    if (!showArchived && c.archived) return false;
    if (!showSlack && c.origin === 'slack') return false;
    if (!showInference && c.origin === 'inference_api') return false;
    return true;
  });
}

function buildSortedSidebarItems(
  ungrouped: Conversation[],
  routineGroups: RoutineConversationGroup[],
): SidebarItem[] {
  const items: SidebarItem[] = [];

  for (const conv of ungrouped) {
    items.push({ type: 'conversation', conversation: conv });
  }
  for (const group of routineGroups) {
    items.push({ type: 'routine-group', group });
  }

  // Sort by most recent activity (descending)
  items.sort((a, b) => {
    const aKey = a.type === 'conversation'
      ? a.conversation.last_message_at
      : a.group.conversations[0]?.last_message_at || '';
    const bKey = b.type === 'conversation'
      ? b.conversation.last_message_at
      : b.group.conversations[0]?.last_message_at || '';
    return bKey.localeCompare(aKey);
  });

  return items;
}

function formatRoutineTimestamp(isoTimestamp: string): string {
  const date = parseUTCTimestamp(isoTimestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffHours = diffMs / (1000 * 60 * 60);
  const diffDays = diffMs / (1000 * 60 * 60 * 24);

  if (diffHours < 1) {
    const diffMins = Math.floor(diffMs / (1000 * 60));
    return `${diffMins}m ago`;
  } else if (diffHours < 24) {
    return date.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  } else if (diffDays < 7) {
    return date.toLocaleDateString(undefined, {
      weekday: 'short',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  } else {
    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  }
}

/**
 * Self-contained badge for the open-action-request count.
 *
 * Initial value comes from a one-shot REST fetch on mount. Subsequent
 * updates arrive via the persistent WS ``request_count_changed`` event,
 * with the local same-tab ``onRequestCountChange`` bus kept as a latency
 * shortcut for clicks in this tab (see open question 5 in 00062).
 */
function RequestsBadge() {
  const [count, setCount] = useState(0);
  const inFlightRef = useRef(false);

  const loadCount = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const resp = await fetchActionRequestCounts();
      setCount(resp.counts.open);
    } catch {
      // Silently ignore fetch errors
    } finally {
      inFlightRef.current = false;
    }
  };

  // One-shot fetch on mount.
  useEffect(() => {
    loadCount();
  }, []);

  // Local same-tab signal (e.g. immediate update after Approve/Deny in this tab).
  useEffect(() => {
    return onRequestCountChange(loadCount);
  }, []);

  // Persistent WS: cross-tab + cross-device updates.
  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'request_count_changed') return;
      const counts = (event.counts as Record<string, number> | undefined) ?? null;
      if (counts && typeof counts.open === 'number') {
        setCount(counts.open);
      } else {
        loadCount();
      }
    });
  }, []);

  if (count <= 0) return null;
  return <span className="requests-badge">{count > 9 ? '9+' : count}</span>;
}

interface SidebarProps {
  activeConversationId: string | null;
  // opts.implicit marks selections that are side effects of drilling in/out of
  // a project (keeping the main pane in sync) rather than the user picking a
  // conversation. The mobile shell keeps its nav drawer open for those.
  onConversationSelect: (id: string, projectId?: string | null, opts?: { implicit?: boolean }) => void;
  onNewConversation: (id: string, projectId?: string | null) => void;
}

/** Small monochrome Slack-style hash mark indicating a Slack-driven conversation. */
function SlackConversationIcon() {
  return (
    <span className="conversation-slack-icon" title="Started from Slack DM">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <line x1="4" y1="9" x2="20" y2="9"></line>
        <line x1="4" y1="15" x2="20" y2="15"></line>
        <line x1="10" y1="3" x2="8" y2="21"></line>
        <line x1="16" y1="3" x2="14" y2="21"></line>
      </svg>
    </span>
  );
}

/** Small monochrome robot glyph indicating a cross-user subagent conversation. */
function SubagentConversationIcon() {
  return (
    <span className="conversation-subagent-icon" title="Subagent run by another user">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M12 8V4H8"></path>
        <rect x="4" y="8" width="16" height="12" rx="2"></rect>
        <path d="M2 14h2"></path>
        <path d="M20 14h2"></path>
        <path d="M15 13v2"></path>
        <path d="M9 13v2"></path>
      </svg>
    </span>
  );
}

/** Small monochrome zap glyph indicating a one-shot inference API run. */
function InferenceConversationIcon() {
  return (
    <span className="conversation-inference-icon" title="One-shot run via the Inference API">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
      </svg>
    </span>
  );
}

/**
 * Sidebar component wrapped in React.memo to prevent re-renders when
 * parent re-renders with the same props. Combined with useCallback on the
 * callback props in App.tsx, this ensures the Sidebar only re-renders
 * when its own props or internal state actually change.
 */
export const Sidebar = React.memo(function Sidebar({ activeConversationId, onConversationSelect, onNewConversation }: SidebarProps) {
  const {
    userEmail,
    userName,
    setSettingsOpen,
    projects,
    projectsLoaded,
    loadProjects,
    activeProjectId,
    setActiveProjectId,
    drilledProjectId,
    setDrilledProjectId,
    setModelForConversation,
    setLoadedSkillsForConversation,
    setPendingRoutineMessage,
    isAuthenticated,
    showRequestsView,
    setShowRequestsView,
    setScrollToMessageIndex,
    updateAvailable,
    appName,
    enabledFeatures,
  } = useConversationContext();
  // Public projects get routines only while an admin has the
  // public_project_routines feature gate open for this user (the routine
  // API and the scheduler enforce the same gate).
  const publicRoutinesEnabled = enabledFeatures.includes(PUBLIC_PROJECT_ROUTINES_FEATURE);
  // The routine modals trim themselves for a public project (public model
  // list, no skill auto-loads or guide override).
  const drilledProjectIsPublic = Boolean(
    projects.find((p) => p.id === drilledProjectId)?.public,
  );

  const navigate = useNavigate();

  // Standalone conversations (paged: `nextCursor` is the keyset cursor for
  // the next older page, null when everything loaded is all there is)
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [loadingMore, setLoadingMore] = useState<boolean>(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState<boolean>(false);

  // Project conversations (keyed by project ID)
  const [projectConversations, setProjectConversations] = useState<Record<string, Conversation[]>>({});
  const [loadingProjectConvos, setLoadingProjectConvos] = useState<Record<string, boolean>>({});
  const [creatingInProject, setCreatingInProject] = useState<string | null>(null);

  // Drill-down state. drilledProjectId lives in ConversationContext (not local
  // state) so the root HomeComposer can target new chats at the drilled
  // project; the slide animation state stays local.
  const [slideDirection, setSlideDirection] = useState<'none' | 'left' | 'right'>('none');

  // Project routines (keyed by project ID)
  const [projectRoutines, setProjectRoutines] = useState<Record<string, Routine[]>>({});
  const [loadingProjectRoutines, setLoadingProjectRoutines] = useState<Record<string, boolean>>({});

  // Routine conversation group expansion state
  const [expandedRoutineGroups, setExpandedRoutineGroups] = useState<Record<string, boolean>>({});

  // Remember the top-level conversation selected before drilling into a project
  const previousTopLevelConversationId = useRef<string | null>(null);

  // Sidebar filter state (archive + Slack origin) and meatball menu state.
  // All four filters default to false (hide archived, hide Slack conversations)
  // on every page load; no persistence. Each conversation section (top-level
  // and per-project) maintains independent filter state.
  const [showArchivedTop, setShowArchivedTop] = useState(false);
  const [showSlackTop, setShowSlackTop] = useState(false);
  const [showInferenceTop, setShowInferenceTop] = useState(false);
  // Refs mirroring the top-level filter toggles and the number of loaded
  // rows. Background refreshes fire from long-lived closures (WS event
  // subscriptions) that would otherwise capture stale toggle state; reading
  // through refs keeps every refetch on the current filters and preserves
  // the user's loaded page window instead of collapsing back to page one.
  const topFiltersRef = useRef({ archived: false, slack: false, inference: false });
  topFiltersRef.current = {
    archived: showArchivedTop,
    slack: showSlackTop,
    inference: showInferenceTop,
  };
  const loadedCountRef = useRef(0);

  // Glide rows to their new slot when a background refresh re-sorts the
  // list (a conversation with new activity jumps to the top). One ref per
  // list: the top-level standalone list and the drilled project list are
  // separate containers.
  const conversationListRef = useFlipListAnimation<HTMLDivElement>();
  const projectConversationListRef = useFlipListAnimation<HTMLDivElement>();

  // Auto-page as the user scrolls: when the "Load more" row scrolls into
  // view (or is still in view after a page lands), fetch the next page.
  // The button stays clickable as a manual fallback. The observer reads
  // the latest loadMoreConversations through a ref because it outlives the
  // render that created it.
  const loadMoreButtonRef = useRef<HTMLButtonElement | null>(null);
  const loadMoreRef = useRef<() => void>(() => {});
  const [showArchivedProject, setShowArchivedProject] = useState(false);
  const [showSlackProject, setShowSlackProject] = useState(false);
  const [filterMenuOpenTop, setFilterMenuOpenTop] = useState(false);
  const [filterMenuOpenProject, setFilterMenuOpenProject] = useState(false);
  const [openMenuConversationId, setOpenMenuConversationId] = useState<string | null>(null);

  // Inline rename state
  const [renamingConversationId, setRenamingConversationId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState<string>('');

  // Search modal state
  const [showSearchModal, setShowSearchModal] = useState(false);

  // Guard to prevent overlapping poll fetches
  const pollInFlightRef = useRef(false);

  // Open action request count badge is managed by the RequestsBadge component below

  // Global keyboard shortcut: Cmd/Ctrl+K opens search modal
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setShowSearchModal(prev => !prev);
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  function toggleRoutineGroup(routineId: string) {
    setExpandedRoutineGroups(prev => ({
      ...prev,
      [routineId]: !prev[routineId],
    }));
  }

  // Modals
  const [showNewProjectModal, setShowNewProjectModal] = useState(false);
  const [convertConversation, setConvertConversation] =
    useState<{ id: string; title: string } | null>(null);
  const [settingsProjectId, setSettingsProjectId] = useState<string | null>(null);
  const [showNewRoutineModal, setShowNewRoutineModal] = useState(false);
  const [settingsRoutine, setSettingsRoutine] = useState<Routine | null>(null);

  // Load standalone conversations on mount and when a filter toggle changes.
  // Filters are enforced server-side, so toggling Slack/Inference visibility
  // needs a refetch (the instant client-side pass only ever hides rows).
  useEffect(() => {
    loadConversations();
  }, [showArchivedTop, showSlackTop, showInferenceTop]);

  // Request count polling is now handled by the RequestsBadge component
  // which manages its own state and polling interval independently

  // When activeProjectId is set externally, auto drill-down to that project
  useEffect(() => {
    if (activeProjectId && activeProjectId !== drilledProjectId) {
      handleProjectDrillDown(activeProjectId);
    }
  }, [activeProjectId]);

  // Shared fetch for the top-level list. `reset` collapses back to the first
  // page (filter toggle changed); otherwise the refetch spans the window the
  // user has already loaded so background refreshes don't shrink the list.
  // `silent` keeps the existing list visible while fetching
  // (stale-while-revalidate) for background refreshes after stream
  // completion, so the sidebar doesn't flash "Loading...".
  async function refreshTopConversations({ reset = false, silent = false } = {}) {
    const filters = topFiltersRef.current;
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
  }

  function loadConversations() {
    return refreshTopConversations({ reset: true });
  }

  function silentLoadConversations() {
    return refreshTopConversations({ silent: true });
  }

  // Fetch the next (older) page and append it, deduped by id. Keyset
  // pagination means conversations created since the last fetch can't shift
  // rows into this page, but a bumped-to-top conversation could still appear
  // twice without the dedupe.
  async function loadMoreConversations() {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const filters = topFiltersRef.current;
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
  }
  loadMoreRef.current = loadMoreConversations;

  // (Re)observe the "Load more" row. Re-running on conversations.length
  // matters: IntersectionObserver only fires on visibility *changes*, so if
  // the row is still on screen after a short page lands, a fresh observe()
  // re-reports the current state and keeps paging until the row leaves the
  // viewport or the cursor runs out.
  useEffect(() => {
    const el = loadMoreButtonRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          loadMoreRef.current();
        }
      },
      // Start fetching a bit before the row actually enters the viewport
      // so fast scrolling doesn't stall on the pager.
      { rootMargin: '150px' }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [nextCursor === null, conversations.length]);

  const loadProjectConversations = useCallback(async (projectId: string, silent: boolean = false): Promise<Conversation[]> => {
    // When silent=true, skip toggling the loading flag so the existing list
    // stays rendered (stale-while-revalidate). Used by new-chat / run-routine
    // flows to avoid a "Loading..." flash after creating a conversation.
    if (!silent) {
      setLoadingProjectConvos((prev) => ({ ...prev, [projectId]: true }));
    }
    try {
      const response = await fetchProjectConversations(projectId, showArchivedProject);
      setProjectConversations((prev) => ({ ...prev, [projectId]: response.conversations }));
      return response.conversations;
    } catch (err) {
      console.error('Failed to load project conversations:', err);
      return [];
    } finally {
      if (!silent) {
        setLoadingProjectConvos((prev) => ({ ...prev, [projectId]: false }));
      }
    }
  }, [showArchivedProject]);

  // Subscribe directly to WebSocket stream-complete events to refresh the
  // sidebar when an LLM response finishes. This replaces the old
  // refreshTrigger prop that polluted the global ConversationContext and
  // caused unnecessary re-renders across the entire component tree.
  useEffect(() => {
    const unsubStreamComplete = webSocketManager.onStreamComplete(() => {
      silentLoadConversations();
      if (drilledProjectId) {
        void loadProjectConversations(drilledProjectId, /* silent */ true);
      }
    });
    const unsubRenamed = webSocketManager.onConversationRenamed((conversationId, customName) => {
      setConversations((prev) =>
        prev.map((c) =>
          c.id === conversationId ? { ...c, custom_name: customName, title: customName } : c
        )
      );
      setProjectConversations((prev) => {
        const updated: Record<string, Conversation[]> = {};
        for (const [pid, convos] of Object.entries(prev)) {
          updated[pid] = convos.map((c) =>
            c.id === conversationId ? { ...c, custom_name: customName, title: customName } : c
          );
        }
        return updated;
      });
    });
    // Persistent WS: re-fetch the sidebar whenever the server advertises a
    // conversation-list mutation (new chat created on first send, new
    // Slack-bot conversation, archive, rename, model change). Cheaper than a
    // poll and immediate. Both lists refresh stale-while-revalidate: the
    // drilled project list must NOT flip its loading flag here, or the whole
    // drill-down panel (routines + rows) is replaced by "Loading..." and
    // re-mounted on every event instead of the new row simply appearing.
    const unsubListChanged = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'conversation_list_changed') return;
      silentLoadConversations();
      if (drilledProjectId) {
        void loadProjectConversations(drilledProjectId, /* silent */ true);
      }
    });
    return () => {
      unsubStreamComplete();
      unsubRenamed();
      unsubListChanged();
    };
  }, [drilledProjectId, loadProjectConversations]);

  const loadProjectRoutines = useCallback(async (projectId: string) => {
    setLoadingProjectRoutines((prev) => ({ ...prev, [projectId]: true }));
    try {
      const response = await fetchProjectRoutines(projectId);
      setProjectRoutines((prev) => ({ ...prev, [projectId]: response.routines }));
    } catch (err) {
      console.error('Failed to load project routines:', err);
    } finally {
      setLoadingProjectRoutines((prev) => ({ ...prev, [projectId]: false }));
    }
  }, []);

  // Persistent WS: re-fetch a project's routines when the server advertises
  // a routine-list mutation (agent-driven create_routine / edit_routine
  // action request approved). The modal flows reload directly via
  // handleRoutineCreated/Updated; this covers writes the sidebar didn't
  // initiate, in this tab or any other.
  useEffect(() => {
    const unsub = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'routine_list_changed') return;
      const projectId = event.project_id as string | undefined;
      if (projectId) {
        loadProjectRoutines(projectId);
      }
    });
    return unsub;
  }, [loadProjectRoutines]);

  // Reload project conversations when the project archive filter changes
  useEffect(() => {
    if (drilledProjectId) {
      loadProjectConversations(drilledProjectId);
    }
  }, [showArchivedProject]);

  // Poll for new project conversations every 30s while drilled down and tab is visible
  useEffect(() => {
    if (!drilledProjectId) return;

    const pollProjectConversations = async () => {
      // Skip if a fetch is already in-flight or the tab is hidden
      if (pollInFlightRef.current || document.hidden) return;

      pollInFlightRef.current = true;
      try {
        const response = await fetchProjectConversations(drilledProjectId, showArchivedProject);
        setProjectConversations((prev) => ({ ...prev, [drilledProjectId]: response.conversations }));
      } catch (err) {
        // Silently ignore poll errors -- the user didn't initiate this request.
        // Network errors or auth failures will surface when the user interacts next.
        console.debug('Poll for project conversations failed:', err);
      } finally {
        pollInFlightRef.current = false;
      }
    };

    const intervalId = setInterval(pollProjectConversations, PROJECT_CONVERSATIONS_POLL_INTERVAL_MS);

    // Also re-fetch when the tab becomes visible again (user returns after being away)
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
  }, [drilledProjectId, showArchivedProject]);

  // Close meatball menu on click outside
  useEffect(() => {
    if (!openMenuConversationId) return;
    const handleClickOutside = () => setOpenMenuConversationId(null);
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, [openMenuConversationId]);

  // Close the top-level filter popover on click outside
  useEffect(() => {
    if (!filterMenuOpenTop) return;
    const handleClickOutside = () => setFilterMenuOpenTop(false);
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, [filterMenuOpenTop]);

  // Close the per-project filter popover on click outside
  useEffect(() => {
    if (!filterMenuOpenProject) return;
    const handleClickOutside = () => setFilterMenuOpenProject(false);
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, [filterMenuOpenProject]);

  async function handleProjectDrillDown(projectId: string) {
    // Save the current top-level conversation so we can restore it on back-nav
    if (!drilledProjectId) {
      previousTopLevelConversationId.current = activeConversationId || null;
    }
    // Load routines if not already loaded
    if (!projectRoutines[projectId]) {
      loadProjectRoutines(projectId);
    }
    setSlideDirection('left');
    setDrilledProjectId(projectId);
    setActiveProjectId(projectId);

    // Determine the project's conversations (use cache or fetch)
    let convos: Conversation[];
    if (projectConversations[projectId]) {
      convos = projectConversations[projectId];
    } else {
      // Only clear current conversation while loading if we don't already
      // have a URL-driven conversationId (i.e., the URL is the source of truth)
      if (!activeConversationId) {
        onConversationSelect('', null, { implicit: true });
      }
      convos = await loadProjectConversations(projectId);
    }

    // If the currently active conversation (e.g. from the URL) belongs to
    // this project, keep it selected instead of clobbering it with the first
    // conversation.  The URL is the source of truth.
    if (activeConversationId && convos.some((c) => c.id === activeConversationId)) {
      onConversationSelect(activeConversationId, projectId, { implicit: true });
    } else if (convos.length > 0) {
      // Auto-select the latest conversation when no specific one is requested
      onConversationSelect(convos[0].id, projectId, { implicit: true });
    } else {
      onConversationSelect('', null, { implicit: true });
    }
  }

  function handleDrillDownBack() {
    setSlideDirection('right');
    // Restore the top-level conversation that was active before drilling in,
    // or show the empty state if none was selected
    const savedId = previousTopLevelConversationId.current;
    previousTopLevelConversationId.current = null;
    onConversationSelect(savedId ?? '', null, { implicit: true });
    // Use a short timeout to let the animation play before clearing state
    setTimeout(() => {
      setDrilledProjectId(null);
      setSlideDirection('none');
    }, 200); // match CSS transition duration
  }

  // Seed the conversation store and adjacent caches for a chat we just
  // created on this tab (see utils/newConversation.ts for the rationale).
  // Shared with the root HomeComposer so both new-chat paths seed identically.
  function seedConversation(conversationId: string) {
    seedNewConversation(conversationId, setLoadedSkillsForConversation);
  }

  // "New Chat" (top-level Conversations "+", and the brand bar when not
  // drilled): nothing is created here. Navigate to the root home composer,
  // which creates the conversation as a response to the FIRST message (see
  // HomeComposer.tsx). Only reachable while not drilled into a project, so
  // the home composer targets a standalone chat.
  function handleNewChat() {
    setError(null);
    setShowRequestsView(false);
    setActiveProjectId(null);
    onNewConversation('', null);
  }

  // Brand-bar "new chat" pencil. While drilled into a project it must target
  // that project (same path as the drill-down panel's "+"), otherwise the
  // user lands in a standalone chat that isn't in the list they're looking at.
  function handleBrandBarNewChat() {
    if (drilledProjectId) {
      handleNewProjectChat(drilledProjectId);
    } else {
      handleNewChat();
    }
  }

  async function handleDuplicateWorkspace(sourceConversationId: string) {
    try {
      setCreating(true);
      setError(null);
      setShowRequestsView(false);
      const response = await duplicateConversationWorkspace(sourceConversationId);
      // Same optimistic seed + fire-and-forget refresh as handleNewChat: the
      // new conversation is empty, only its workspace files differ.
      seedConversation(response.id);
      void silentLoadConversations();
      setActiveProjectId(null);
      onNewConversation(response.id, null);
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to duplicate workspace');
      }
    } finally {
      setCreating(false);
    }
  }

  // Project "+" New Chat: same deferred-create flow as handleNewChat, but the
  // Sidebar stays drilled into the project so the home composer creates the
  // chat INSIDE it on first send ("New chat in <project>" hint).
  function handleNewProjectChat(projectId: string) {
    setError(null);
    setShowRequestsView(false);
    setActiveProjectId(projectId);
    onNewConversation('', projectId);
  }

  function handleConversationClick(id: string, projectId?: string | null) {
    setShowRequestsView(false);
    if (projectId) {
      setActiveProjectId(projectId);
    } else {
      setActiveProjectId(null);
    }
    onConversationSelect(id, projectId);
  }

  async function handleRunRoutine(projectId: string, routine: Routine) {
    try {
      setCreatingInProject(projectId);
      setError(null);

      // 1. Create a new conversation in the project, linked to the routine
      const response = await createProjectConversation(projectId, routine.id);
      seedConversation(response.id);
      // Fire-and-forget refresh of the project conversation list.
      void loadProjectConversations(projectId, /* silent */ true);
      setActiveProjectId(projectId);

      // 2. Select the conversation and notify parent
      onNewConversation(response.id, projectId);

      // 3. Set the model for this conversation if the routine specifies one
      if (routine.model) {
        const effectiveModel = DEPRECATED_MODEL_MAP[routine.model] || routine.model;
        setModelForConversation(response.id, effectiveModel);
      }

      // 4. Auto-expand the routine group so the new conversation is visible
      setExpandedRoutineGroups(prev => ({ ...prev, [routine.id]: true }));

      // 5. Set the pending routine message so ChatPanel auto-sends the prompt
      setPendingRoutineMessage({
        conversationId: response.id,
        prompt: routine.prompt,
        guideId: routine.guide_id,
      });
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to run routine');
      }
    } finally {
      setCreatingInProject(null);
    }
  }

  function handleArchiveConversation(conversationId: string, projectId?: string | null) {
    setOpenMenuConversationId(null);

    // Optimistic local state update (avoids full reload flicker)
    if (projectId && drilledProjectId) {
      setProjectConversations((prev) => {
        const list = prev[drilledProjectId] || [];
        if (showArchivedProject) {
          // Toggle flag in-place so the item stays visible but shows as archived
          return { ...prev, [drilledProjectId]: list.map((c) =>
            c.id === conversationId ? { ...c, archived: true } : c
          )};
        }
        // Remove from list when archived items are hidden
        return { ...prev, [drilledProjectId]: list.filter((c) => c.id !== conversationId) };
      });
    } else {
      setConversations((prev) => {
        if (showArchivedTop) {
          return prev.map((c) =>
            c.id === conversationId ? { ...c, archived: true } : c
          );
        }
        return prev.filter((c) => c.id !== conversationId);
      });
    }

    // Deselect if the archived conversation was active
    if (activeConversationId === conversationId) {
      onConversationSelect('', null);
    }

    // Fire-and-forget API call to persist on the server
    archiveConversation(conversationId).catch((err) => {
      console.error('Failed to archive conversation:', err);
    });
  }

  function handleUnarchiveConversation(conversationId: string, projectId?: string | null) {
    setOpenMenuConversationId(null);

    // Optimistic local state update
    if (projectId && drilledProjectId) {
      setProjectConversations((prev) => {
        const list = prev[drilledProjectId] || [];
        return { ...prev, [drilledProjectId]: list.map((c) =>
          c.id === conversationId ? { ...c, archived: false } : c
        )};
      });
    } else {
      setConversations((prev) =>
        prev.map((c) =>
          c.id === conversationId ? { ...c, archived: false } : c
        )
      );
    }

    // Fire-and-forget API call to persist on the server
    unarchiveConversation(conversationId).catch((err) => {
      console.error('Failed to unarchive conversation:', err);
    });
  }

  function handleStartRename(conversationId: string, currentName: string) {
    setOpenMenuConversationId(null);
    setRenamingConversationId(conversationId);
    // Pre-fill with empty string for placeholder titles like "New Chat"
    setRenameValue(currentName === 'New Chat' ? '' : currentName);
  }

  function handleRenameSubmit(conversationId: string, projectId?: string | null) {
    const trimmed = renameValue.trim();
    const newName = trimmed || null; // empty string = clear custom name

    // Optimistic UI update
    if (projectId && drilledProjectId) {
      setProjectConversations((prev) => {
        const list = prev[drilledProjectId] || [];
        return {
          ...prev,
          [drilledProjectId]: list.map((c) =>
            c.id === conversationId
              ? { ...c, custom_name: newName, title: newName || c.title }
              : c
          ),
        };
      });
    } else {
      setConversations((prev) =>
        prev.map((c) =>
          c.id === conversationId
            ? { ...c, custom_name: newName, title: newName || c.title }
            : c
        )
      );
    }

    setRenamingConversationId(null);
    setRenameValue('');

    // Fire-and-forget API call
    renameConversation(conversationId, newName).catch((err) => {
      console.error('Failed to rename conversation:', err);
    });
  }

  function handleRenameCancel() {
    setRenamingConversationId(null);
    setRenameValue('');
  }

  function handleProjectCreated(projectId: string) {
    loadProjects();
    // Initialize conversations for the new project and drill down into it
    setProjectConversations((prev) => ({ ...prev, [projectId]: [] }));
    handleProjectDrillDown(projectId);
  }

  function handleConvertedToProject(projectId: string, conversationId: string) {
    loadProjects();
    // The conversation now belongs to the project; drop it from the
    // top-level list immediately rather than waiting for the WS refetch.
    setConversations((prev) => prev.filter((c) => c.id !== conversationId));
    // Drill into the new project. The conversation list isn't cached yet, so
    // the drill-down fetches it fresh and auto-selects the moved conversation
    // (it's the only one in the project).
    handleProjectDrillDown(projectId);
  }

  function handleProjectUpdated() {
    loadProjects();
    // Reload routines for the current project in case they were added/edited/deleted
    if (drilledProjectId) {
      loadProjectRoutines(drilledProjectId);
    }
  }

  function handleProjectDeleted() {
    loadProjects();
    if (settingsProjectId === drilledProjectId) {
      handleDrillDownBack();
    }
    if (settingsProjectId === activeProjectId) {
      setActiveProjectId(null);
    }
    setSettingsProjectId(null);
  }

  function handleRoutineCreated() {
    if (drilledProjectId) {
      loadProjectRoutines(drilledProjectId);
    }
  }

  function handleRoutineUpdated() {
    if (drilledProjectId) {
      loadProjectRoutines(drilledProjectId);
    }
  }

  function handleRoutineDeleted() {
    if (drilledProjectId) {
      loadProjectRoutines(drilledProjectId);
    }
  }

  return (
    <div className="sidebar">
      {error && (
        <div className="sidebar-error">
          {error}
        </div>
      )}

      <div className="sidebar-content">
        {/* Brand bar: logo + app name on the left, new chat + search + requests inbox on the right */}
        <div className="sidebar-brand">
          <div className="sidebar-brand-identity">
            <QuestLogo className="sidebar-brand-logo" />
            <span className="sidebar-brand-name">{appName}</span>
          </div>
          <div className="sidebar-brand-actions">
            <button
              className="sidebar-icon-button"
              onClick={handleBrandBarNewChat}
              disabled={creating || (drilledProjectId !== null && creatingInProject === drilledProjectId)}
              title={drilledProjectId ? 'New chat in this project' : 'New Chat'}
              aria-label={drilledProjectId ? 'New chat in this project' : 'New Chat'}
            >
              <SquarePen size={18} />
            </button>
            <button
              className="sidebar-icon-button"
              onClick={() => setShowSearchModal(true)}
              title="Search conversations"
              aria-label="Search conversations"
            >
              <Search size={18} />
            </button>
            <button
              className={`sidebar-icon-button${showRequestsView ? ' active' : ''}`}
              onClick={() => {
                // /inbox is the view's deep-linkable URL; the route->state
                // sync in App.tsx flips showRequestsView. Set it here too so
                // the view opens without waiting for the navigation effect.
                setShowRequestsView(true);
                navigate('/inbox');
              }}
              title="Requests"
              aria-label="Open requests"
            >
              <Inbox size={18} />
              <RequestsBadge />
            </button>
          </div>
        </div>

        <div className={`sidebar-panels ${drilledProjectId ? 'drilled' : ''} slide-${slideDirection}`}>
          {/* Main panel: projects + conversations */}
          <div className="sidebar-panel sidebar-panel-main">
            {/* Projects section */}
            {projectsLoaded && (
              <div className="projects-section">
                <div className="section-header">
                  <div className="section-label">Projects</div>
                  {projects.length > 0 && (
                    <button
                      className="section-add-button"
                      onClick={() => setShowNewProjectModal(true)}
                      title="New Project"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <line x1="12" y1="5" x2="12" y2="19"></line>
                        <line x1="5" y1="12" x2="19" y2="12"></line>
                      </svg>
                    </button>
                  )}
                </div>
                {projects.length === 0 ? (
                  <button
                    className="create-project-button"
                    onClick={() => setShowNewProjectModal(true)}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                      <line x1="12" y1="11" x2="12" y2="17"></line>
                      <line x1="9" y1="14" x2="15" y2="14"></line>
                    </svg>
                    Create Project
                  </button>
                ) : (
                  projects.map((project: Project) => (
                    <div key={project.id} className="project-group">
                      <div
                        className="project-header"
                        onClick={() => handleProjectDrillDown(project.id)}
                      >
                        {/* Folder icon (outline SVG) */}
                        <svg
                          className="project-folder-icon"
                          width="16"
                          height="16"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                        </svg>
                        <span className="project-name">{project.name}</span>
                        {project.public && (
                          /* Globe badge: public project (internet sandbox,
                             no internal data access) */
                          <svg
                            className="project-public-badge"
                            width="12"
                            height="12"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                          >
                            <title>Public project — internet access, no internal data</title>
                            <circle cx="12" cy="12" r="10"></circle>
                            <line x1="2" y1="12" x2="22" y2="12"></line>
                            <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
                          </svg>
                        )}
                        {/* Right chevron indicating navigation */}
                        <svg
                          className="project-nav-chevron"
                          width="14"
                          height="14"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <polyline points="9 18 15 12 9 6"></polyline>
                        </svg>
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}

            {/* Standalone conversations section */}
            {loading ? (
              <div className="sidebar-loading">
                Loading conversations...
              </div>
            ) : (
              <>
                <div className="section-header">
                  <div className="section-label">Conversations</div>
                  <div className="section-header-actions">
                    <button
                      className="section-add-button"
                      onClick={handleNewChat}
                      disabled={creating}
                      title="New Chat"
                      aria-label="New Chat"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <line x1="12" y1="5" x2="12" y2="19"></line>
                      <line x1="5" y1="12" x2="19" y2="12"></line>
                    </svg>
                    </button>
                  <div className="conversation-filter-wrapper">
                    <button
                      className="conversation-filter-button"
                      onClick={(e) => {
                        e.stopPropagation();
                        setFilterMenuOpenTop((prev) => !prev);
                      }}
                      title="Conversation options"
                      aria-label="Conversation options"
                    >
                      <MoreVertical size={14} />
                    </button>
                    {filterMenuOpenTop && (
                      <div
                        className="conversation-filter-menu"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <label className="conversation-filter-menu-item">
                          <input
                            type="checkbox"
                            checked={showArchivedTop}
                            onChange={(e) => setShowArchivedTop(e.target.checked)}
                          />
                          <span>Show Archived</span>
                        </label>
                        <label className="conversation-filter-menu-item">
                          <input
                            type="checkbox"
                            checked={showSlackTop}
                            onChange={(e) => setShowSlackTop(e.target.checked)}
                          />
                          <span>Show Slack Conversations</span>
                        </label>
                        <label className="conversation-filter-menu-item">
                          <input
                            type="checkbox"
                            checked={showInferenceTop}
                            onChange={(e) => setShowInferenceTop(e.target.checked)}
                          />
                          <span>Show Inference API Runs</span>
                        </label>
                      </div>
                    )}
                  </div>
                  </div>
                </div>
                <div className="conversation-list" ref={conversationListRef}>
                  {applyConversationFilters(conversations, showArchivedTop, showSlackTop, showInferenceTop).map((conversation) => (
                    <div
                      key={conversation.id}
                      data-flip-id={conversation.id}
                      className={`conversation-item ${!showRequestsView && activeConversationId === conversation.id ? 'active' : ''} ${conversation.archived ? 'archived' : ''}`}
                      onClick={() => handleConversationClick(conversation.id, null)}
                    >
                      {renamingConversationId === conversation.id ? (
                        <input
                          className="conversation-rename-input"
                          type="text"
                          value={renameValue}
                          onChange={(e) => setRenameValue(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              handleRenameSubmit(conversation.id, null);
                            } else if (e.key === 'Escape') {
                              handleRenameCancel();
                            }
                          }}
                          onBlur={() => handleRenameSubmit(conversation.id, null)}
                          autoFocus
                          onFocus={(e) => e.target.select()}
                          maxLength={100}
                          onClick={(e) => e.stopPropagation()}
                        />
                      ) : (
                        <div className="conversation-title">
                          {conversation.origin === 'slack' && <SlackConversationIcon />}
                          {conversation.origin === 'user_subagent' && <SubagentConversationIcon />}
                          {conversation.origin === 'inference_api' && <InferenceConversationIcon />}
                          {conversation.custom_name || conversation.title}
                        </div>
                      )}
                      <button
                        className="conversation-menu-button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpenMenuConversationId(
                            openMenuConversationId === conversation.id ? null : conversation.id
                          );
                        }}
                        title="More options"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                          <circle cx="12" cy="5" r="2"></circle>
                          <circle cx="12" cy="12" r="2"></circle>
                          <circle cx="12" cy="19" r="2"></circle>
                        </svg>
                      </button>
                      {openMenuConversationId === conversation.id && (
                        <div className="conversation-menu-dropdown">
                          <button
                            className="conversation-menu-item"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleStartRename(conversation.id, conversation.custom_name || conversation.title);
                            }}
                          >
                            Rename
                          </button>
                          {conversation.origin !== 'slack' && conversation.origin !== 'user_subagent' && conversation.origin !== 'inference_api' && (
                            <button
                              className="conversation-menu-item"
                              onClick={(e) => {
                                e.stopPropagation();
                                setOpenMenuConversationId(null);
                                setConvertConversation({
                                  id: conversation.id,
                                  title: conversation.custom_name || conversation.title,
                                });
                              }}
                            >
                              Create Project from Chat
                            </button>
                          )}
                          <button
                            className="conversation-menu-item"
                            onClick={(e) => {
                              e.stopPropagation();
                              setOpenMenuConversationId(null);
                              void handleDuplicateWorkspace(conversation.id);
                            }}
                          >
                            Duplicate Workspace
                          </button>
                          {conversation.archived ? (
                            <button
                              className="conversation-menu-item"
                              onClick={(e) => {
                                e.stopPropagation();
                                handleUnarchiveConversation(conversation.id, null);
                              }}
                            >
                              Unarchive
                            </button>
                          ) : (
                            <button
                              className="conversation-menu-item"
                              onClick={(e) => {
                                e.stopPropagation();
                                handleArchiveConversation(conversation.id, null);
                              }}
                            >
                              Archive
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  ))}
                  {nextCursor && (
                    <button
                      ref={loadMoreButtonRef}
                      className="load-more-conversations-button"
                      onClick={loadMoreConversations}
                      disabled={loadingMore}
                    >
                      {loadingMore ? 'Loading...' : 'Load more'}
                    </button>
                  )}
                </div>
              </>
            )}
          </div>

          {/* Drill-down panel: project conversations */}
          <div className="sidebar-panel sidebar-panel-project">
            {drilledProjectId && (() => {
              const project = projects.find(p => p.id === drilledProjectId);
              if (!project) return null;
              return (
                <>
                  <div className="drill-down-header">
                    <button className="drill-down-back-button" onClick={handleDrillDownBack}>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="15 18 9 12 15 6"></polyline>
                      </svg>
                    </button>
                    <svg
                      className="drill-down-folder-icon"
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                    </svg>
                    <span className="drill-down-project-name">{project.name}</span>
                    {project.public && (
                      <span className="drill-down-public-badge" title="Public project — internet access, no internal data">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <circle cx="12" cy="12" r="10"></circle>
                          <line x1="2" y1="12" x2="22" y2="12"></line>
                          <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
                        </svg>
                        Public
                      </span>
                    )}
                    <button
                      className="drill-down-settings-button"
                      onClick={() => setSettingsProjectId(project.id)}
                      title="Project settings"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <circle cx="12" cy="12" r="3"></circle>
                        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
                      </svg>
                    </button>
                  </div>

                  <div className="drill-down-conversations">
                    {loadingProjectConvos[drilledProjectId] ? (
                      <div className="project-loading">Loading...</div>
                    ) : (
                      <>
                        {/* Routines section -- hidden for public projects
                            unless the public_project_routines feature gate
                            is open (the API rejects routines there otherwise) */}
                        {(!project.public || publicRoutinesEnabled) && (
                        <div className="routines-section">
                          <div className="section-header">
                            <div className="section-label">Routines</div>
                            <button
                              className="section-add-button"
                              onClick={() => setShowNewRoutineModal(true)}
                              title="New Routine"
                              aria-label="New Routine"
                            >
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <line x1="12" y1="5" x2="12" y2="19"></line>
                                <line x1="5" y1="12" x2="19" y2="12"></line>
                              </svg>
                            </button>
                          </div>
                          {(projectRoutines[drilledProjectId] || []).map((routine) => (
                              <div key={routine.id} className="routine-item">
                                <span className="routine-name">{routine.name}</span>
                                {routine.schedule?.is_enabled && (
                                  <span className="routine-schedule-indicator" title="Scheduled">
                                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                      <circle cx="12" cy="12" r="10"></circle>
                                      <polyline points="12 6 12 12 16 14"></polyline>
                                    </svg>
                                  </span>
                                )}
                                <button
                                  className="routine-settings-button"
                                  onClick={() => setSettingsRoutine(routine)}
                                  title={`Settings for "${routine.name}"`}
                                >
                                  {/* Gear icon */}
                                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                    <circle cx="12" cy="12" r="3"></circle>
                                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
                                  </svg>
                                </button>
                                <button
                                  className="routine-play-button"
                                  onClick={() => handleRunRoutine(drilledProjectId, routine)}
                                  title={`Run "${routine.name}"`}
                                  disabled={creatingInProject === drilledProjectId}
                                >
                                  <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none">
                                    <polygon points="5,3 19,12 5,21"></polygon>
                                  </svg>
                                </button>
                              </div>
                          ))}
                        </div>
                        )}

                        {/* Conversations section header */}
                        <div className="section-header">
                          <div className="section-label">Conversations</div>
                          <div className="section-header-actions">
                            <button
                              className="section-add-button"
                              onClick={() => handleNewProjectChat(drilledProjectId)}
                              disabled={creatingInProject === drilledProjectId}
                              title="New Chat"
                              aria-label="New Chat"
                            >
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <line x1="12" y1="5" x2="12" y2="19"></line>
                                <line x1="5" y1="12" x2="19" y2="12"></line>
                              </svg>
                            </button>
                          <div className="conversation-filter-wrapper">
                            <button
                              className="conversation-filter-button"
                              onClick={(e) => {
                                e.stopPropagation();
                                setFilterMenuOpenProject((prev) => !prev);
                              }}
                              title="Conversation options"
                              aria-label="Conversation options"
                            >
                              <MoreVertical size={14} />
                            </button>
                            {filterMenuOpenProject && (
                              <div
                                className="conversation-filter-menu"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <label className="conversation-filter-menu-item">
                                  <input
                                    type="checkbox"
                                    checked={showArchivedProject}
                                    onChange={(e) => setShowArchivedProject(e.target.checked)}
                                  />
                                  <span>Show Archived</span>
                                </label>
                                <label className="conversation-filter-menu-item">
                                  <input
                                    type="checkbox"
                                    checked={showSlackProject}
                                    onChange={(e) => setShowSlackProject(e.target.checked)}
                                  />
                                  <span>Show Slack Conversations</span>
                                </label>
                              </div>
                            )}
                          </div>
                          </div>
                        </div>

                        {/* Grouped conversations: routine groups interleaved with ungrouped conversations */}
                        {(() => {
                          const convos = applyConversationFilters(
                            projectConversations[drilledProjectId] || [],
                            showArchivedProject,
                            showSlackProject,
                            // Inference API runs are never project conversations,
                            // so the project view has no toggle for them.
                            false,
                          );
                          const routines = projectRoutines[drilledProjectId] || [];
                          const { ungrouped, routineGroups } = groupConversationsByRoutine(convos, routines);
                          const sortedItems = buildSortedSidebarItems(ungrouped, routineGroups);

                          return (
                            <div className="conversation-list" ref={projectConversationListRef}>
                              {sortedItems.map((item) => {
                                if (item.type === 'routine-group') {
                                  const group = item.group;
                                  const isExpanded = expandedRoutineGroups[group.routineId] || false;
                                  const hasActiveConvo = group.conversations.some(c => c.id === activeConversationId);

                                  return (
                                    <div
                                      key={`routine-group-${group.routineId}`}
                                      data-flip-id={`routine-group-${group.routineId}`}
                                      className="routine-conversation-group"
                                    >
                                      <div
                                        className={`routine-group-header ${hasActiveConvo && !isExpanded ? 'has-active' : ''}`}
                                        onClick={() => toggleRoutineGroup(group.routineId)}
                                      >
                                        <svg className="routine-group-chevron" width="12" height="12" viewBox="0 0 24 24"
                                             fill="none" stroke="currentColor" strokeWidth="2"
                                             style={{ transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)' }}>
                                          <polyline points="9 18 15 12 9 6"></polyline>
                                        </svg>
                                        <svg className="routine-group-icon" width="14" height="14" viewBox="0 0 24 24"
                                             fill="none" stroke="currentColor" strokeWidth="2"
                                             strokeLinecap="round" strokeLinejoin="round">
                                          <polyline points="23 4 23 10 17 10"></polyline>
                                          <polyline points="1 20 1 14 7 14"></polyline>
                                          <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
                                        </svg>
                                        <span className="routine-group-name">{group.routineName}</span>
                                        <span className="routine-group-count">{group.conversations.length}</span>
                                      </div>
                                      {isExpanded && (
                                        <div className="routine-group-items">
                                          {group.conversations.map((conversation) => (
                                            <div
                                              key={conversation.id}
                                              className={`conversation-item routine-sub-item ${!showRequestsView && activeConversationId === conversation.id ? 'active' : ''} ${conversation.archived ? 'archived' : ''}`}
                                              onClick={() => handleConversationClick(conversation.id, drilledProjectId)}
                                            >
                                              {renamingConversationId === conversation.id ? (
                                                <input
                                                  className="conversation-rename-input"
                                                  type="text"
                                                  value={renameValue}
                                                  onChange={(e) => setRenameValue(e.target.value)}
                                                  onKeyDown={(e) => {
                                                    if (e.key === 'Enter') {
                                                      handleRenameSubmit(conversation.id, drilledProjectId);
                                                    } else if (e.key === 'Escape') {
                                                      handleRenameCancel();
                                                    }
                                                  }}
                                                  onBlur={() => handleRenameSubmit(conversation.id, drilledProjectId)}
                                                  autoFocus
                                                  maxLength={100}
                                                  onClick={(e) => e.stopPropagation()}
                                                />
                                              ) : (
                                                <div className="conversation-title routine-sub-item-timestamp">
                                                  {conversation.origin === 'slack' && <SlackConversationIcon />}
                                                  {conversation.origin === 'user_subagent' && <SubagentConversationIcon />}
                                                  {conversation.origin === 'inference_api' && <InferenceConversationIcon />}
                                                  {conversation.custom_name
                                                    ? `${conversation.custom_name} (${formatRoutineTimestamp(conversation.created_at)})`
                                                    : formatRoutineTimestamp(conversation.created_at)}
                                                </div>
                                              )}
                                              <button
                                                className="conversation-menu-button"
                                                onClick={(e) => {
                                                  e.stopPropagation();
                                                  setOpenMenuConversationId(
                                                    openMenuConversationId === conversation.id ? null : conversation.id
                                                  );
                                                }}
                                                title="More options"
                                              >
                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                                                  <circle cx="12" cy="5" r="2"></circle>
                                                  <circle cx="12" cy="12" r="2"></circle>
                                                  <circle cx="12" cy="19" r="2"></circle>
                                                </svg>
                                              </button>
                                              {openMenuConversationId === conversation.id && (
                                                <div className="conversation-menu-dropdown">
                                                  <button
                                                    className="conversation-menu-item"
                                                    onClick={(e) => {
                                                      e.stopPropagation();
                                                      handleStartRename(
                                                        conversation.id,
                                                        conversation.custom_name || '',
                                                      );
                                                    }}
                                                  >
                                                    Rename
                                                  </button>
                                                  {conversation.archived ? (
                                                    <button
                                                      className="conversation-menu-item"
                                                      onClick={(e) => {
                                                        e.stopPropagation();
                                                        handleUnarchiveConversation(conversation.id, drilledProjectId);
                                                      }}
                                                    >
                                                      Unarchive
                                                    </button>
                                                  ) : (
                                                    <button
                                                      className="conversation-menu-item"
                                                      onClick={(e) => {
                                                        e.stopPropagation();
                                                        handleArchiveConversation(conversation.id, drilledProjectId);
                                                      }}
                                                    >
                                                      Archive
                                                    </button>
                                                  )}
                                                </div>
                                              )}
                                            </div>
                                          ))}
                                        </div>
                                      )}
                                    </div>
                                  );
                                } else {
                                  const conversation = item.conversation;
                                  return (
                                    <div
                                      key={conversation.id}
                                      data-flip-id={conversation.id}
                                      className={`conversation-item ${!showRequestsView && activeConversationId === conversation.id ? 'active' : ''} ${conversation.archived ? 'archived' : ''}`}
                                      onClick={() => handleConversationClick(conversation.id, drilledProjectId)}
                                    >
                                      {renamingConversationId === conversation.id ? (
                                        <input
                                          className="conversation-rename-input"
                                          type="text"
                                          value={renameValue}
                                          onChange={(e) => setRenameValue(e.target.value)}
                                          onKeyDown={(e) => {
                                            if (e.key === 'Enter') {
                                              handleRenameSubmit(conversation.id, drilledProjectId);
                                            } else if (e.key === 'Escape') {
                                              handleRenameCancel();
                                            }
                                          }}
                                          onBlur={() => handleRenameSubmit(conversation.id, drilledProjectId)}
                                          autoFocus
                                          maxLength={100}
                                          onClick={(e) => e.stopPropagation()}
                                        />
                                      ) : (
                                        <div className="conversation-title">
                                          {conversation.origin === 'slack' && <SlackConversationIcon />}
                                          {conversation.origin === 'user_subagent' && <SubagentConversationIcon />}
                                          {conversation.origin === 'inference_api' && <InferenceConversationIcon />}
                                          {conversation.custom_name || conversation.title}
                                        </div>
                                      )}
                                      <button
                                        className="conversation-menu-button"
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          setOpenMenuConversationId(
                                            openMenuConversationId === conversation.id ? null : conversation.id
                                          );
                                        }}
                                        title="More options"
                                      >
                                        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                                          <circle cx="12" cy="5" r="2"></circle>
                                          <circle cx="12" cy="12" r="2"></circle>
                                          <circle cx="12" cy="19" r="2"></circle>
                                        </svg>
                                      </button>
                                      {openMenuConversationId === conversation.id && (
                                        <div className="conversation-menu-dropdown">
                                          <button
                                            className="conversation-menu-item"
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              handleStartRename(conversation.id, conversation.custom_name || conversation.title);
                                            }}
                                          >
                                            Rename
                                          </button>
                                          {conversation.archived ? (
                                            <button
                                              className="conversation-menu-item"
                                              onClick={(e) => {
                                                e.stopPropagation();
                                                handleUnarchiveConversation(conversation.id, drilledProjectId);
                                              }}
                                            >
                                              Unarchive
                                            </button>
                                          ) : (
                                            <button
                                              className="conversation-menu-item"
                                              onClick={(e) => {
                                                e.stopPropagation();
                                                handleArchiveConversation(conversation.id, drilledProjectId);
                                              }}
                                            >
                                              Archive
                                            </button>
                                          )}
                                        </div>
                                      )}
                                    </div>
                                  );
                                }
                              })}
                            </div>
                          );
                        })()}

                        {(projectConversations[drilledProjectId] || []).length === 0 && !loadingProjectConvos[drilledProjectId] && (
                          <div className="sidebar-empty">No conversations yet</div>
                        )}
                      </>
                    )}
                  </div>
                </>
              );
            })()}
          </div>
        </div>
      </div>

      {updateAvailable && (
        <div className="version-update-banner">
          Quest has updated. Please <a href="#" onClick={(e) => { e.preventDefault(); window.location.reload(); }}>reload</a> ASAP!
        </div>
      )}

      <UserInfoBar
        email={userEmail}
        name={userName}
        onSettingsClick={() => setSettingsOpen(true)}
      />

      <NewProjectModal
        isOpen={showNewProjectModal}
        onClose={() => setShowNewProjectModal(false)}
        onProjectCreated={handleProjectCreated}
      />

      <ConvertToProjectModal
        isOpen={convertConversation !== null}
        conversationId={convertConversation?.id ?? ''}
        conversationTitle={convertConversation?.title ?? ''}
        onClose={() => setConvertConversation(null)}
        onConverted={handleConvertedToProject}
      />

      <ProjectSettingsModal
        isOpen={settingsProjectId !== null}
        projectId={settingsProjectId}
        onClose={() => setSettingsProjectId(null)}
        onProjectUpdated={handleProjectUpdated}
        onProjectDeleted={handleProjectDeleted}
      />

      <NewRoutineModal
        isOpen={showNewRoutineModal}
        projectId={drilledProjectId}
        isPublicProject={drilledProjectIsPublic}
        onClose={() => setShowNewRoutineModal(false)}
        onRoutineCreated={handleRoutineCreated}
      />

      <RoutineSettingsModal
        isOpen={settingsRoutine !== null}
        projectId={drilledProjectId}
        isPublicProject={drilledProjectIsPublic}
        routine={settingsRoutine}
        onClose={() => setSettingsRoutine(null)}
        onRoutineUpdated={handleRoutineUpdated}
        onRoutineDeleted={handleRoutineDeleted}
        onOpenRunConversation={(conversationId) => {
          setSettingsRoutine(null);
          onConversationSelect(conversationId, drilledProjectId);
        }}
      />

      <SearchModal
        isOpen={showSearchModal}
        onClose={() => setShowSearchModal(false)}
        onNavigate={(conversationId, projectId, messageIndex) => {
          setShowSearchModal(false);
          setShowRequestsView(false);
          onConversationSelect(conversationId, projectId);
          setScrollToMessageIndex(messageIndex);
        }}
      />
    </div>
  );
});
