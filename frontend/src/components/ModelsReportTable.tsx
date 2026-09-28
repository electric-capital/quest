/**
 * ModelsReportTable - System Reports "Models" section.
 *
 * Fetches /admin/system-monitor/model-report for a selected date range (same
 * picker as Cost Analysis and Users) and renders one row per model with at
 * least one call in the range: total cost, how many users used it, the ten
 * most expensive users, the share of spend accrued in routine runs and by
 * sub-agent calls, and the model's token totals. On-demand report like the
 * other ranged sections: fetches on mount, on range change, and via the
 * manual refresh button.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { fetchAdminModelReport } from '../api/client';
import type {
  AdminCostSource,
  AdminModelReportRow,
  AdminModelTopUser,
} from '../api/types';
import { getModelDisplayName } from '../constants/models';
import {
  ConversationUsageCell,
  describeCostSource,
  formatCost,
} from './ConversationUsageCell';
import { ReportDateRange, resolveRange } from './ReportDateRange';
import type { RangeKey } from './ReportDateRange';
import './ModelsReportTable.css';

const UNPRICED_TITLE =
  'This model has no pricing entry and reported no amounts, so its cost cannot be computed';

function CostCell({
  cost,
  source,
}: {
  cost: number | null;
  source: AdminCostSource | null;
}) {
  if (cost === null) {
    return (
      <span className="cell-empty" title={UNPRICED_TITLE}>
        n/a
      </span>
    );
  }
  if (cost === 0) {
    return <span className="cell-empty">&mdash;</span>;
  }
  return (
    <span className="cost-value" title={describeCostSource(source)}>
      {formatCost(cost, source)}
    </span>
  );
}

// Top users shown before the per-cell "+N more" expander kicks in; the
// server sends up to ten, the priciest few are what the column is for.
const TOP_USER_ROWS_COLLAPSED = 5;

function formatTopUserTooltip(u: AdminModelTopUser): string {
  const who = u.user_name ? `${u.user_name} <${u.user_email}>` : u.user_email || u.user_name;
  const calls = `${u.call_count} call${u.call_count === 1 ? '' : 's'} in range`;
  const cost =
    u.cost_usd === null
      ? 'cost: n/a (model has no pricing entry)'
      : `cost: ${u.cost_source === 'reported' ? '' : '~'}$${u.cost_usd.toFixed(4)} ` +
        `(${describeCostSource(u.cost_source)})`;
  return `${who}\n${calls}\n${cost}`;
}

/**
 * The model's most expensive users: one row per user (name, ~$), priciest
 * first, collapsed past the top few.
 */
function TopUsersCell({ users }: { users: AdminModelTopUser[] }) {
  const [expanded, setExpanded] = useState(false);
  if (users.length === 0) {
    return <span className="cell-empty">&mdash;</span>;
  }
  const hidden = users.length - TOP_USER_ROWS_COLLAPSED;
  const visible = expanded || hidden <= 0 ? users : users.slice(0, TOP_USER_ROWS_COLLAPSED);
  return (
    <div className="top-users-cell">
      {visible.map((u) => (
        <div key={u.user_id} className="top-user-row" title={formatTopUserTooltip(u)}>
          <span className="top-user-name">{u.user_name || u.user_email}</span>
          <span className="top-user-value">
            {u.cost_usd === null ? (
              <span className="cell-empty">n/a</span>
            ) : (
              <>{formatCost(u.cost_usd, u.cost_source)}</>
            )}
          </span>
        </div>
      ))}
      {hidden > 0 && (
        <button
          type="button"
          className="top-users-toggle"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show fewer' : `+${hidden} more`}
        </button>
      )}
    </div>
  );
}

export function ModelsReportTable() {
  const [rows, setRows] = useState<AdminModelReportRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [rangeKey, setRangeKey] = useState<RangeKey>('last30');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  const mountedRef = useRef(true);
  // Monotonic fetch counter: a stale response (slow query for a wide range)
  // must never overwrite the result of a newer range selection.
  const fetchSeqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++fetchSeqRef.current;
    setIsRefreshing(true);
    try {
      const { start, end } = resolveRange(rangeKey, customStart, customEnd);
      const resp = await fetchAdminModelReport(start, end);
      if (!mountedRef.current || seq !== fetchSeqRef.current) return;
      setRows(resp.models);
      setError(null);
    } catch (err) {
      if (!mountedRef.current || seq !== fetchSeqRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current && seq === fetchSeqRef.current) {
        setIsRefreshing(false);
      }
    }
  }, [rangeKey, customStart, customEnd]);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  return (
    <div className="models-report">
      <div className="models-report-header">
        <h3>Models</h3>
        <div className="header-actions">
          <ReportDateRange
            rangeKey={rangeKey}
            customStart={customStart}
            customEnd={customEnd}
            onRangeKeyChange={setRangeKey}
            onCustomStartChange={setCustomStart}
            onCustomEndChange={setCustomEnd}
          />
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
      {rows === null ? (
        <div className="models-report-loading">Loading...</div>
      ) : rows.length === 0 ? (
        <div className="models-report-empty">No model calls in this range.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th className="col-model">Model</th>
              <th className="col-cost">Cost</th>
              <th className="col-users">Users</th>
              <th className="col-top-users">Top users</th>
              <th className="col-cost">Routine cost</th>
              <th className="col-cost">Subagent cost</th>
              <th className="col-tokens">Token usage</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const displayName = getModelDisplayName(row.model);
              return (
                <tr key={row.model}>
                  <td className="col-model" title={`${row.model} (${row.provider})`}>
                    <span className="model-cell-name">{displayName}</span>
                    {displayName !== row.model && (
                      <span className="model-cell-id">{row.model}</span>
                    )}
                  </td>
                  <td
                    className="col-cost"
                    title="Total cost of every call to this model in the selected range, across all users (~ marks a list-price estimate)"
                  >
                    <CostCell
                      cost={row.usage_total.estimated_cost_usd}
                      source={row.usage_total.cost_source}
                    />
                  </td>
                  <td
                    className="col-users"
                    title={
                      `${row.user_count} distinct user${row.user_count === 1 ? '' : 's'} ` +
                      `across ${row.conversation_count} conversation${
                        row.conversation_count === 1 ? '' : 's'
                      } with at least one call to this model in the selected range`
                    }
                  >
                    {row.user_count}
                  </td>
                  <td className="col-top-users">
                    <TopUsersCell users={row.top_users} />
                  </td>
                  <td
                    className="col-cost"
                    title="Share of this model's cost accrued in routine-created conversations in the selected range (~ marks a list-price estimate)"
                  >
                    <CostCell
                      cost={row.cost_routines_usd}
                      source={row.cost_routines_source}
                    />
                  </td>
                  <td
                    className="col-cost"
                    title="Share of this model's cost accrued by sub-agent calls in the selected range, wherever the parent conversation ran (~ marks a list-price estimate)"
                  >
                    <CostCell
                      cost={row.cost_subagents_usd}
                      source={row.cost_subagents_source}
                    />
                  </td>
                  <td className="col-tokens">
                    <ConversationUsageCell
                      usageByModel={row.usage_by_model}
                      usageTotal={row.usage_total}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {error && <div className="models-report-error">error: {error}</div>}
    </div>
  );
}
