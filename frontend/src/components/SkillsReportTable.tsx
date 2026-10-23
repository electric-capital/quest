/**
 * SkillsReportTable - System Reports "Skills" section.
 *
 * Fetches /admin/system-monitor/skills-report for a selected date range and
 * renders one row per skill whose body was put in front of the model in
 * range, from the skill_loads ledger: distinct conversations (with the
 * routine share), distinct users, the loads split by how the skill got
 * there (auto-load, composer, model call) and the newest load. Skill
 * content is never shipped. Frame, fetch cycle and cell helpers come from
 * FeatureUsageReport.tsx.
 */

import { useMemo } from 'react';
import { fetchAdminSkillsReport } from '../api/client';
import type { AdminSkillReportKind, AdminSkillsReportRow } from '../api/types';
import { formatNumber } from '../utils/formatters';
import {
  Count,
  FeatureUsageReportFrame,
  SubLine,
  WhenCell,
  plural,
  useRangedAdminReport,
} from './FeatureUsageReport';

const KIND_TITLES: Record<AdminSkillReportKind, string> = {
  system: 'Built-in system skill',
  private: 'User skill visible to its creator only',
  shared: 'User skill shared with specific users',
  public: 'User skill visible to everyone',
  project: 'Project skill',
  deleted: 'Skill no longer in the library',
};

function ownerLabel(row: AdminSkillsReportRow): string | null {
  if (row.kind === 'system' || row.kind === 'deleted') return null;
  if (row.owner_name) return `by ${row.owner_name}`;
  if (row.owner_email) return `by ${row.owner_email}`;
  return 'creator deleted';
}

function loadSplit(row: AdminSkillsReportRow): (string | null)[] {
  return [
    row.autoload_count > 0 ? `${formatNumber(row.autoload_count)} auto` : null,
    row.composer_count > 0 ? `${formatNumber(row.composer_count)} composer` : null,
    row.model_load_count > 0 ? `${formatNumber(row.model_load_count)} model` : null,
    row.backfill_count > 0 ? `${formatNumber(row.backfill_count)} imported` : null,
  ];
}

export function SkillsReportTable() {
  const { data, error, isRefreshing, load, range } = useRangedAdminReport(
    fetchAdminSkillsReport,
  );
  const rows = data?.skills ?? null;

  const summary = useMemo(() => {
    if (!data) return null;
    const imported = data.backfill
      ? ` · ${formatNumber(data.backfill.row_count)} imported from history`
      : ' · history import pending';
    return (
      `${plural(data.skills.length, 'skill')} · ` +
      `${plural(data.conversation_count, 'conversation')} · ` +
      `${plural(data.load_count, 'load')} in range${imported}`
    );
  }, [data]);

  return (
    <FeatureUsageReportFrame
      title="Skills"
      summary={summary}
      summaryTitle="Distinct skills loaded in the selected range, the conversations and loads behind them, and whether the pre-ledger skill_reads.json history has been imported (imported rows are stamped at their conversation's creation time)"
      range={range}
      isRefreshing={isRefreshing}
      onRefresh={() => load()}
      loading={rows === null}
      emptyMessage={rows && rows.length === 0 ? 'No skill loads in the selected range.' : null}
      error={error}
    >
      <table>
        <thead>
          <tr>
            <th className="col-owner col-w32">Skill</th>
            <th
              className="col-w14"
              title="Distinct conversations with at least one load of the skill in range"
            >
              Conversations
            </th>
            <th className="col-w14" title="Of those, conversations created by a routine">
              Routine convos
            </th>
            <th className="col-w11" title="Distinct users whose conversations loaded the skill">
              Users
            </th>
            <th
              className="col-w14"
              title="Times the skill's body was put in front of the model in range: auto-loads count once per turn, composer loads once per send, model calls (load_skills / get_skill / create_skill) once per call; imported rows come from pre-ledger history"
            >
              Loads
            </th>
            <th className="col-when col-w15" title="Newest load of the skill in range">
              Last used
            </th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => (
            <tr key={row.skill_id}>
              <td
                className="col-owner col-w32"
                title={`${KIND_TITLES[row.kind]} · ${row.skill_id}`}
              >
                <span className="owner-cell-name">
                  <span className={`kind-badge kind-${row.kind}`}>{row.kind}</span>
                  {row.name}
                </span>
                <SubLine parts={[ownerLabel(row)]} />
              </td>
              <td className="col-w14">
                <Count value={row.conversation_count} />
              </td>
              <td className="col-w14">
                <Count value={row.routine_conversation_count} />
              </td>
              <td className="col-w11">
                <Count value={row.user_count} />
              </td>
              <td className="col-w14">
                <Count value={row.load_count} />
                <SubLine parts={loadSplit(row)} />
              </td>
              <WhenCell iso={row.last_used_at} />
            </tr>
          ))}
        </tbody>
      </table>
    </FeatureUsageReportFrame>
  );
}
