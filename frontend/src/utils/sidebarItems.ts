/**
 * Pure list derivation for the Sidebar: filter toggles, routine-run grouping
 * and the interleaved most-recent-first ordering of a project's conversation
 * list. No React, no fetching -- unit-testable on plain data.
 */

import type { Conversation, Routine } from '../api/types';
import { parseUTCTimestamp } from './formatters';

export interface RoutineConversationGroup {
  routineId: string;
  routineName: string;
  conversations: Conversation[];
}

export type SidebarItem =
  | { type: 'conversation'; conversation: Conversation }
  | { type: 'routine-group'; group: RoutineConversationGroup };

export function groupConversationsByRoutine(
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
export function applyConversationFilters(
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

export function buildSortedSidebarItems(
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

/**
 * The drilled project list as rendered: filter toggles applied, routine runs
 * folded into one group per routine, groups interleaved with standalone
 * conversations by most recent activity. Inference API runs are never
 * project conversations, so the project view has no toggle for them.
 */
export function deriveProjectSidebarItems(
  conversations: Conversation[],
  routines: Routine[],
  filters: { showArchived: boolean; showSlack: boolean },
): SidebarItem[] {
  const visible = applyConversationFilters(conversations, filters.showArchived, filters.showSlack, false);
  const { ungrouped, routineGroups } = groupConversationsByRoutine(visible, routines);
  return buildSortedSidebarItems(ungrouped, routineGroups);
}

export function formatRoutineTimestamp(isoTimestamp: string): string {
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
