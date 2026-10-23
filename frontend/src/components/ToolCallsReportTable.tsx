/**
 * ToolCallsReportTable - System Reports "Tool Calls" section.
 *
 * Fetches /admin/system-monitor/tool-calls-report for a selected date range
 * and renders one row per tool called in range, from the tool_calls ledger
 * (a call routed through the tool_call meta-tool counts under the tool it
 * routed to): calls with their sub-agent / script / dynamic / imported
 * splits, the share made in routine conversations, distinct conversations
 * and users, and the newest call. Arguments and outputs are never recorded,
 * so nothing here can show them. Frame, fetch cycle and cell helpers come
 * from FeatureUsageReport.tsx.
 */

import { useMemo } from 'react';
import { fetchAdminToolCallsReport } from '../api/client';
import type { AdminToolCallsReportRow } from '../api/types';
import { formatNumber } from '../utils/formatters';
import {
  Count,
  FeatureUsageReportFrame,
  SubLine,
  WhenCell,
  plural,
  useRangedAdminReport,
} from './FeatureUsageReport';

function callSplit(row: AdminToolCallsReportRow): (string | null)[] {
  return [
    row.sub_agent_call_count > 0 ? `${formatNumber(row.sub_agent_call_count)} sub-agent` : null,
    row.script_call_count > 0 ? `${formatNumber(row.script_call_count)} script` : null,
    row.dynamic_count > 0 ? `${formatNumber(row.dynamic_count)} via tool_call` : null,
    row.backfill_count > 0 ? `${formatNumber(row.backfill_count)} imported` : null,
  ];
}

export function ToolCallsReportTable() {
  const { data, error, isRefreshing, load, range } = useRangedAdminReport(
    fetchAdminToolCallsReport,
  );
  const rows = data?.tools ?? null;

  const summary = useMemo(() => {
    if (!data) return null;
    const imported = data.backfill
      ? ` · ${formatNumber(data.backfill.row_count)} imported from history`
      : ' · history import pending';
    return (
      `${plural(data.tools.length, 'tool')} · ` +
      `${plural(data.conversation_count, 'conversation')} · ` +
      `${plural(data.call_count, 'call')} in range${imported}`
    );
  }, [data]);

  return (
    <FeatureUsageReportFrame
      title="Tool Calls"
      summary={summary}
      summaryTitle="Distinct tools called in the selected range, the conversations and calls behind them, and whether the pre-ledger transcript history has been imported"
      range={range}
      isRefreshing={isRefreshing}
      onRefresh={() => load()}
      loading={rows === null}
      emptyMessage={rows && rows.length === 0 ? 'No tool calls in the selected range.' : null}
      error={error}
    >
      <table>
        <thead>
          <tr>
            <th className="col-owner col-w32">Tool</th>
            <th
              className="col-w14"
              title="Calls in range, counted when the call was dispatched: top-level turns, sub-agents and sandbox scripts alike; the sub-line splits out sub-agent and script calls, calls issued through the tool_call meta-tool, and rows imported from pre-ledger transcripts"
            >
              Calls
            </th>
            <th className="col-w14" title="Of those, calls made in routine-created conversations">
              Routine calls
            </th>
            <th className="col-w14" title="Distinct conversations with at least one call in range">
              Conversations
            </th>
            <th className="col-w11" title="Distinct users whose conversations or scripts made the call">
              Users
            </th>
            <th className="col-when col-w15" title="Newest call of the tool in range">
              Last call
            </th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => {
            const share =
              data && data.call_count > 0
                ? `${Math.round((row.call_count / data.call_count) * 100)}% of calls`
                : null;
            return (
              <tr key={row.tool}>
                <td className="col-owner col-w32" title={row.tool}>
                  <span className="owner-cell-name name-cell-code">{row.tool}</span>
                  <SubLine parts={[share]} />
                </td>
                <td className="col-w14">
                  <Count value={row.call_count} />
                  <SubLine parts={callSplit(row)} />
                </td>
                <td className="col-w14">
                  <Count value={row.routine_call_count} />
                </td>
                <td className="col-w14">
                  <Count value={row.conversation_count} />
                </td>
                <td className="col-w11">
                  <Count value={row.user_count} />
                </td>
                <WhenCell iso={row.last_called_at} />
              </tr>
            );
          })}
        </tbody>
      </table>
    </FeatureUsageReportFrame>
  );
}
