/**
 * TablesReportTable - System Reports "Tables" section.
 *
 * The Quest Tables twin of DocsReportTable: fetches
 * /admin/system-monitor/tables-report for a selected date range and renders
 * one row per user who owns at least one table -- tables owned now (with
 * the user/project split, public and shared tables, tables whose latest
 * write came from a model, cached row and column totals) next to the tables
 * created and updated inside the range. Titles, schemas and rows are never
 * shipped. Frame, fetch cycle and cell helpers come from
 * FeatureUsageReport.tsx.
 */

import { useMemo } from 'react';
import { fetchAdminTablesReport } from '../api/client';
import type { AdminTablesReportRow } from '../api/types';
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

function splitLine(row: AdminTablesReportRow): (string | null)[] {
  return [
    row.user_table_count > 0 ? `${formatNumber(row.user_table_count)} user` : null,
    row.project_table_count > 0 ? `${formatNumber(row.project_table_count)} project` : null,
  ];
}

export function TablesReportTable() {
  const { data, error, isRefreshing, load, range } = useRangedAdminReport(fetchAdminTablesReport);
  const rows = data?.users ?? null;

  const summary = useMemo(() => {
    if (!data) return null;
    const tables = data.users.reduce((n, r) => n + r.table_count, 0);
    const rowTotal = data.users.reduce((n, r) => n + r.row_total, 0);
    const updated = data.users.reduce((n, r) => n + r.updated_in_range, 0);
    return (
      `${plural(tables, 'table')} · ${formatNumber(data.users.length)} of ` +
      `${plural(data.total_users, 'user')} · ${plural(rowTotal, 'row')} · ` +
      `${formatNumber(updated)} updated in range`
    );
  }, [data]);

  return (
    <FeatureUsageReportFrame
      title="Tables"
      summary={summary}
      summaryTitle="Tables in the system, users owning at least one, stored rows, and tables updated in the selected range"
      range={range}
      isRefreshing={isRefreshing}
      onRefresh={() => load()}
      loading={rows === null}
      emptyMessage={rows && rows.length === 0 ? 'No Quest Tables in the system.' : null}
      error={error}
    >
      <table>
        <thead>
          <tr>
            <th className="col-owner">Owner</th>
            <th
              className="col-w10"
              title="Tables the user owns now, split into user tables and project tables"
            >
              Tables
            </th>
            <th className="col-w7" title="Tables of public projects">
              Public
            </th>
            <th className="col-w7" title="Tables with at least one share (a user or everyone)">
              Shared
            </th>
            <th
              className="col-w8"
              title="Tables whose latest write came from a model (conversation or approved action request) rather than a person in the UI or a History restore"
            >
              By model
            </th>
            <th className="col-w9" title="Stored rows across the user's tables">
              Rows
            </th>
            <th className="col-w7" title="Columns across the user's tables">
              Columns
            </th>
            <th className="col-w8 col-range" title="Tables created in the selected range">
              Created
            </th>
            <th
              className="col-w8 col-range"
              title="Tables whose latest change (rows, schema, rename or share) falls in the selected range; a table counts once however often it was written"
            >
              Updated
            </th>
            <th className="col-when" title="Most recent change to any of the user's tables">
              Last activity
            </th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => (
            <tr key={row.user_id}>
              <OwnerCell name={row.user_name} email={row.user_email} />
              <td className="col-w10">
                <Count value={row.table_count} />
                <SubLine parts={splitLine(row)} />
              </td>
              <td className="col-w7">
                <Count value={row.public_table_count} />
              </td>
              <td className="col-w7">
                <Count value={row.shared_table_count} />
              </td>
              <td className="col-w8">
                <Count value={row.model_written_count} />
              </td>
              <td className="col-w9" title={`${formatNumber(row.row_total)} rows`}>
                {row.row_total === 0 ? (
                  <span className="cell-empty">&mdash;</span>
                ) : (
                  formatCompactNumber(row.row_total)
                )}
              </td>
              <td className="col-w7">
                <Count value={row.column_total} />
              </td>
              <td className="col-w8 col-range">
                <Count value={row.created_in_range} />
              </td>
              <td className="col-w8 col-range">
                <Count value={row.updated_in_range} />
              </td>
              <WhenCell iso={row.last_updated_at} />
            </tr>
          ))}
        </tbody>
      </table>
    </FeatureUsageReportFrame>
  );
}
