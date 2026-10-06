/**
 * Pure derivation for the Sidebar's Docs blocks (the user's docs on the main
 * panel, a project's docs in the drill-down): which rows to show, the header
 * count and the compact per-row time. No React, no fetching -- unit-testable
 * on plain data, like utils/sidebarItems.ts.
 */

import type { Doc } from '../api/types';
import { formatRelativeTimestamp, parseUTCTimestamp } from './formatters';

/** How many docs a sidebar Docs block lists. */
export const SIDEBAR_DOC_LIMIT = 5;

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

function timeOf(timestamp: string): number {
  const ms = parseUTCTimestamp(timestamp).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}

/**
 * The rows a sidebar Docs block shows: newest `updated_at` first, ties broken
 * by id descending (the server's keyset order), at most `limit` rows. The
 * server already returns this order; re-sorting keeps the block correct when
 * a refresh lands rows in a different order. Never mutates `docs`.
 */
export function deriveSidebarDocItems(docs: Doc[], limit = SIDEBAR_DOC_LIMIT): Doc[] {
  return [...docs]
    .sort((a, b) => {
      const byTime = timeOf(b.updated_at) - timeOf(a.updated_at);
      if (byTime !== 0) return byTime;
      // Same millisecond: the raw naive-ISO strings still order microseconds.
      if (a.updated_at !== b.updated_at) return a.updated_at < b.updated_at ? 1 : -1;
      if (a.id === b.id) return 0;
      return a.id < b.id ? 1 : -1;
    })
    .slice(0, Math.max(0, limit));
}

/**
 * Right-aligned row time. Within a week it is formatRelativeTimestamp's
 * relative label ("5m ago", "3d ago"); older docs get a short date ("Oct 6",
 * or "Oct 2025" from another year) instead of the full date + time, which
 * would squeeze the title out of a 260px sidebar row. Reads the clock like
 * formatRelativeTimestamp does (tests pin it with fake timers).
 */
export function formatSidebarDocTime(timestamp: string): string {
  const date = parseUTCTimestamp(timestamp);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  if (now.getTime() - date.getTime() < WEEK_MS) {
    return formatRelativeTimestamp(timestamp);
  }
  return date.getFullYear() === now.getFullYear()
    ? date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : date.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
}

/** Absolute timestamp for the row time's hover title. */
export function formatSidebarDocTimeTitle(timestamp: string): string {
  const date = parseUTCTimestamp(timestamp);
  return Number.isNaN(date.getTime()) ? '' : `Updated ${date.toLocaleString()}`;
}
