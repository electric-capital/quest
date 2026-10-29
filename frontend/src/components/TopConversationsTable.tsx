/**
 * TopConversationsTable - System Reports "Top Conversations" section.
 *
 * Fetches /admin/system-monitor/most-expensive-conversations for a selected
 * date range (shortcut presets or a custom start/end pair) and renders the
 * top-30 conversations ranked by estimated cost. No polling: this is an
 * on-demand report, so it fetches on mount, on range change, and via
 * the manual refresh button.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { fetchMostExpensiveConversations } from '../api/client';
import type { AdminActiveConversation } from '../api/types';
import { formatNumber, formatRelativeTimestamp } from '../utils/formatters';
import {
  ConversationUsageCell,
  describeCostSource,
  formatCost,
} from './ConversationUsageCell';
import { ReportDateRange, resolveRange } from './ReportDateRange';
import type { RangeKey } from './ReportDateRange';
import './TopConversationsTable.css';

const LIMIT = 30;

const ROUTINE_EMOJI = '⏰';

export function TopConversationsTable() {
  const [rows, setRows] = useState<AdminActiveConversation[] | null>(null);
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
      const resp = await fetchMostExpensiveConversations(start, end, LIMIT);
      if (!mountedRef.current || seq !== fetchSeqRef.current) return;
      setRows(resp.conversations);
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
    <div className="top-conversations">
      <div className="top-conversations-header">
        <h3>Top conversations</h3>
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
        <div className="top-conversations-loading">Loading...</div>
      ) : rows.length === 0 ? (
        <div className="top-conversations-empty">
          No recorded model calls in this date range.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th className="col-rank">#</th>
              <th className="col-title">Conversation</th>
              <th className="col-user">User</th>
              <th className="col-cost">Est. cost</th>
              <th className="col-tokens">Token usage</th>
              <th className="col-context">Context</th>
              <th className="col-active-days">Active days</th>
              <th className="col-compactions">Compactions</th>
              <th className="col-last-active">Last active</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={row.id}>
                <td className="col-rank">{index + 1}</td>
                <td className="col-title" title={row.title}>
                  {row.routine_id ? `${ROUTINE_EMOJI} ${row.title}` : row.title}
                </td>
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
                <td className="col-cost">
                  {row.usage_total.estimated_cost_usd !== null ? (
                    <span
                      className="cost-value"
                      title={describeCostSource(row.usage_total.cost_source)}
                    >
                      {formatCost(
                        row.usage_total.estimated_cost_usd,
                        row.usage_total.cost_source
                      )}
                    </span>
                  ) : (
                    <span
                      className="cell-empty"
                      title="Some models in this conversation have no pricing entry; per-model estimates are in the token-usage tooltips"
                    >
                      n/a
                    </span>
                  )}
                </td>
                <td className="col-tokens">
                  <ConversationUsageCell
                    usageByModel={row.usage_by_model}
                    usageTotal={row.usage_total}
                  />
                </td>
                <td
                  className="col-context"
                  title="Input-side token count of the latest top-level model call -- what the next turn re-reads"
                >
                  {row.latest_context_tokens !== null ? (
                    formatNumber(row.latest_context_tokens)
                  ) : (
                    <span className="cell-empty">&mdash;</span>
                  )}
                </td>
                <td
                  className="col-active-days"
                  title="Distinct UTC days with at least one user message"
                >
                  {row.active_days}
                </td>
                <td
                  className="col-compactions"
                  title="Times the conversation's history was compacted, over its whole lifetime"
                >
                  {row.compaction_count > 0 ? (
                    row.compaction_count
                  ) : (
                    <span className="cell-empty">0</span>
                  )}
                </td>
                <td className="col-last-active">
                  {row.last_message_at ? formatRelativeTimestamp(row.last_message_at) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error && <div className="top-conversations-error">error: {error}</div>}
    </div>
  );
}
