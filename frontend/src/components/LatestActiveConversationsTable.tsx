/**
 * LatestActiveConversationsTable - System Reports "Latest Conversations"
 * section.
 *
 * Polls /admin/system-monitor/latest-active-conversations (usePolledReport:
 * every 60s, paused while the tab is hidden, manual refresh on demand) and
 * renders the global top-20 most-recently-active conversations. A future
 * iteration could replace the poll with an admin-scoped WS event
 * ("admin_activity_changed"); for v1 polling is fine.
 */

import { useCallback, useState } from 'react';
import { fetchLatestActiveConversations } from '../api/client';
import { formatNumber, formatRelativeTimestamp } from '../utils/formatters';
import { ConversationUsageCell } from './ConversationUsageCell';
import { PolledReportFrame, ReportUserCell, usePolledReport } from './PolledReport';

// Title prefix marking conversations created by a routine (routine_id set --
// scheduled or one-click runs).
const ROUTINE_EMOJI = '⏰';

export function LatestActiveConversationsTable() {
  const [includeRoutines, setIncludeRoutines] = useState(true);
  // Toggling the routines filter recreates the fetcher, which restarts the
  // poll with an immediate re-fetch under the new filter.
  const fetcher = useCallback(
    () => fetchLatestActiveConversations(undefined, includeRoutines),
    [includeRoutines],
  );
  const report = usePolledReport(fetcher);
  const rows = report.data?.conversations ?? null;

  // The server already filters when includeRoutines is off; this client-side
  // pass makes the toggle take effect instantly on the rows we already have
  // (the refetch then restores a full-length list).
  const visibleRows =
    rows === null || includeRoutines
      ? rows
      : rows.filter((row) => !row.routine_id);

  return (
    <PolledReportFrame
      title="Latest conversations"
      report={report}
      rowCount={visibleRows ? visibleRows.length : null}
      emptyLabel="No active conversations."
      actions={
        <label className="routines-toggle" title="Show conversations created by routines">
          <input
            type="checkbox"
            checked={includeRoutines}
            onChange={(e) => setIncludeRoutines(e.target.checked)}
          />
          Include Routines
        </label>
      }
    >
      <table>
        <thead>
          <tr>
            <th className="col-title">Conversation</th>
            <th className="col-user">User</th>
            <th className="col-tokens">Token usage</th>
            <th className="col-context">Context</th>
            <th className="col-active-days">Active days</th>
            <th className="col-model">Model</th>
            <th className="col-last-active">Last active</th>
          </tr>
        </thead>
        <tbody>
          {(visibleRows ?? []).map((row) => (
            <tr key={row.id}>
              <td className="col-title" title={row.title}>
                {row.routine_id ? `${ROUTINE_EMOJI} ${row.title}` : row.title}
              </td>
              <ReportUserCell name={row.user_name} email={row.user_email} />
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
                className="col-model"
                title={row.last_model ?? undefined}
              >
                {row.last_model ? (
                  row.last_model
                ) : (
                  <span className="cell-empty">&mdash;</span>
                )}
              </td>
              <td className="col-last-active">
                {row.last_message_at ? formatRelativeTimestamp(row.last_message_at) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </PolledReportFrame>
  );
}
