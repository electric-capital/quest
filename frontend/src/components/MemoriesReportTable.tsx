/**
 * MemoriesReportTable - System Reports "Memories" section.
 *
 * Fetches /admin/system-monitor/memories-report for a selected date range
 * and renders one row per user with any memory activity: active and
 * archived personal memories with the characters stored, memories created
 * in range, the suggestion funnel for suggestions raised in range (accepted
 * / dismissed / still pending), and the same for project memories (in the
 * projects the user owns) and project suggestions (raised in the user's
 * conversations). Memory text is never shipped. Frame, fetch cycle and cell
 * helpers come from FeatureUsageReport.tsx.
 */

import { useMemo } from 'react';
import { fetchAdminMemoriesReport } from '../api/client';
import { formatCompactNumber, formatNumber } from '../utils/formatters';
import {
  Count,
  FeatureUsageReportFrame,
  OwnerCell,
  SubLine,
  WhenCell,
  plural,
  useRangedAdminReport,
} from './FeatureUsageReport';

/**
 * Suggestion funnel cell: suggestions raised in range over their current
 * status split ("12" / "8 accepted · 3 dismissed · 1 pending").
 */
function FunnelCell({
  suggested,
  accepted,
  dismissed,
  pending,
  className,
}: {
  suggested: number;
  accepted: number;
  dismissed: number;
  pending: number;
  className: string;
}) {
  const rate = suggested > 0 ? Math.round((accepted / suggested) * 100) : null;
  return (
    <td
      className={className}
      title={
        suggested === 0
          ? 'No suggestions raised in the selected range'
          : `${plural(suggested, 'suggestion')} raised in range; ${rate}% accepted so far`
      }
    >
      <Count value={suggested} />
      {suggested > 0 && (
        <SubLine
          parts={[
            accepted > 0 ? `${formatNumber(accepted)} accepted` : null,
            dismissed > 0 ? `${formatNumber(dismissed)} dismissed` : null,
            pending > 0 ? `${formatNumber(pending)} pending` : null,
          ]}
        />
      )}
    </td>
  );
}

export function MemoriesReportTable() {
  const { data, error, isRefreshing, load, range } = useRangedAdminReport(
    fetchAdminMemoriesReport,
  );
  const rows = data?.users ?? null;

  const summary = useMemo(() => {
    if (!data) return null;
    const memories = data.users.reduce((n, r) => n + r.memory_count, 0);
    const projectMemories = data.users.reduce((n, r) => n + r.project_memory_count, 0);
    const suggested = data.users.reduce(
      (n, r) => n + r.suggested_in_range + r.project_suggested_in_range,
      0,
    );
    const accepted = data.users.reduce(
      (n, r) => n + r.accepted_in_range + r.project_accepted_in_range,
      0,
    );
    const funnel =
      suggested === 0
        ? 'no suggestions in range'
        : `${formatNumber(suggested)} suggested in range, ` +
          `${Math.round((accepted / suggested) * 100)}% accepted`;
    return (
      `${plural(memories, 'memory', 'memories')} + ${formatNumber(projectMemories)} project · ` +
      `${formatNumber(data.users.length)} of ${plural(data.total_users, 'user')} · ${funnel}`
    );
  }, [data]);

  return (
    <FeatureUsageReportFrame
      title="Memories"
      summary={summary}
      summaryTitle="Active personal and project memories, users with any memory activity, and the suggestion acceptance rate in the selected range"
      range={range}
      isRefreshing={isRefreshing}
      onRefresh={() => load()}
      loading={rows === null}
      emptyMessage={rows && rows.length === 0 ? 'No memories or suggestions in the system.' : null}
      error={error}
    >
      <table>
        <thead>
          <tr>
            <th className="col-owner col-w17">User</th>
            <th className="col-w8" title="Active personal memories (archived ones in the sub-line)">
              Memories
            </th>
            <th className="col-w6" title="Characters stored in the active personal memories">
              Chars
            </th>
            <th
              className="col-w8 col-range"
              title="Personal memories created in the selected range (accepted suggestions included)"
            >
              Created
            </th>
            <th
              className="col-w15 col-range"
              title="Memory suggestions the model raised in the user's conversations in the selected range, by their current status"
            >
              Suggestions
            </th>
            <th
              className="col-w15"
              title="Active memories of the projects the user owns, and how many of those projects hold one"
            >
              Project memories
            </th>
            <th
              className="col-w16 col-range"
              title="Project memory suggestions raised in the user's conversations in the selected range, by their current status"
            >
              Project suggestions
            </th>
            <th className="col-when col-w15" title="Most recent memory or suggestion event of any kind">
              Last activity
            </th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => (
            <tr key={row.user_id}>
              <OwnerCell name={row.user_name} email={row.user_email} />
              <td className="col-w8">
                <Count value={row.memory_count} />
                <SubLine
                  parts={[
                    row.archived_count > 0 ? `+${formatNumber(row.archived_count)} archived` : null,
                  ]}
                />
              </td>
              <td className="col-w6" title={`${formatNumber(row.content_chars)} characters`}>
                {row.content_chars === 0 ? (
                  <span className="cell-empty">&mdash;</span>
                ) : (
                  formatCompactNumber(row.content_chars)
                )}
              </td>
              <td className="col-w8 col-range">
                <Count value={row.created_in_range} />
              </td>
              <FunnelCell
                className="col-w15 col-range"
                suggested={row.suggested_in_range}
                accepted={row.accepted_in_range}
                dismissed={row.dismissed_in_range}
                pending={row.pending_in_range}
              />
              <td className="col-w15">
                <Count value={row.project_memory_count} />
                <SubLine
                  parts={[
                    row.project_count > 0 ? `in ${plural(row.project_count, 'project')}` : null,
                    row.project_archived_count > 0
                      ? `+${formatNumber(row.project_archived_count)} archived`
                      : null,
                    row.project_created_in_range > 0
                      ? `${formatNumber(row.project_created_in_range)} created in range`
                      : null,
                  ]}
                />
              </td>
              <FunnelCell
                className="col-w16 col-range"
                suggested={row.project_suggested_in_range}
                accepted={row.project_accepted_in_range}
                dismissed={row.project_dismissed_in_range}
                pending={row.project_pending_in_range}
              />
              <WhenCell iso={row.last_activity_at} />
            </tr>
          ))}
        </tbody>
      </table>
    </FeatureUsageReportFrame>
  );
}
