import { describe, expect, it } from 'vitest';
import type { Doc, Project } from '../api/types';
import {
  SHARED_DOCS_GROUP_KEY,
  USER_DOCS_GROUP_KEY,
  docScopeLabel,
  docScopeTitle,
  filterDocs,
  formatDocSize,
  groupDocs,
  projectGroupKey,
} from './allDocsGrouping';

function doc(overrides: Partial<Doc> & { id: string }): Doc {
  return {
    owner_id: 1,
    project_id: null,
    title: overrides.id,
    description: '',
    mode: 'private',
    content_size: 0,
    asset_count: 0,
    require_approval: false,
    last_write_source: 'ui',
    created_at: '2026-01-01T00:00:00',
    updated_at: '2026-01-01T00:00:00',
    scope: overrides.project_id ? 'project' : 'user',
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free', can_edit: true, can_share: true, can_delete_assets: true, can_require_approval: true },
    ...overrides,
  };
}

function project(id: string, name: string, overrides: Partial<Project> = {}): Project {
  return {
    id,
    user_id: 1,
    name,
    guide: '',
    public: false,
    archived: false,
    created_at: '2026-01-01T00:00:00',
    updated_at: null,
    conversation_count: 0,
    ...overrides,
  };
}

describe('filterDocs', () => {
  const docs = [
    doc({ id: 'a', title: 'Quarterly Plan', description: 'Goals for Q3' }),
    doc({ id: 'b', title: 'Recipes', description: 'Weeknight dinners' }),
    doc({ id: 'c', title: 'Notes' }),
  ];

  it('keeps every doc for an empty or whitespace-only query', () => {
    expect(filterDocs(docs, '')).toEqual(docs);
    expect(filterDocs(docs, '   ')).toEqual(docs);
  });

  it('matches the title case-insensitively', () => {
    expect(filterDocs(docs, 'quarterly').map((d) => d.id)).toEqual(['a']);
    expect(filterDocs(docs, 'NOTES').map((d) => d.id)).toEqual(['c']);
  });

  it('matches the description and trims the query', () => {
    expect(filterDocs(docs, '  dinner ').map((d) => d.id)).toEqual(['b']);
    expect(filterDocs(docs, 'q3').map((d) => d.id)).toEqual(['a']);
  });

  it('returns nothing when no doc matches', () => {
    expect(filterDocs(docs, 'zzz')).toEqual([]);
  });

  it("matches a shared doc's owner name or email, never the viewer's own docs' owner", () => {
    const shared = doc({
      id: 's',
      title: 'Budget',
      owner_id: 2,
      shared_with_me: true,
      owner: { id: 2, name: 'Ana Lopez', email: 'ana@x.test' },
    });
    const own = doc({ id: 'o', title: 'Mine', owner: { id: 2, name: 'Ana Lopez', email: 'ana@x.test' } });
    expect(filterDocs([shared, own], 'lopez').map((d) => d.id)).toEqual(['s']);
    expect(filterDocs([shared, own], 'ANA@X').map((d) => d.id)).toEqual(['s']);
    // Name and email are matched separately.
    expect(filterDocs([shared], 'lopez ana')).toEqual([]);
  });

  it('keeps the input order', () => {
    const shared = [doc({ id: 'x', title: 'Plan B' }), doc({ id: 'y', title: 'Plan A' })];
    expect(filterDocs(shared, 'plan').map((d) => d.id)).toEqual(['x', 'y']);
  });
});

describe('groupDocs', () => {
  const projects = [
    project('p-zeta', 'zeta'),
    project('p-alpha', 'Alpha'),
    project('p-beta', 'beta'),
    project('p-empty', 'Empty'),
  ];

  it('always puts "Your docs" first, even when it is empty', () => {
    const groups = groupDocs([], {}, projects);
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      key: USER_DOCS_GROUP_KEY,
      label: 'Your docs',
      project: null,
      projectId: null,
      docs: [],
    });
  });

  it('orders project groups by name case-insensitively and skips empty projects', () => {
    const groups = groupDocs(
      [doc({ id: 'u1' })],
      {
        'p-zeta': [doc({ id: 'z1', project_id: 'p-zeta' })],
        'p-empty': [],
        'p-beta': [doc({ id: 'b1', project_id: 'p-beta' })],
        'p-alpha': [doc({ id: 'a1', project_id: 'p-alpha' })],
      },
      projects,
    );
    expect(groups.map((g) => g.label)).toEqual(['Your docs', 'Alpha', 'beta', 'zeta']);
    expect(groups[1]).toMatchObject({
      key: projectGroupKey('p-alpha'),
      projectId: 'p-alpha',
      project: projects[1],
    });
    expect(groups[0].docs.map((d) => d.id)).toEqual(['u1']);
  });

  it('labels an unknown project id "Project"', () => {
    const groups = groupDocs([], { gone: [doc({ id: 'g1', project_id: 'gone' })] }, projects);
    expect(groups[1]).toMatchObject({
      key: projectGroupKey('gone'),
      label: 'Project',
      project: null,
      projectId: 'gone',
    });
  });

  it('keeps each group in the order its docs arrived', () => {
    const docs = [
      doc({ id: 'new', project_id: 'p-beta' }),
      doc({ id: 'old', project_id: 'p-beta' }),
    ];
    const groups = groupDocs([], { 'p-beta': docs }, projects);
    expect(groups[1].docs.map((d) => d.id)).toEqual(['new', 'old']);
  });

  it('puts "Shared with you" right after "Your docs" when shared docs are passed', () => {
    const shared = [doc({ id: 's1', owner_id: 2, shared_with_me: true })];
    const groups = groupDocs(
      [doc({ id: 'u1' })],
      { 'p-alpha': [doc({ id: 'a1', project_id: 'p-alpha' })] },
      projects,
      shared,
    );
    expect(groups.map((g) => g.key)).toEqual([
      USER_DOCS_GROUP_KEY,
      SHARED_DOCS_GROUP_KEY,
      projectGroupKey('p-alpha'),
    ]);
    expect(groups[1]).toMatchObject({
      label: 'Shared with you',
      project: null,
      projectId: null,
      docs: shared,
    });
  });

  it('keeps an empty "Shared with you" (the view decides) and omits it when not passed', () => {
    expect(groupDocs([], {}, projects, []).map((g) => g.key)).toEqual([
      USER_DOCS_GROUP_KEY,
      SHARED_DOCS_GROUP_KEY,
    ]);
    expect(groupDocs([], {}, projects).map((g) => g.key)).toEqual([USER_DOCS_GROUP_KEY]);
  });

  it('breaks a name tie by project id', () => {
    const twins = [project('p2', 'Same'), project('p1', 'same')];
    const groups = groupDocs(
      [],
      {
        p2: [doc({ id: 'd2', project_id: 'p2' })],
        p1: [doc({ id: 'd1', project_id: 'p1' })],
      },
      twins,
    );
    expect(groups.slice(1).map((g) => g.projectId)).toEqual(['p1', 'p2']);
  });
});

describe('docScopeLabel', () => {
  const alpha = project('p-alpha', 'Alpha');

  it('names the project for a project doc', () => {
    expect(docScopeLabel(doc({ id: 'a', project_id: 'p-alpha' }), alpha)).toBe('Alpha');
    expect(docScopeLabel(doc({ id: 'a', project_id: 'p-gone' }), null)).toBe('Project');
  });

  it('says "Your doc" for an owned user doc', () => {
    expect(docScopeLabel(doc({ id: 'u' }), null)).toBe('Your doc');
  });

  it('says "Shared by <owner>" for a doc shared with the viewer, project docs included', () => {
    const readerAccess = { can_rename: false, can_switch_mode: false, can_delete: false, write: 'denied' as const, can_edit: false, can_share: false, can_delete_assets: false, can_require_approval: false };
    const shared = doc({
      id: 's',
      owner_id: 2,
      shared_with_me: true,
      permission: 'read',
      owner: { id: 2, name: 'Ana', email: 'ana@x.test' },
      access: readerAccess,
    });
    expect(docScopeLabel(shared, null)).toBe('Shared by Ana');
    expect(docScopeTitle(shared)).toBe('Shared by Ana · can view');

    const sharedProjectDoc = doc({
      id: 'sp',
      owner_id: 3,
      project_id: 'p-theirs',
      shared_with_me: true,
      permission: 'write',
      owner: { id: 3, name: '', email: 'bo@x.test' },
      access: { ...readerAccess, write: 'free', can_edit: true },
    });
    expect(docScopeLabel(sharedProjectDoc, null)).toBe('Shared by bo@x.test');
    expect(docScopeTitle(sharedProjectDoc)).toBe('Shared by bo@x.test · can edit');
  });

  it('has no tooltip for the viewer\'s own docs', () => {
    expect(docScopeTitle(doc({ id: 'u' }))).toBeUndefined();
    expect(docScopeTitle(doc({ id: 'a', project_id: 'p-alpha' }))).toBeUndefined();
  });
});

describe('formatDocSize', () => {
  it('formats bytes, kilobytes and megabytes', () => {
    expect(formatDocSize(0, 0)).toBe('0 B');
    expect(formatDocSize(340, 0)).toBe('340 B');
    expect(formatDocSize(1023, 0)).toBe('1023 B');
    expect(formatDocSize(1024, 0)).toBe('1.0 KB');
    expect(formatDocSize(1229, 0)).toBe('1.2 KB');
    expect(formatDocSize(2 * 1024 * 1024, 0)).toBe('2.0 MB');
  });

  it('rolls over to MB instead of printing 1024.0 KB', () => {
    expect(formatDocSize(1024 * 1024 - 1, 0)).toBe('1.0 MB');
  });

  it('appends the image count with the right plural', () => {
    expect(formatDocSize(1229, 1)).toBe('1.2 KB + 1 image');
    expect(formatDocSize(1229, 3)).toBe('1.2 KB + 3 images');
    expect(formatDocSize(0, 2)).toBe('0 B + 2 images');
  });

  it('treats negative or non-finite inputs as zero', () => {
    expect(formatDocSize(-5, -1)).toBe('0 B');
    expect(formatDocSize(Number.NaN, Number.NaN)).toBe('0 B');
  });
});
