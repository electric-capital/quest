/**
 * ToolCallsReportTable - System Reports "Tool Calls" section.
 *
 * Fetches /admin/system-monitor/tool-calls-report for a selected date range
 * and renders one row per tool the model called in range (a call routed
 * through the tool_call meta-tool counts under the tool it routed to): calls,
 * the share made in routine conversations, distinct conversations and users,
 * and the newest call. Frame, fetch cycle and cell helpers come from
 * FeatureUsageReport.tsx.
 */

import { useMemo } from 'react';
import { fetchAdminToolCallsReport } from '../api/client';
import { formatNumber } from '../utils/formatters';
import {
  Count,
  FeatureUsageReportFrame,
  SubLine,
  WhenCell,
  plural,
  useRangedAdminReport,
} from './FeatureUsageReport';

export function ToolCallsReportTable() {
  const { data, error, isRefreshing, load, range } = useRangedAdminReport(
    fetchAdminToolCallsReport,
  );
  const rows = data?.tools ?? null;

  const summary = useMemo(() => {
    if (!data) return null;
    return (
      `${plural(data.call_count, 'call')} · ${plural(data.tools.length, 'tool')} · ` +
      `${formatNumber(data.conversation_count)} of ` +
      `${plural(data.scanned_conversations, 'conversation')} in range`
    );
  }, [data]);

  return (
    <FeatureUsageReportFrame
      title="Tool Calls"
      summary={summary}
      summaryTitle="Tool calls the model made in the selected range, distinct tools, and how many of the conversations overlapping the range contain a call"
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
            <th className="col-w14" title="Tool calls in the selected range">
              Calls
            </th>
            <th className="col-w14" title="Calls made in routine-created conversations">
              Routine calls
            </th>
            <th className="col-w14" title="Distinct conversations with at least one call in range">
              Conversations
            </th>
            <th className="col-w11" title="Distinct owners of those conversations">
              Users
            </th>
            <th className="col-when col-w15" title="Newest call in the selected range">
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
                <td
                  className="col-owner col-w32"
                  title={
                    row.dynamic
                      ? `${row.tool} (called through the tool_call router)`
                      : `${row.tool} (called by its own name)`
                  }
                >
                  <span className="owner-cell-name name-cell-code">{row.tool}</span>
                  <SubLine parts={[row.dynamic ? 'via tool_call' : null, share]} />
                </td>
                <td className="col-w14">
                  <Count value={row.call_count} />
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
