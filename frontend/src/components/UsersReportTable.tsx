/**
 * UsersReportTable - System Reports "Users" section.
 *
 * Fetches /admin/system-monitor/user-report for a selected date range (same
 * picker as Top Conversations) and renders one row per user: range-clipped
 * active days and conversation count (both excluding routines), the
 * cost split into non-routine vs routine spend (provider-reported where
 * the provider reports it, list-price estimated otherwise), the routine spend
 * itemized per routine (priciest first), and the per-model token breakdown
 * aggregated across all the user's conversations. On-demand
 * report like Top Conversations: fetches on mount, on range change, and via the
 * manual refresh button.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { fetchAdminUserReport } from '../api/client';
import type {
  AdminCostSource,
  AdminUserReportRow,
  AdminUserRoutineCost,
} from '../api/types';
import {
  ConversationUsageCell,
  describeCostSource,
  formatCost,
} from './ConversationUsageCell';
import { ReportDateRange, resolveRange } from './ReportDateRange';
import type { RangeKey } from './ReportDateRange';
import './UsersReportTable.css';

const UNPRICED_TITLE =
  'Some models in this split have no pricing entry; per-model estimates are in the token-usage tooltips';

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

// Routines shown before the per-cell "+N more" expander kicks in. Power
// users can own dozens of routines; the priciest few are what the column
// is for, the rest stay one click away.
const ROUTINE_ROWS_COLLAPSED = 5;

function formatRoutineTooltip(r: AdminUserRoutineCost): string {
  const where = r.project_name ? `${r.routine_name} (${r.project_name})` : r.routine_name;
  const runs = `${r.conversation_count} run${r.conversation_count === 1 ? '' : 's'} in range`;
  const cost =
    r.cost_usd === null
      ? 'cost: n/a (a model used by this routine has no pricing entry)'
      : `cost: ${r.cost_source === 'reported' ? '' : '~'}$${r.cost_usd.toFixed(4)} ` +
        `(${describeCostSource(r.cost_source)})`;
  return `${where}\n${runs}\n${cost}`;
}

/**
 * Per-routine itemization of the user's routine spend: one row per routine
 * (name, project, ~$), priciest first, collapsed past the top few.
 */
function RoutineCostsCell({ routines }: { routines: AdminUserRoutineCost[] }) {
  const [expanded, setExpanded] = useState(false);
  if (routines.length === 0) {
    return <span className="cell-empty">&mdash;</span>;
  }
  const hidden = routines.length - ROUTINE_ROWS_COLLAPSED;
  const visible = expanded || hidden <= 0 ? routines : routines.slice(0, ROUTINE_ROWS_COLLAPSED);
  return (
    <div className="routine-costs-cell">
      {visible.map((r) => (
        <div key={r.routine_id} className="routine-cost-row" title={formatRoutineTooltip(r)}>
          <span className="routine-cost-name">
            {r.routine_name}
            {r.project_name && (
              <span className="routine-cost-project"> · {r.project_name}</span>
            )}
          </span>
          <span className="routine-cost-value">
            {r.cost_usd === null ? (
              <span className="cell-empty">n/a</span>
            ) : (
              <>{formatCost(r.cost_usd, r.cost_source)}</>
            )}
          </span>
        </div>
      ))}
      {hidden > 0 && (
        <button
          type="button"
          className="routine-costs-toggle"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show fewer' : `+${hidden} more`}
        </button>
      )}
    </div>
  );
}

export function UsersReportTable() {
  const [rows, setRows] = useState<AdminUserReportRow[] | null>(null);
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
      const resp = await fetchAdminUserReport(start, end);
      if (!mountedRef.current || seq !== fetchSeqRef.current) return;
      setRows(resp.users);
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
    <div className="users-report">
      <div className="users-report-header">
        <h3>Users</h3>
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
        <div className="users-report-loading">Loading...</div>
      ) : rows.length === 0 ? (
        <div className="users-report-empty">No users.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th className="col-user">User</th>
              <th className="col-active-days">Active days</th>
              <th className="col-conversations">Convos</th>
              <th className="col-cost">Cost</th>
              <th className="col-cost">Routine cost</th>
              <th className="col-routines">By routine</th>
              <th className="col-tokens">Token usage</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.user_id}>
                <td
                  className="col-user"
                  title={row.user_name ? `${row.user_name} <${row.user_email}>` : row.user_email}
                >
                  {row.user_name ? (
                    <>
                      <span className="user-cell-name">{row.user_name}</span>
                      <span className="user-cell-email">{row.user_email}</span>
                    </>
                  ) : (
                    <span className="user-cell-name">{row.user_email}</span>
                  )}
                </td>
                <td
                  className="col-active-days"
                  title="Distinct UTC days with at least one user message in the selected range, across the user's non-routine conversations"
                >
                  {row.active_days}
                </td>
                <td
                  className="col-conversations"
                  title={
                    'Non-routine conversations with at least one model call in the selected range' +
                    (row.routine_conversation_count > 0
                      ? ` (plus ${row.routine_conversation_count} routine)`
                      : '')
                  }
                >
                  {row.conversation_count}
                </td>
                <td
                  className="col-cost"
                  title="Cost of the user's non-routine conversations in the selected range (~ marks a list-price estimate)"
                >
                  <CostCell
                    cost={row.cost_excluding_routines_usd}
                    source={row.cost_excluding_routines_source}
                  />
                </td>
                <td
                  className="col-cost"
                  title="Cost of the user's routine-created conversations in the selected range (~ marks a list-price estimate)"
                >
                  <CostCell
                    cost={row.cost_routines_usd}
                    source={row.cost_routines_source}
                  />
                </td>
                <td className="col-routines">
                  <RoutineCostsCell routines={row.routine_costs} />
                </td>
                <td className="col-tokens">
                  <ConversationUsageCell
                    usageByModel={row.usage_by_model}
                    usageTotal={row.usage_total}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error && <div className="users-report-error">error: {error}</div>}
    </div>
  );
}
