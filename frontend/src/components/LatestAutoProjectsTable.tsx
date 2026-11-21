/**
 * LatestAutoProjectsTable - System Reports "AutoProjects" section.
 *
 * Polls /admin/system-monitor/latest-active-autoprojects and renders the
 * most recently active AutoProjects across all users, each row aggregating
 * the token usage and cost of all the AutoProject's updates (runs).
 */

import { fetchLatestActiveAutoProjects } from '../api/client';
import { formatRelativeTimestamp } from '../utils/formatters';
import { ConversationUsageCell } from './ConversationUsageCell';
import { PolledReportFrame, ReportUserCell, usePolledReport } from './PolledReport';

export function LatestAutoProjectsTable() {
  const report = usePolledReport(fetchLatestActiveAutoProjects);
  const rows = report.data?.autoprojects ?? null;

  return (
    <PolledReportFrame
      title="Latest AutoProjects"
      report={report}
      rowCount={rows ? rows.length : null}
      emptyLabel="No AutoProjects."
    >
      <table className="latest-active-groups">
        <thead>
          <tr>
            <th className="col-title">AutoProject</th>
            <th className="col-user">User</th>
            <th className="col-tokens">Token usage</th>
            <th className="col-count">Updates</th>
            <th className="col-active-days">Active days</th>
            <th className="col-last-active">Last active</th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => (
            <tr key={row.id}>
              <td className="col-title" title={row.title}>
                {row.title}
                {row.archived && <span className="scope-badge">archived</span>}
              </td>
              <ReportUserCell name={row.user_name} email={row.user_email} />
              <td className="col-tokens">
                <ConversationUsageCell
                  usageByModel={row.usage_by_model}
                  usageTotal={row.usage_total}
                />
              </td>
              <td className="col-count" title="Updates (runs) the AutoProject holds">
                {row.run_count}
                {row.failed_run_count > 0 && (
                  <span className="count-detail">{row.failed_run_count} failed</span>
                )}
              </td>
              <td
                className="col-active-days"
                title="Distinct UTC days on which an update was started"
              >
                {row.active_days}
              </td>
              <td className="col-last-active">
                {row.last_activity_at ? formatRelativeTimestamp(row.last_activity_at) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </PolledReportFrame>
  );
}
