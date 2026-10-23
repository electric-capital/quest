/**
 * SkillsReportTable - System Reports "Skills" section.
 *
 * Fetches /admin/system-monitor/skills-report for a selected date range and
 * renders one row per skill that was in the model's context in a
 * conversation written to in range (any tier: auto-loads, composer loads,
 * load_skills / get_skill), or that the model loaded in range: conversations
 * (with the routine share), distinct users, explicit model loads and the
 * newest use. Skill content is never shipped. Frame, fetch cycle and cell
 * helpers come from FeatureUsageReport.tsx.
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

const KIND_LABELS: Record<AdminSkillReportKind, string> = {
  system: 'system',
  private: 'private',
  shared: 'shared',
  public: 'public',
  project: 'project',
  deleted: 'deleted',
};

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

export function SkillsReportTable() {
  const { data, error, isRefreshing, load, range } = useRangedAdminReport(
    fetchAdminSkillsReport,
  );
  const rows = data?.skills ?? null;

  const summary = useMemo(() => {
    if (!data) return null;
    const loads = data.skills.reduce((n, r) => n + r.load_count, 0);
    return (
      `${plural(data.skills.length, 'skill')} · ${formatNumber(data.conversation_count)} of ` +
      `${plural(data.scanned_conversations, 'conversation')} in range · ` +
      `${plural(loads, 'model load')}`
    );
  }, [data]);

  return (
    <FeatureUsageReportFrame
      title="Skills"
      summary={summary}
      summaryTitle="Distinct skills used, how many of the conversations overlapping the range had a skill in context, and explicit load_skills / get_skill calls"
      range={range}
      isRefreshing={isRefreshing}
      onRefresh={() => load()}
      loading={rows === null}
      emptyMessage={rows && rows.length === 0 ? 'No skill use in the selected range.' : null}
      error={error}
    >
      <table>
        <thead>
          <tr>
            <th className="col-owner col-w32">Skill</th>
            <th
              className="col-w14"
              title="Conversations written to in range that had the skill in context, or loaded it"
            >
              Conversations
            </th>
            <th className="col-w14" title="Of those, conversations created by a routine">
              Routine convos
            </th>
            <th className="col-w11" title="Distinct owners of those conversations">
              Users
            </th>
            <th
              className="col-w14"
              title="Explicit load_skills / get_skill calls naming the skill in the selected range (auto-loads and composer loads are not calls)"
            >
              Model loads
            </th>
            <th className="col-when col-w15" title="Most recent activity in a conversation that used the skill">
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
                  <span className={`kind-badge kind-${row.kind}`}>{KIND_LABELS[row.kind]}</span>
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
              </td>
              <WhenCell iso={row.last_used_at} />
            </tr>
          ))}
        </tbody>
      </table>
    </FeatureUsageReportFrame>
  );
}
