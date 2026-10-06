/**
 * Pure helpers behind the System Reports "Total Usage" section: period
 * labels, period-over-period deltas, and the chart's axis math. Kept out of
 * the components so they are unit-testable without rendering.
 */

import type { AdminUsageBucket, AdminUsageGranularity } from '../api/types';
import { formatUsd } from '../components/ConversationUsageCell';
import { formatNumber } from './formatters';

/** Parse a `YYYY-MM-DD` report date as UTC midnight. */
export function parseReportDate(day: string): Date {
  return new Date(`${day}T00:00:00Z`);
}

const MONTH_DAY: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', timeZone: 'UTC' };
const MONTH_DAY_YEAR: Intl.DateTimeFormatOptions = { ...MONTH_DAY, year: 'numeric' };
const MONTH_YEAR: Intl.DateTimeFormatOptions = { month: 'long', year: 'numeric', timeZone: 'UTC' };

/** Short axis tick for a series point: "Oct 7" (daily/weekly), "Oct '26" (monthly). */
export function formatPeriodTick(point: AdminUsageBucket, granularity: AdminUsageGranularity): string {
  const start = parseReportDate(point.start);
  if (granularity === 'monthly') {
    const month = start.toLocaleDateString(undefined, { month: 'short', timeZone: 'UTC' });
    return `${month} '${String(start.getUTCFullYear()).slice(-2)}`;
  }
  return start.toLocaleDateString(undefined, MONTH_DAY);
}

/** Full period wording for readouts: "Oct 7, 2026", "Oct 5 – Oct 11, 2026", "October 2026". */
export function formatPeriodLabel(point: AdminUsageBucket, granularity: AdminUsageGranularity): string {
  if (granularity === 'monthly') {
    return parseReportDate(point.start).toLocaleDateString(undefined, MONTH_YEAR);
  }
  return formatDateRange(point.start, point.end);
}

/**
 * "Oct 1 – Oct 7, 2026" for a comparison-matrix row; the start carries its
 * own year when the range straddles a year boundary ("Oct 7, 2025 – Oct 6,
 * 2026"), and a one-day range collapses to a single date.
 */
export function formatDateRange(start: string, end: string): string {
  const startDate = parseReportDate(start);
  const endDate = parseReportDate(end);
  if (start === end) return startDate.toLocaleDateString(undefined, MONTH_DAY_YEAR);
  const startOptions =
    startDate.getUTCFullYear() === endDate.getUTCFullYear() ? MONTH_DAY : MONTH_DAY_YEAR;
  return `${startDate.toLocaleDateString(undefined, startOptions)} – ${endDate.toLocaleDateString(undefined, MONTH_DAY_YEAR)}`;
}

/** Row label for a comparison-matrix window. */
export function describeWindow(days: number): string {
  if (days === 1) return 'Today';
  if (days === 365) return 'Last 12 months';
  return `Last ${days} days`;
}

/** The "vs ..." wording for a window's previous period. */
export function describePreviousPeriod(days: number): string {
  if (days === 1) return 'yesterday';
  if (days === 365) return 'the 12 months before';
  return `the ${days} days before`;
}

export type MetricKind = 'count' | 'cost';

export type DeltaTrend = 'up' | 'down' | 'flat' | 'none';

export interface MetricDelta {
  trend: DeltaTrend;
  // Short cell text: "+12 (+40%)", "-$3.10 (-25%)", "no change", "n/a".
  text: string;
  // Tooltip: what the previous period's figure was.
  title: string;
}

function formatSigned(diff: number, kind: MetricKind): string {
  const sign = diff > 0 ? '+' : '-';
  const abs = Math.abs(diff);
  return `${sign}${kind === 'cost' ? formatUsd(abs) : formatNumber(abs)}`;
}

/**
 * Period-over-period wording for one metric cell: the signed change plus
 * a percentage when the previous period had a non-zero base. Either side
 * null (an unpriced model in that period's cost) yields "n/a".
 */
export function describeMetricDelta(
  current: number | null,
  previous: number | null,
  kind: MetricKind,
  previousLabel: string,
): MetricDelta {
  const base = `vs ${previousLabel}`;
  if (current === null || previous === null) {
    return {
      trend: 'none',
      text: 'n/a',
      title: `${base}: one of the periods includes a model without a pricing entry`,
    };
  }
  const diff = current - previous;
  const epsilon = kind === 'cost' ? 0.005 : 0.5;
  const previousText = kind === 'cost' ? formatUsd(previous) : formatNumber(previous);
  if (Math.abs(diff) < epsilon) {
    return { trend: 'flat', text: 'no change', title: `${base}: ${previousText}` };
  }
  const amount = formatSigned(diff, kind);
  if (previous < epsilon) {
    return { trend: 'up', text: `${amount} (from 0)`, title: `${base}: ${previousText}` };
  }
  const pct = Math.round((diff / previous) * 100);
  return {
    trend: diff > 0 ? 'up' : 'down',
    text: `${amount} (${diff > 0 ? '+' : '-'}${Math.abs(pct)}%)`,
    title: `${base}: ${previousText}`,
  };
}

/**
 * Round a chart's data maximum up to a "nice" axis ceiling (1 / 2 / 2.5 /
 * 5 / 10 times a power of ten) so gridlines land on readable values.
 */
export function niceCeil(value: number): number {
  if (!(value > 0)) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const fraction = value / magnitude;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * magnitude;
}

/**
 * `niceCeil` for integer-valued series: even ceilings (1 / 2 / 4 / 6 / 8 /
 * 10 times a power of ten) so the mid gridline lands on a whole number
 * (except for a ceiling of 1, whose mid label the chart skips).
 */
export function niceCeilEven(value: number): number {
  if (!(value > 0)) return 1;
  if (value <= 1) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const fraction = value / magnitude;
  const nice = fraction <= 2 ? 2 : fraction <= 4 ? 4 : fraction <= 6 ? 6 : fraction <= 8 ? 8 : 10;
  return nice * magnitude;
}

/**
 * Indices of the series points that get an x-axis label: evenly spaced so
 * at most `maxTicks` labels show, always including the first point.
 */
export function selectTickIndices(count: number, maxTicks: number): number[] {
  if (count <= 0) return [];
  const step = Math.max(1, Math.ceil(count / maxTicks));
  const indices: number[] = [];
  for (let i = 0; i < count; i += step) indices.push(i);
  return indices;
}
