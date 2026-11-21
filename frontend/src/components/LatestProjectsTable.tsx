/**
 * LatestProjectsTable - System Reports "Projects" section.
 *
 * The project-level twin of Latest Conversations: polls
 * /admin/system-monitor/latest-active-projects and renders the most
 * recently active projects across all users, each row aggregating the
 * token usage, cost and activity of all the project's conversations.
 */

import { fetchLatestActiveProjects } from '../api/client';
import { formatRelativeTimestamp } from '../utils/formatters';
import { ConversationUsageCell } from './ConversationUsageCell';
import { PolledReportFrame, ReportUserCell, usePolledReport } from './PolledReport';

export function LatestProjectsTable() {
  const report = usePolledReport(fetchLatestActiveProjects);
  const rows = report.data?.projects ?? null;

  return (
    <PolledReportFrame
      title="Latest projects"
      report={report}
      rowCount={rows ? rows.length : null}
      emptyLabel="No active projects."
    >
      <table className="latest-active-groups">
        <thead>
          <tr>
            <th className="col-title">Project</th>
            <th className="col-user">User</th>
            <th className="col-tokens">Token usage</th>
            <th className="col-count">Conversations</th>
            <th className="col-active-days">Active days</th>
            <th className="col-last-active">Last active</th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => (
            <tr key={row.id}>
              <td className="col-title" title={row.name}>
                {row.name}
                {row.public && <span className="scope-badge">public</span>}
                {row.archived && <span className="scope-badge">archived</span>}
              </td>
              <ReportUserCell name={row.user_name} email={row.user_email} />
              <td className="col-tokens">
                <ConversationUsageCell
                  usageByModel={row.usage_by_model}
                  usageTotal={row.usage_total}
                />
              </td>
              <td
                className="col-count"
                title="Conversations in the project, routine runs included"
              >
                {row.conversation_count}
                {row.routine_conversation_count > 0 && (
                  <span className="count-detail">
                    {row.routine_conversation_count} routine{' '}
                    {row.routine_conversation_count === 1 ? 'run' : 'runs'}
                  </span>
                )}
              </td>
              <td
                className="col-active-days"
                title="Distinct UTC days with at least one user message in a non-routine conversation of the project"
              >
                {row.active_days}
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
