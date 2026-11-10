/**
 * Pure derivation for the Sidebar's Tasks block: which unarchived tasks it
 * lists and in what order. No React, no fetching -- unit-testable on plain
 * data, like utils/sidebarDocs.ts.
 */

import type { TaskListRow } from '../api/types';
import { parseUTCTimestamp } from './formatters';

/** How many tasks the sidebar Tasks block lists (the docs / tables limit). */
export const SIDEBAR_TASK_LIMIT = 5;

/** Epoch ms of a last-activity time, or null when there is none / it is unparseable. */
function activityTime(timestamp: string | null): number | null {
  if (!timestamp) return null;
  const ms = parseUTCTimestamp(timestamp).getTime();
  return Number.isNaN(ms) ? null : ms;
}

/**
 * The rows the sidebar Tasks block shows: newest `last_activity_at` first,
 * tasks without activity last, ties broken by id descending, at most `limit`
 * rows. The server already returns newest activity first; re-sorting keeps
 * the block correct whatever order a refresh lands in. Never mutates `tasks`.
 */
export function deriveSidebarTaskItems(
  tasks: TaskListRow[],
  limit = SIDEBAR_TASK_LIMIT,
): TaskListRow[] {
  return [...tasks]
    .sort((a, b) => {
      const ta = activityTime(a.last_activity_at);
      const tb = activityTime(b.last_activity_at);
      if (ta === null || tb === null) {
        if (ta !== tb) return ta === null ? 1 : -1;
      } else if (ta !== tb) {
        return tb - ta;
      } else if (a.last_activity_at !== b.last_activity_at) {
        // Same millisecond: the raw naive-ISO strings still order microseconds.
        return (a.last_activity_at ?? '') < (b.last_activity_at ?? '') ? 1 : -1;
      }
      if (a.id === b.id) return 0;
      return a.id < b.id ? 1 : -1;
    })
    .slice(0, Math.max(0, limit));
}
