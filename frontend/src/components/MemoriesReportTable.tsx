/**
 * MemoriesReportTable - System Reports "Memories" section.
 *
 * Fetches /admin/system-monitor/memories-report for a selected date range
 * and renders one row per user with any memory activity: active and
 * archived personal memories with the characters stored, memories created
 * in range, the suggestion funnel for suggestions raised in the user's
 * conversations in range (accepted / dismissed / still pending), the dreams
 * (offline memory reviews) run in range with their failures and cost and
 * the funnel of the suggestions they raised, and the same for project
 * memories (in the projects the user owns) and project suggestions (raised
 * in the user's conversations). Memory text is never shipped. Frame, fetch
 * cycle and cell helpers come from FeatureUsageReport.tsx.
 */

import { useMemo } from 'react';
import { fetchAdminMemoriesReport } from '../api/client';
import type { AdminMemoriesReportRow } from '../api/types';
import { formatCompactNumber, formatNumber, formatRelativeTimestamp } from '../utils/formatters';
import { formatCost } from './ConversationUsageCell';
import {
  Count,
  FeatureUsageReportFrame,
  OwnerCell,
  SubLine,
  WhenCell,
  plural,
  useRangedAdminReport,
} from './FeatureUsageReport';

// Dream costs are always list-price estimates (the run row carries no
// provider-reported amount), hence the "~" prefix.
function formatDreamCost(cost: number): string {
  return formatCost(cost, 'estimated');
}

/**
 * Dreams cell: runs started in range over their failures, summed cost and
 * the newest run of any date ("3" / "1 failed · ~$0.04 · last 2d ago").
 */
function DreamsCell({ row }: { row: AdminMemoriesReportRow }) {
  const runs = row.dreams_in_range;
  const cost =
    row.dream_cost_usd_in_range === null
      ? 'cost n/a (a run used an unpriced model)'
      : runs > 0 && row.dream_cost_usd_in_range === 0
        ? 'free'
        : formatDreamCost(row.dream_cost_usd_in_range);
  const lifetime =
    row.dream_count === 0
      ? 'never dreamed'
      : `${plural(row.dream_count, 'dream')} all time, last ${row.last_dream_at}`;
  return (
    <td
      className="col-w9 col-range"
      title={
        runs === 0
          ? `No dreams started in the selected range; ${lifetime}`
          : `${plural(runs, 'dream')} started in range` +
            `${row.dreams_failed_in_range > 0 ? ` (${row.dreams_failed_in_range} failed)` : ''}, ` +
            `${cost}; ${lifetime}`
      }
    >
      <Count value={runs} />
      <SubLine
        parts={[
          row.dreams_failed_in_range > 0 ? `${formatNumber(row.dreams_failed_in_range)} failed` : null,
          runs > 0 ? (row.dream_cost_usd_in_range === null ? 'cost n/a' : cost) : null,
          row.last_dream_at ? `last ${formatRelativeTimestamp(row.last_dream_at)}` : null,
        ]}
      />
    </td>
  );
}

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
    const dreams = data.users.reduce((n, r) => n + r.dreams_in_range, 0);
    const dreamSuggested = data.users.reduce((n, r) => n + r.dream_suggested_in_range, 0);
    const dreamAccepted = data.users.reduce((n, r) => n + r.dream_accepted_in_range, 0);
    // The priceable portion, marked as a lower bound when a run ran on an
    // unpriced model (the Total Usage convention).
    const dreamCost = data.users.reduce((n, r) => n + (r.dream_cost_usd_in_range ?? 0), 0);
    const dreamCostPartial = data.users.some((r) => r.dream_cost_usd_in_range === null);
    const dreaming =
      dreams === 0
        ? 'no dreams in range'
        : `${plural(dreams, 'dream')} in range ` +
          `(${dreamCostPartial ? '≥ ' : ''}${formatDreamCost(dreamCost)}), ` +
          (dreamSuggested === 0
            ? 'nothing proposed'
            : `${formatNumber(dreamSuggested)} proposed, ` +
              `${Math.round((dreamAccepted / dreamSuggested) * 100)}% accepted`);
    return (
      `${plural(memories, 'memory', 'memories')} + ${formatNumber(projectMemories)} project · ` +
      `${formatNumber(data.users.length)} of ${plural(data.total_users, 'user')} · ${funnel} · ` +
      dreaming
    );
  }, [data]);

  return (
    <FeatureUsageReportFrame
      title="Memories"
      summary={summary}
      summaryTitle="Active personal and project memories, users with any memory activity, the suggestion acceptance rate in the selected range, and the dreams (offline memory reviews) run in range with their cost and acceptance rate"
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
            <th className="col-owner col-w14">User</th>
            <th className="col-w7" title="Active personal memories (archived ones in the sub-line)">
              Memories
            </th>
            <th className="col-w6" title="Characters stored in the active personal memories">
              Chars
            </th>
            <th
              className="col-w6 col-range"
              title="Personal memories created in the selected range (accepted suggestions included)"
            >
              Created
            </th>
            <th
              className="col-w11 col-range"
              title="Memory suggestions the model raised in the user's conversations in the selected range, by their current status"
            >
              Suggestions
            </th>
            <th
              className="col-w9 col-range"
              title="Dreams (offline reviews of the user's memories) started in the selected range, with failures and the list-price cost of their model calls; the newest dream of any date in the sub-line"
            >
              Dreams
            </th>
            <th
              className="col-w12 col-range"
              title="Memory changes the dreams proposed in the selected range, by their current status"
            >
              Dream proposals
            </th>
            <th
              className="col-w12"
              title="Active memories of the projects the user owns, and how many of those projects hold one"
            >
              Project memories
            </th>
            <th
              className="col-w14 col-range"
              title="Project memory suggestions raised in the user's conversations in the selected range, by their current status"
            >
              Project suggestions
            </th>
            <th className="col-when col-w9" title="Most recent memory or suggestion event of any kind">
              Last activity
            </th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => (
            <tr key={row.user_id}>
              <OwnerCell name={row.user_name} email={row.user_email} />
              <td className="col-w7">
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
              <td className="col-w6 col-range">
                <Count value={row.created_in_range} />
              </td>
              <FunnelCell
                className="col-w11 col-range"
                suggested={row.suggested_in_range}
                accepted={row.accepted_in_range}
                dismissed={row.dismissed_in_range}
                pending={row.pending_in_range}
              />
              <DreamsCell row={row} />
              <FunnelCell
                className="col-w12 col-range"
                suggested={row.dream_suggested_in_range}
                accepted={row.dream_accepted_in_range}
                dismissed={row.dream_dismissed_in_range}
                pending={row.dream_pending_in_range}
              />
              <td className="col-w12">
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
                className="col-w14 col-range"
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
