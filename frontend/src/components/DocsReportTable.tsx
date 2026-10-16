/**
 * DocsReportTable - System Reports "Docs" section.
 *
 * Fetches /admin/system-monitor/docs-report for a selected date range and
 * renders one row per user who owns at least one Quest Doc: what they own
 * now (docs with the user/project split, public and shared docs, docs
 * whose latest write came from a model, body bytes and embedded images)
 * next to the docs they created and updated inside the range. Titles and
 * bodies are never shipped. Frame, fetch cycle and cell helpers are shared
 * with the Tables and Memories sections (FeatureUsageReport.tsx).
 */

import { useMemo } from 'react';
import { fetchAdminDocsReport } from '../api/client';
import type { AdminDocsReportRow } from '../api/types';
import { formatBytes, formatNumber } from '../utils/formatters';
import {
  Count,
  FeatureUsageReportFrame,
  OwnerCell,
  SubLine,
  WhenCell,
  plural,
  useRangedAdminReport,
} from './FeatureUsageReport';

function splitLine(row: AdminDocsReportRow): (string | null)[] {
  return [
    row.user_doc_count > 0 ? `${formatNumber(row.user_doc_count)} user` : null,
    row.project_doc_count > 0 ? `${formatNumber(row.project_doc_count)} project` : null,
  ];
}

export function DocsReportTable() {
  const { data, error, isRefreshing, load, range } = useRangedAdminReport(fetchAdminDocsReport);
  const rows = data?.users ?? null;

  const summary = useMemo(() => {
    if (!data) return null;
    const docs = data.users.reduce((n, r) => n + r.doc_count, 0);
    const bytes = data.users.reduce((n, r) => n + r.content_bytes, 0);
    const updated = data.users.reduce((n, r) => n + r.updated_in_range, 0);
    return (
      `${plural(docs, 'doc')} · ${formatNumber(data.users.length)} of ` +
      `${plural(data.total_users, 'user')} · ${formatBytes(bytes)} · ` +
      `${formatNumber(updated)} updated in range`
    );
  }, [data]);

  return (
    <FeatureUsageReportFrame
      title="Docs"
      summary={summary}
      summaryTitle="Docs in the system, users owning at least one, total body size, and docs updated in the selected range"
      range={range}
      isRefreshing={isRefreshing}
      onRefresh={() => load()}
      loading={rows === null}
      emptyMessage={rows && rows.length === 0 ? 'No Quest Docs in the system.' : null}
      error={error}
    >
      <table>
        <thead>
          <tr>
            <th className="col-owner">Owner</th>
            <th className="col-w10" title="Docs the user owns now, split into user docs and project docs">
              Docs
            </th>
            <th className="col-w7" title="Docs of public projects">
              Public
            </th>
            <th className="col-w7" title="Docs with at least one share (a user or everyone)">
              Shared
            </th>
            <th
              className="col-w8"
              title="Docs whose latest write came from a model (conversation or approved action request) rather than a person in the UI"
            >
              By model
            </th>
            <th className="col-w9" title="Total size of the doc bodies">
              Size
            </th>
            <th className="col-w7" title="Embedded images across the user's docs">
              Images
            </th>
            <th className="col-w8 col-range" title="Docs created in the selected range">
              Created
            </th>
            <th
              className="col-w8 col-range"
              title="Docs whose latest change (body, rename or share) falls in the selected range; a doc counts once however often it was written"
            >
              Updated
            </th>
            <th className="col-when" title="Most recent change to any of the user's docs">
              Last activity
            </th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => (
            <tr key={row.user_id}>
              <OwnerCell name={row.user_name} email={row.user_email} />
              <td className="col-w10">
                <Count value={row.doc_count} />
                <SubLine parts={splitLine(row)} />
              </td>
              <td className="col-w7">
                <Count value={row.public_doc_count} />
              </td>
              <td className="col-w7">
                <Count value={row.shared_doc_count} />
              </td>
              <td className="col-w8">
                <Count value={row.model_written_count} />
              </td>
              <td className="col-w9" title={`${formatNumber(row.content_bytes)} bytes`}>
                {row.content_bytes === 0 ? (
                  <span className="cell-empty">&mdash;</span>
                ) : (
                  formatBytes(row.content_bytes)
                )}
              </td>
              <td className="col-w7">
                <Count value={row.asset_count} />
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
