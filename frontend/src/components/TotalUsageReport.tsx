/**
 * TotalUsageReport - System Reports "Total Usage" section.
 *
 * Fetches /admin/system-monitor/usage-report (no parameters: the report
 * always covers fixed rolling windows plus lifetime) and renders two
 * things: a comparison matrix -- one row per window (today, last 7 / 30 /
 * 90 / 365 days, all time), one column per metric (conversations, active
 * users, new users, calls, tokens, cost), each cell carrying the figure
 * and its change vs the preceding period of the same length -- and four
 * trend charts (conversations, active users, cost, tokens) over a daily /
 * weekly / monthly series picked by a granularity dropdown. Fetch-on-demand
 * like the other report sections: on mount and via the refresh button.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { fetchAdminUsageReport } from '../api/client';
import type {
  AdminUsageBucket,
  AdminUsageGranularity,
  AdminUsageReport,
} from '../api/types';
import { describeCostSource, formatCost, formatUsd } from './ConversationUsageCell';
import { UsageBarChart } from './UsageBarChart';
import { formatCompactNumber, formatNumber } from '../utils/formatters';
import {
  describeMetricDelta,
  describePreviousPeriod,
  describeWindow,
  formatDateRange,
} from '../utils/usageReport';
import type { MetricKind } from '../utils/usageReport';
import './TotalUsageReport.css';

const GRANULARITIES: { key: AdminUsageGranularity; label: string }[] = [
  { key: 'daily', label: 'Daily · last 90 days' },
  { key: 'weekly', label: 'Weekly · last 52 weeks' },
  { key: 'monthly', label: 'Monthly · last 24 months' },
];

// Matrix columns. `good` says which direction of change is the welcome
// one (growth for activity, decline for spend) so the delta colouring
// reads the same way across columns.
interface MetricColumn {
  key: string;
  label: string;
  kind: MetricKind;
  good: 'up' | 'down';
  title: string;
  valueOf: (b: AdminUsageBucket) => number | null;
  format: (value: number, b: AdminUsageBucket) => string;
  detail?: (b: AdminUsageBucket) => string | null;
}

const METRIC_COLUMNS: MetricColumn[] = [
  {
    key: 'conversations',
    label: 'Conversations',
    kind: 'count',
    good: 'up',
    title: 'Distinct conversations with at least one model call in the period, routine runs included',
    valueOf: (b) => b.conversations,
    format: (v) => formatNumber(v),
    detail: (b) =>
      b.routine_conversations > 0
        ? `${formatNumber(b.routine_conversations)} routine ${b.routine_conversations === 1 ? 'run' : 'runs'}`
        : null,
  },
  {
    key: 'active_users',
    label: 'Active users',
    kind: 'count',
    good: 'up',
    title: 'Distinct users with at least one model call in a non-routine conversation in the period (routine runs do not make a user active)',
    valueOf: (b) => b.active_users,
    format: (v) => formatNumber(v),
  },
  {
    key: 'new_users',
    label: 'New users',
    kind: 'count',
    good: 'up',
    title: 'Accounts created in the period',
    valueOf: (b) => b.new_users,
    format: (v) => formatNumber(v),
  },
  {
    key: 'call_count',
    label: 'Calls',
    kind: 'count',
    good: 'up',
    title: 'Model API calls recorded in the period (top-level and sub-agent)',
    valueOf: (b) => b.call_count,
    format: (v) => formatCompactNumber(v),
  },
  {
    key: 'total_tokens',
    label: 'Tokens',
    kind: 'count',
    good: 'up',
    title: 'All-in token magnitude across providers (input, cached, output, thinking)',
    valueOf: (b) => b.total_tokens,
    format: (v) => formatCompactNumber(v),
  },
  {
    key: 'cost',
    label: 'Cost',
    kind: 'cost',
    good: 'down',
    title: 'Spend in the period: provider-reported amounts where captured, list-price estimates otherwise (~). Shown as a ≥ lower bound when a model without a pricing entry was also used.',
    valueOf: (b) => b.cost_usd,
    format: (v, b) => formatCost(v, b.cost_source),
  },
];

const PARTIAL_COST_TITLE =
  'Lower bound: the period also contains calls on a model without a pricing entry, which cannot be priced';

/**
 * One matrix cell's figure. A null cost (an unpriced model in the period)
 * shows the priceable portion as a "≥" lower bound rather than hiding the
 * spend behind n/a, since one unpriced model would otherwise blank the
 * lifetime figure for good.
 */
function formatCellValue(column: MetricColumn, bucket: AdminUsageBucket): string {
  const value = column.valueOf(bucket);
  if (value === null) {
    return column.kind === 'cost' ? `≥ ~${formatUsd(bucket.known_cost_usd)}` : 'n/a';
  }
  return column.format(value, bucket);
}

/**
 * One matrix cell: the window's figure, an optional detail line, and the
 * change vs the previous period (omitted for the lifetime row, which has
 * no previous period).
 */
function MetricCell({
  column,
  current,
  previous,
  days,
}: {
  column: MetricColumn;
  current: AdminUsageBucket;
  previous: AdminUsageBucket | null;
  days: number | null;
}) {
  const value = column.valueOf(current);
  const detail = column.detail?.(current);
  const exactTitle =
    column.kind === 'cost' && value !== null
      ? `$${value.toFixed(4)} (${describeCostSource(current.cost_source)})`
      : value !== null && column.format(value, current) !== formatNumber(value)
        ? formatNumber(value)
        : undefined;
  const delta =
    previous && days !== null
      ? describeMetricDelta(
          value,
          column.valueOf(previous),
          column.kind,
          `${describePreviousPeriod(days)} (${formatDateRange(previous.start, previous.end)})`,
        )
      : null;
  const deltaClass =
    delta === null || delta.trend === 'flat' || delta.trend === 'none'
      ? 'usage-delta-neutral'
      : delta.trend === column.good
        ? 'usage-delta-good'
        : 'usage-delta-bad';
  return (
    <td className={`usage-metric-cell${column.kind === 'cost' ? ' usage-metric-cost' : ''}`}>
      <div
        className={`usage-metric-value${value === null ? ' usage-metric-partial' : ''}`}
        title={value === null ? PARTIAL_COST_TITLE : exactTitle}
      >
        {formatCellValue(column, current)}
      </div>
      {detail && <div className="usage-metric-detail">{detail}</div>}
      {delta && (
        <div className={`usage-metric-delta ${deltaClass}`} title={delta.title}>
          {delta.trend === 'up' && <span aria-hidden="true">&#9650; </span>}
          {delta.trend === 'down' && <span aria-hidden="true">&#9660; </span>}
          {delta.text}
        </div>
      )}
    </td>
  );
}

export function TotalUsageReport() {
  const [report, setReport] = useState<AdminUsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [granularity, setGranularity] = useState<AdminUsageGranularity>('daily');
  const mountedRef = useRef(true);
  // Monotonic fetch counter so a slow response never overwrites a newer one.
  const fetchSeqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++fetchSeqRef.current;
    setIsRefreshing(true);
    try {
      const resp = await fetchAdminUsageReport();
      if (!mountedRef.current || seq !== fetchSeqRef.current) return;
      setReport(resp);
      setError(null);
    } catch (err) {
      if (!mountedRef.current || seq !== fetchSeqRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current && seq === fetchSeqRef.current) {
        setIsRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  const series = report ? report.series[granularity] : [];
  const hasEstimate =
    report !== null &&
    [report.lifetime, ...report.windows.map((w) => w.current)].some(
      (b) => b.cost_usd !== null && b.cost_usd > 0 && b.cost_source !== 'reported',
    );

  return (
    <div className="total-usage">
      <div className="total-usage-header">
        <h3>Total Usage</h3>
        <div className="header-actions">
          <button
            type="button"
            className="refresh-button"
            title="Refresh now"
            aria-label="Refresh now"
            disabled={isRefreshing}
            onClick={() => load()}
          >
            <RefreshCw size={12} className={isRefreshing ? 'spinning' : undefined} />
          </button>
        </div>
      </div>
      {report === null ? (
        <div className="total-usage-loading">Loading...</div>
      ) : (
        <>
          <table className="total-usage-matrix">
            <thead>
              <tr>
                <th className="col-period">Period</th>
                {METRIC_COLUMNS.map((column) => (
                  <th key={column.key} title={column.title}>
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {report.windows.map((window) => (
                <tr key={window.days}>
                  <td className="col-period">
                    <div className="usage-period-label">{describeWindow(window.days)}</div>
                    <div className="usage-period-range">
                      {formatDateRange(window.current.start, window.current.end)}
                    </div>
                  </td>
                  {METRIC_COLUMNS.map((column) => (
                    <MetricCell
                      key={column.key}
                      column={column}
                      current={window.current}
                      previous={window.previous}
                      days={window.days}
                    />
                  ))}
                </tr>
              ))}
              <tr className="usage-lifetime-row">
                <td className="col-period">
                  <div className="usage-period-label">All time</div>
                  <div className="usage-period-range">
                    since {formatDateRange(report.lifetime.start, report.lifetime.start)}
                  </div>
                </td>
                {METRIC_COLUMNS.map((column) => (
                  <MetricCell
                    key={column.key}
                    column={column}
                    current={report.lifetime}
                    previous={null}
                    days={null}
                  />
                ))}
              </tr>
            </tbody>
          </table>

          <div className="total-usage-trends-header">
            <span className="total-usage-trends-title">Trends</span>
            <select
              className="report-range-select"
              value={granularity}
              onChange={(e) => setGranularity(e.target.value as AdminUsageGranularity)}
              aria-label="Chart granularity"
            >
              {GRANULARITIES.map((g) => (
                <option key={g.key} value={g.key}>
                  {g.label}
                </option>
              ))}
            </select>
          </div>
          <div className="total-usage-charts">
            <UsageBarChart
              title="Conversations"
              points={series}
              granularity={granularity}
              today={report.today}
              valueOf={(b) => b.conversations}
              integer
              formatValue={(v) => formatNumber(v)}
              formatAxis={formatCompactNumber}
            />
            <UsageBarChart
              title="Active users"
              points={series}
              granularity={granularity}
              today={report.today}
              valueOf={(b) => b.active_users}
              integer
              formatValue={(v) => formatNumber(v)}
              formatAxis={formatCompactNumber}
            />
            <UsageBarChart
              title="Cost"
              points={series}
              granularity={granularity}
              today={report.today}
              valueOf={(b) => b.known_cost_usd}
              partialOf={(b) => b.cost_usd === null}
              formatValue={(v) => (v > 0 && v < 0.01 ? '<$0.01' : formatUsd(v))}
              formatAxis={(v) => `$${formatCompactNumber(v)}`}
            />
            <UsageBarChart
              title="Tokens"
              points={series}
              granularity={granularity}
              today={report.today}
              valueOf={(b) => b.total_tokens}
              integer
              formatValue={(v) => formatNumber(v)}
              formatAxis={formatCompactNumber}
            />
          </div>

          <p className="total-usage-footnote">
            Every figure keys on the UTC day of each model call. Conversations and active
            users are distinct within each period, so a week is not the sum of its days.
            Active users exclude routine-only activity.
            {hasEstimate && ' Figures marked ~ include list-price estimates.'}
            {' '}Costs marked ≥ are lower bounds: the period also contains calls on a
            model without a pricing entry. The cost chart plots that priceable portion and
            hatches such periods.
          </p>
        </>
      )}
      {error && <div className="total-usage-error">error: {error}</div>}
    </div>
  );
}
