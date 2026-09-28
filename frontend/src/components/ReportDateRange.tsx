/**
 * ReportDateRange - shared date-range picker for the System Reports sections
 * that window on a UTC date range (Cost Analysis, Users, Models).
 *
 * A preset dropdown (Today, Last 7/30/90 days, Last 12 months, All time) plus a
 * custom start/end date pair; `resolveRange` maps the selection to the
 * endpoints' inclusive `start`/`end` query params. Extracted from
 * CostAnalysisTable so the two sections can never drift on the date math.
 */

import './ReportDateRange.css';

// Shortcut presets: days counted back from today (inclusive; 1 = today's
// UTC date alone), null = no lower bound ("All time"). "Custom" swaps the
// preset for two date inputs.
export const RANGE_PRESETS = [
  { key: 'today', label: 'Today', days: 1 },
  { key: 'last7', label: 'Last 7 days', days: 7 },
  { key: 'last30', label: 'Last 30 days', days: 30 },
  { key: 'last90', label: 'Last 90 days', days: 90 },
  { key: 'last365', label: 'Last 12 months', days: 365 },
  { key: 'all', label: 'All time', days: null },
] as const;

export type RangeKey = (typeof RANGE_PRESETS)[number]['key'] | 'custom';

/** Today's date in UTC (the timezone the llm_calls rows are stamped in). */
function utcToday(): string {
  return new Date().toISOString().slice(0, 10);
}

function utcDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/** Resolve the selected range to the endpoints' start/end query params. */
export function resolveRange(
  rangeKey: RangeKey,
  customStart: string,
  customEnd: string,
): { start?: string; end?: string } {
  if (rangeKey === 'custom') {
    return {
      start: customStart || undefined,
      end: customEnd || undefined,
    };
  }
  const preset = RANGE_PRESETS.find((p) => p.key === rangeKey);
  if (!preset || preset.days === null) return {};
  // "Last N days" includes today, so the window starts N-1 days back.
  return { start: utcDaysAgo(preset.days - 1), end: utcToday() };
}

interface ReportDateRangeProps {
  rangeKey: RangeKey;
  customStart: string;
  customEnd: string;
  onRangeKeyChange: (key: RangeKey) => void;
  onCustomStartChange: (value: string) => void;
  onCustomEndChange: (value: string) => void;
}

export function ReportDateRange({
  rangeKey,
  customStart,
  customEnd,
  onRangeKeyChange,
  onCustomStartChange,
  onCustomEndChange,
}: ReportDateRangeProps) {
  return (
    <>
      <select
        className="report-range-select"
        value={rangeKey}
        onChange={(e) => onRangeKeyChange(e.target.value as RangeKey)}
        aria-label="Date range"
      >
        {RANGE_PRESETS.map((p) => (
          <option key={p.key} value={p.key}>
            {p.label}
          </option>
        ))}
        <option value="custom">Custom range</option>
      </select>
      {rangeKey === 'custom' && (
        <span className="report-custom-range">
          <input
            type="date"
            value={customStart}
            max={customEnd || undefined}
            onChange={(e) => onCustomStartChange(e.target.value)}
            aria-label="Start date"
          />
          <span className="report-custom-range-sep">to</span>
          <input
            type="date"
            value={customEnd}
            min={customStart || undefined}
            onChange={(e) => onCustomEndChange(e.target.value)}
            aria-label="End date"
          />
        </span>
      )}
    </>
  );
}
