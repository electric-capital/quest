import { describe, expect, it } from 'vitest';
import type { Project, TableListRow } from '../api/types';
import {
  SHARED_TABLES_GROUP_KEY,
  SIDEBAR_TABLE_LIMIT,
  USER_TABLES_GROUP_KEY,
  deriveSidebarTableItems,
  formatColumnCount,
  formatRowCount,
  formatTableCount,
  groupTables,
  projectTablesGroupKey,
  sortTablesNewestFirst,
  tableScopeLabel,
  tableScopeTitle,
} from './allTablesGrouping';

function tableRow(id: string, overrides: Partial<TableListRow> = {}): TableListRow {
  return {
    id,
    owner_id: 1,
    project_id: null,
    title: `Table ${id}`,
    description: '',
    mode: 'private',
    schema_version: 1,
    row_count: 0,
    last_write_source: null,
    require_approval: false,
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    scope: overrides.project_id ? 'project' : 'user',
    column_count: 1,
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    shares: [],
    last_write_user: null,
    access: {
      can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
      can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
      can_require_approval: true, can_download_history: true, deny_reason: null,
      schema_deny_reason: null,
    },
    ...overrides,
  };
}

function project(id: string, name: string, overrides: Partial<Project> = {}): Project {
  return {
    id, user_id: 1, name, guide: '', public: false, archived: false,
    created_at: '2026-01-01T00:00:00', updated_at: null, conversation_count: 0, ...overrides,
  };
}

const ids = (tables: TableListRow[]) => tables.map((t) => t.id);

describe('sortTablesNewestFirst / deriveSidebarTableItems', () => {
  it('orders newest updated_at first, ties by id descending, without mutating', () => {
    const input = [
      tableRow('a', { updated_at: '2026-10-01T00:00:00' }),
      tableRow('b', { updated_at: '2026-10-03T00:00:00' }),
      tableRow('c', { updated_at: '2026-10-02T00:00:00' }),
      tableRow('d', { updated_at: '2026-10-02T00:00:00' }),
      // Same millisecond, later microseconds.
      tableRow('e', { updated_at: '2026-10-01T00:00:00.000500' }),
    ];
    const before = ids(input);
    expect(ids(sortTablesNewestFirst(input))).toEqual(['b', 'd', 'c', 'e', 'a']);
    expect(ids(input)).toEqual(before);
  });

  it('caps the sidebar block at the 5 newest', () => {
    expect(SIDEBAR_TABLE_LIMIT).toBe(5);
    const input = Array.from({ length: 8 }, (_, i) =>
      tableRow(`t${i}`, { updated_at: `2026-10-0${i + 1}T00:00:00` }),
    );
    expect(ids(deriveSidebarTableItems(input))).toEqual(['t7', 't6', 't5', 't4', 't3']);
    expect(deriveSidebarTableItems([])).toEqual([]);
    expect(deriveSidebarTableItems(input, 0)).toEqual([]);
  });
});

describe('groupTables', () => {
  const projects = [project('p1', 'beta'), project('p2', 'Alpha')];

  it('puts Your tables, Shared with you, then projects by name, each newest first', () => {
    const groups = groupTables({
      projectId: null,
      userTables: [
        tableRow('u1', { updated_at: '2026-10-01T00:00:00' }),
        tableRow('u2', { updated_at: '2026-10-05T00:00:00' }),
      ],
      byProject: {
        p1: [tableRow('b1', { project_id: 'p1' })],
        p2: [
          tableRow('a1', { project_id: 'p2', updated_at: '2026-10-01T00:00:00' }),
          tableRow('a2', { project_id: 'p2', updated_at: '2026-10-09T00:00:00' }),
        ],
        p3: [],
      },
      projects,
      sharedTables: [tableRow('s1', { shared_with_me: true })],
    });
    expect(groups.map((g) => [g.key, g.label, ids(g.tables)])).toEqual([
      [USER_TABLES_GROUP_KEY, 'Your tables', ['u2', 'u1']],
      [SHARED_TABLES_GROUP_KEY, 'Shared with you', ['s1']],
      [projectTablesGroupKey('p2'), 'Alpha', ['a2', 'a1']],
      [projectTablesGroupKey('p1'), 'beta', ['b1']],
    ]);
    expect(groups[2].project?.id).toBe('p2');
    expect(groups[2].projectId).toBe('p2');
  });

  it('keeps empty Your tables and Shared with you groups, drops empty projects', () => {
    const groups = groupTables({
      projectId: null, userTables: [], byProject: { p1: [] }, projects, sharedTables: [],
    });
    expect(groups.map((g) => [g.key, g.tables.length])).toEqual([
      [USER_TABLES_GROUP_KEY, 0],
      [SHARED_TABLES_GROUP_KEY, 0],
    ]);
  });

  it('has no shared group when no shared list is passed', () => {
    const groups = groupTables({ projectId: null, userTables: [], byProject: {}, projects });
    expect(groups.map((g) => g.key)).toEqual([USER_TABLES_GROUP_KEY]);
  });

  it('labels an unknown project id "Project"', () => {
    const groups = groupTables({
      projectId: null, userTables: [], byProject: { gone: [tableRow('x', { project_id: 'gone' })] },
      projects,
    });
    expect(groups[1]).toMatchObject({ label: 'Project', project: null, projectId: 'gone' });
  });

  it('narrows to the filtered project: one "Tables" group from projectTables only', () => {
    const groups = groupTables({
      projectId: 'p1',
      userTables: [tableRow('u1')],
      projectTables: [
        tableRow('b1', { project_id: 'p1', updated_at: '2026-10-01T00:00:00' }),
        tableRow('b2', { project_id: 'p1', updated_at: '2026-10-04T00:00:00' }),
      ],
      byProject: { p2: [tableRow('a1', { project_id: 'p2' })] },
      projects,
      sharedTables: [tableRow('s1', { shared_with_me: true })],
    });
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      key: projectTablesGroupKey('p1'), label: 'Tables', projectId: 'p1',
    });
    expect(groups[0].project?.name).toBe('beta');
    expect(ids(groups[0].tables)).toEqual(['b2', 'b1']);
  });

  it('keeps the filtered group even when empty', () => {
    const groups = groupTables({ projectId: 'p9', userTables: [], byProject: {}, projects });
    expect(groups).toEqual([
      { key: projectTablesGroupKey('p9'), label: 'Tables', project: null, projectId: 'p9', tables: [] },
    ]);
  });

  it('shows a table in one group only (own, then projects, then shared)', () => {
    const groups = groupTables({
      projectId: null,
      userTables: [tableRow('x'), tableRow('x')],
      byProject: { p1: [tableRow('x'), tableRow('y', { project_id: 'p1' })] },
      projects,
      sharedTables: [tableRow('y'), tableRow('z', { shared_with_me: true })],
    });
    expect(groups.map((g) => ids(g.tables))).toEqual([['x'], ['z'], ['y']]);
  });
});

describe('scope and count cells', () => {
  const p = project('p1', 'Launch');

  it('labels own, project and shared tables', () => {
    expect(tableScopeLabel(tableRow('a'), null)).toBe('Your table');
    expect(tableScopeLabel(tableRow('b', { project_id: 'p1' }), p)).toBe('Launch');
    expect(tableScopeLabel(tableRow('c', { project_id: 'p1' }), null)).toBe('Project');
    const shared = tableRow('d', {
      project_id: 'p1', shared_with_me: true, permission: 'write',
      owner: { id: 2, name: 'Ana', email: 'ana@example.com' },
    });
    expect(tableScopeLabel(shared, p)).toBe('Shared by Ana');
    expect(tableScopeTitle(shared)).toBe('Shared by Ana · can edit');
    const byEmail = tableRow('e', {
      shared_with_me: true, permission: 'read', owner: { id: 3, name: null, email: 'bo@example.com' },
    });
    expect(tableScopeLabel(byEmail, null)).toBe('Shared by bo@example.com');
    expect(tableScopeTitle(byEmail)).toBe('Shared by bo@example.com · can view');
    expect(tableScopeTitle(tableRow('f'))).toBeUndefined();
  });

  it('formats counts', () => {
    expect(formatTableCount(0)).toBe('0');
    expect(formatTableCount(-3)).toBe('0');
    expect(formatTableCount(Number.NaN)).toBe('0');
    expect(formatTableCount(1234)).toBe((1234).toLocaleString());
    expect(formatRowCount(1)).toBe('1 row');
    expect(formatRowCount(2)).toBe('2 rows');
    expect(formatColumnCount(1)).toBe('1 column');
    expect(formatColumnCount(0)).toBe('0 columns');
  });
});
