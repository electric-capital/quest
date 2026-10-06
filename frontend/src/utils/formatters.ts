/**
 * Utility functions for formatting data in the UI
 */

/**
 * Parse ISO timestamp as UTC Date.
 * Handles timestamps with or without Z suffix (for backwards compatibility).
 */
export function parseUTCTimestamp(timestamp: string): Date {
  const isoString = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z';
  return new Date(isoString);
}

/**
 * Format timestamp for message display
 * Today: show time only ("2:30 PM")
 * Other days: show date + time ("Jan 30, 2:30 PM")
 */
export function formatTimestamp(timestamp: string): string {
  const date = parseUTCTimestamp(timestamp);
  const now = new Date();
  const isToday =
    date.getDate() === now.getDate() &&
    date.getMonth() === now.getMonth() &&
    date.getFullYear() === now.getFullYear();

  if (isToday) {
    return date.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true
    });
  } else {
    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true
    });
  }
}

/**
 * Format a number with thousand-separator commas for display.
 * Example: 12345 -> "12,345"
 */
export function formatNumber(n: number): string {
  return n.toLocaleString();
}

/**
 * Compact magnitude for dense cells and chart axes: 950 -> "950",
 * 12,345 -> "12.3K", 4,000,000 -> "4M", 1,250,000,000 -> "1.25B".
 * One significant decimal, trailing zeros dropped.
 */
export function formatCompactNumber(n: number): string {
  const abs = Math.abs(n);
  if (abs < 1000) return formatNumber(Math.round(n));
  const units: [number, string][] = [
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'K'],
  ];
  for (const [size, suffix] of units) {
    if (abs >= size) {
      const scaled = n / size;
      const digits = Math.abs(scaled) >= 100 ? 0 : Math.abs(scaled) >= 10 ? 1 : 2;
      return `${parseFloat(scaled.toFixed(digits))}${suffix}`;
    }
  }
  return formatNumber(n);
}

/**
 * Relative "last active" label for admin tables: minute granularity up to an
 * hour, then hours/days, then an absolute date for anything older than a week.
 */
export function formatRelativeTimestamp(isoTimestamp: string): string {
  const date = parseUTCTimestamp(isoTimestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / (1000 * 60));
  const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;

  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}
