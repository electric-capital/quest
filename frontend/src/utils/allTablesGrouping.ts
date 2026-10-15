/**
 * Pure list derivation for Quest Tables lists: the All Tables view
 * (components/tables/TablesListView) -- "Your tables" + "Shared with you" +
 * per-project grouping, project narrowing, newest-updated order, cross-stream
 * dedupe, the scope and count cells -- and the Sidebar Tables blocks
 * (components/sidebar/TablesSection: the 5 newest). Kept free of React so it
 * is unit-tested in allTablesGrouping.test.ts, like utils/allDocsGrouping.ts
 * and utils/sidebarDocs.ts.
 */

import type { Project, TableListRow } from '../api/types';
import { parseUTCTimestamp } from './formatters';

/** How many tables a sidebar Tables block lists. */
export const SIDEBAR_TABLE_LIMIT = 5;

/** Key of the always-present "Your tables" group. */
export const USER_TABLES_GROUP_KEY = 'user';

/** Key of the "Shared with you" group (tables other people shared). */
export const SHARED_TABLES_GROUP_KEY = 'shared';

/** Group key of one project's tables. */
export function projectTablesGroupKey(projectId: string): string {
  return `project:${projectId}`;
}

export type TableGroup = {
  key: string;
  label: string;
  // The project a project group belongs to; null for "Your tables", "Shared
  // with you" and for a project id the loaded project list does not know.
  project: Project | null;
  // Null for "Your tables" and "Shared with you"; set for every project
  // group (known or not).
  projectId: string | null;
  tables: TableListRow[];
};

function timeOf(timestamp: string): number {
  const ms = parseUTCTimestamp(timestamp).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}

/**
 * Newest `updated_at` first, ties broken by id descending (the server's
 * keyset order). Re-sorting keeps a list correct when a refresh or a merge of
 * several streams lands rows in a different order. Never mutates `tables`.
 */
export function sortTablesNewestFirst(tables: TableListRow[]): TableListRow[] {
  return [...tables].sort((a, b) => {
    const byTime = timeOf(b.updated_at) - timeOf(a.updated_at);
    if (byTime !== 0) return byTime;
    // Same millisecond: the raw naive-ISO strings still order microseconds.
    if (a.updated_at !== b.updated_at) return a.updated_at < b.updated_at ? 1 : -1;
    if (a.id === b.id) return 0;
    return a.id < b.id ? 1 : -1;
  });
}

/** The rows a sidebar Tables block shows: the `limit` newest-updated. */
export function deriveSidebarTableItems(
  tables: TableListRow[],
  limit = SIDEBAR_TABLE_LIMIT,
): TableListRow[] {
  return sortTablesNewestFirst(tables).slice(0, Math.max(0, limit));
}

export interface TableGroupsInput {
  /**
   * The All Tables filter (`/tables?project=<id>`); null = unfiltered. While
   * set, the view is that project's tables only: one group fed by
   * `projectTables`; `userTables`, `byProject` and `sharedTables` are ignored.
   */
  projectId: string | null;
  /** useTables(): the user's own user tables (unfiltered view). */
  userTables: TableListRow[];
  /** useTables({projectId}): the filtered project's tables. */
  projectTables?: TableListRow[];
  /** useProjectTablesIndex().byProject (unfiltered view). */
  byProject: Record<string, TableListRow[]>;
  projects: Project[];
  /** useTables({shared: true}) (unfiltered view); omitted = no shared group. */
  sharedTables?: TableListRow[];
}

/**
 * The All Tables groups.
 *
 * Filtered (`projectId` set): exactly one group, labelled "Tables", keyed
 * like that project's group, holding `projectTables` (present even when
 * empty; the view shows its empty line).
 *
 * Unfiltered: "Your tables" first (always present, even when empty), then --
 * when `sharedTables` is passed -- "Shared with you" (present even when
 * empty; the view decides whether an empty one shows), then one group per
 * project id in `byProject` that has at least one table, ordered by project
 * name (case-insensitive, id as the tiebreak). A project id missing from
 * `projects` is labelled "Project".
 *
 * Every group is sorted newest-updated first. A table id appears in one
 * group only: the streams should not overlap (own user tables, own projects'
 * tables, tables shared by others), but a row that moved between streams
 * while they refreshed independently is kept in the first of "Your tables",
 * the project groups (in `byProject` order), "Shared with you".
 */
export function groupTables({
  projectId,
  userTables,
  projectTables = [],
  byProject,
  projects,
  sharedTables,
}: TableGroupsInput): TableGroup[] {
  const projectsById = new Map(projects.map((p) => [p.id, p]));

  if (projectId) {
    return [
      {
        key: projectTablesGroupKey(projectId),
        label: 'Tables',
        project: projectsById.get(projectId) ?? null,
        projectId,
        tables: sortTablesNewestFirst(dedupe(projectTables, new Set())),
      },
    ];
  }

  const seen = new Set<string>();
  const own = sortTablesNewestFirst(dedupe(userTables, seen));
  const projectGroups: TableGroup[] = Object.entries(byProject)
    .map(([id, tables]) => {
      const project = projectsById.get(id) ?? null;
      return {
        key: projectTablesGroupKey(id),
        label: project?.name ?? 'Project',
        project,
        projectId: id,
        tables: sortTablesNewestFirst(dedupe(tables, seen)),
      };
    })
    .filter((group) => group.tables.length > 0);
  projectGroups.sort((a, b) => {
    const byName = a.label.toLowerCase().localeCompare(b.label.toLowerCase());
    if (byName !== 0) return byName;
    return (a.projectId ?? '') < (b.projectId ?? '') ? -1 : 1;
  });

  const groups: TableGroup[] = [
    { key: USER_TABLES_GROUP_KEY, label: 'Your tables', project: null, projectId: null, tables: own },
  ];
  if (sharedTables) {
    groups.push({
      key: SHARED_TABLES_GROUP_KEY,
      label: 'Shared with you',
      project: null,
      projectId: null,
      tables: sortTablesNewestFirst(dedupe(sharedTables, seen)),
    });
  }
  return [...groups, ...projectGroups];
}

/** `tables` minus ids already in `seen` (and repeats); adds the kept ids. */
function dedupe(tables: TableListRow[], seen: Set<string>): TableListRow[] {
  const kept: TableListRow[] = [];
  for (const table of tables) {
    if (seen.has(table.id)) continue;
    seen.add(table.id);
    kept.push(table);
  }
  return kept;
}

/** Display name of whoever shared the table with the viewer. */
export function tableOwnerName(table: TableListRow): string {
  return table.owner?.name || table.owner?.email || 'someone';
}

/**
 * The scope column: "Shared by <owner>" for a table someone shared with the
 * viewer (user or project table alike: the viewer cannot open the owner's
 * project), else the project's name for a project table ("Project" when the
 * project is unknown) and "Your table" for the viewer's own user table.
 */
export function tableScopeLabel(table: TableListRow, project: Project | null): string {
  if (table.shared_with_me) return `Shared by ${tableOwnerName(table)}`;
  if (table.project_id) return project?.name ?? 'Project';
  return 'Your table';
}

/**
 * The scope cell's tooltip: "Shared by <owner> · can edit / can view" for a
 * shared table (the effective share, else the UI write verdict), none
 * otherwise.
 */
export function tableScopeTitle(table: TableListRow): string | undefined {
  if (!table.shared_with_me) return undefined;
  const permission = table.permission ?? (table.access?.can_edit ? 'write' : 'read');
  return `Shared by ${tableOwnerName(table)} · ${permission === 'write' ? 'can edit' : 'can view'}`;
}

/** A count cell: "0", "1,234" (locale grouping); bad input reads "0". */
export function formatTableCount(n: number): string {
  const value = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  return value.toLocaleString();
}

/** "1 row" / "1,234 rows" (the phone meta line, row cell titles). */
export function formatRowCount(n: number): string {
  const label = formatTableCount(n);
  return `${label} ${label === '1' ? 'row' : 'rows'}`;
}

/** "1 column" / "12 columns". */
export function formatColumnCount(n: number): string {
  const label = formatTableCount(n);
  return `${label} ${label === '1' ? 'column' : 'columns'}`;
}
