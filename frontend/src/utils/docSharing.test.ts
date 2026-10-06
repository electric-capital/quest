import { describe, expect, it } from 'vitest';
import type { Doc, DocShare } from '../api/types';
import {
  directShares,
  docOwnerName,
  everyoneShare,
  hasEveryoneShare,
  ownerShareSummary,
  permissionLabel,
  recipientPermission,
  recipientShareSummary,
  shareChipTitle,
  shareDialogNotes,
  shareDisplayName,
  sharedByLabel,
  sharedByTitle,
  sharedOwnerSearchText,
} from './docSharing';

const OWNER_ACCESS: Doc['access'] = {
  can_rename: true,
  can_switch_mode: false,
  can_delete: true,
  write: 'free',
  can_edit: true,
  can_share: true,
  can_delete_assets: true,
};

const READER_ACCESS: Doc['access'] = {
  can_rename: false,
  can_switch_mode: false,
  can_delete: false,
  write: 'denied',
  can_edit: false,
  can_share: false,
  can_delete_assets: false,
};

function doc(overrides: Partial<Doc> = {}): Doc {
  return {
    id: 'd1',
    owner_id: 1,
    project_id: null,
    title: 'Plan',
    description: '',
    mode: 'private',
    content_size: 0,
    asset_count: 0,
    last_write_source: null,
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    scope: overrides.project_id ? 'project' : 'user',
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    access: OWNER_ACCESS,
    ...overrides,
  };
}

function share(id: number, userId: number | null, overrides: Partial<DocShare> = {}): DocShare {
  return {
    id,
    user_id: userId,
    permission: 'read',
    created_at: '2026-10-01T00:00:00',
    user: userId === null ? null : { id: userId, name: `User ${userId}`, email: `u${userId}@x.test` },
    ...overrides,
  };
}

const ana = { id: 2, name: 'Ana', email: 'ana@x.test' };

describe('roster helpers', () => {
  const roster = [share(1, 2), share(2, null, { permission: 'write' }), share(3, 3)];

  it('finds the everyone grant', () => {
    expect(everyoneShare(roster)?.id).toBe(2);
    expect(hasEveryoneShare(roster)).toBe(true);
    expect(everyoneShare([share(1, 2)])).toBeNull();
    expect(everyoneShare(undefined)).toBeNull();
    expect(hasEveryoneShare(undefined)).toBe(false);
  });

  it('keeps only the per-person grants, in order', () => {
    expect(directShares(roster).map((s) => s.id)).toEqual([1, 3]);
    expect(directShares(undefined)).toEqual([]);
  });

  it('names a grant by name, then email, then "Deleted user"', () => {
    expect(shareDisplayName(share(1, 2))).toBe('User 2');
    expect(shareDisplayName(share(1, 2, { user: { id: 2, name: '', email: 'e@x.test' } }))).toBe(
      'e@x.test',
    );
    // The server's shape for an account that is gone.
    expect(shareDisplayName(share(1, 9, { user: { id: 9, name: null, email: null } }))).toBe(
      'Deleted user',
    );
    expect(shareDisplayName(share(1, 9, { user: null }))).toBe('Deleted user');
    expect(shareDisplayName(share(1, null))).toBe('Everyone on this install');
  });

  it('labels the permissions', () => {
    expect(permissionLabel('read')).toBe('Can view');
    expect(permissionLabel('write')).toBe('Can edit');
  });
});

describe('owner chip', () => {
  it('summarises the roster', () => {
    expect(ownerShareSummary(doc({ shares: [] }))).toBeNull();
    expect(ownerShareSummary(doc({ shares: [share(1, 2)] }))).toBe('Shared with 1 person');
    expect(ownerShareSummary(doc({ shares: [share(1, 2), share(2, 3)] }))).toBe(
      'Shared with 2 people',
    );
    expect(ownerShareSummary(doc({ shares: [share(1, 2), share(2, null)] }))).toBe(
      'Shared with everyone',
    );
  });

  it('lists every grant in the tooltip', () => {
    const title = shareChipTitle(
      doc({ shares: [share(1, 2, { permission: 'write' }), share(2, null)] }),
    );
    expect(title).toBe('u2@x.test: can edit\nEveryone on this install: can view');
  });
});

describe('recipient labels', () => {
  const reader = doc({ shared_with_me: true, owner: ana, permission: 'read', access: READER_ACCESS });
  const editor = doc({
    shared_with_me: true,
    owner: ana,
    permission: 'write',
    access: { ...READER_ACCESS, write: 'free', can_edit: true },
  });

  it('names the owner, falling back to the email and then "someone"', () => {
    expect(docOwnerName(reader)).toBe('Ana');
    expect(docOwnerName(doc({ owner: { id: 2, name: '', email: 'ana@x.test' } }))).toBe(
      'ana@x.test',
    );
    expect(docOwnerName(doc())).toBe('someone');
  });

  it('builds the viewer chip and the All Docs scope cell', () => {
    expect(recipientShareSummary(reader)).toBe('Shared by Ana · Can view');
    expect(recipientShareSummary(editor)).toBe('Shared by Ana · Can edit');
    expect(recipientShareSummary(doc())).toBeNull();
    expect(sharedByLabel(reader)).toBe('Shared by Ana');
    expect(sharedByTitle(reader)).toBe('Shared by Ana · can view');
    expect(sharedByTitle(editor)).toBe('Shared by Ana · can edit');
  });

  it('derives the permission from the access flags when the row has none', () => {
    expect(recipientPermission(reader)).toBe('read');
    expect(recipientPermission({ ...editor, permission: null })).toBe('write');
    expect(recipientPermission({ ...reader, permission: null })).toBe('read');
  });
});

describe('shareDialogNotes', () => {
  const APPROVAL = 'Once shared, Quest asks for approval before any conversation changes this doc.';
  const DIRECT_EDIT =
    'People with edit access can change it directly in Quest; only changes proposed by conversations need approval.';
  const HISTORY =
    'People with edit access can also see and restore earlier versions, including ones from before you shared it (kept for 30 days).';

  it('explains approval, use in conversations, direct edits and History for a user doc', () => {
    expect(shareDialogNotes(doc())).toEqual([
      APPROVAL,
      'People you add can open it in Quest and use it in their own conversations.',
      DIRECT_EDIT,
      HISTORY,
    ]);
  });

  it("treats a user doc as private whatever its mode says", () => {
    expect(shareDialogNotes(doc({ mode: 'public' }))[0]).toBe(APPROVAL);
  });

  it("says recipients' conversations can't use a private project doc", () => {
    expect(shareDialogNotes(doc({ project_id: 'p1' }))).toEqual([
      APPROVAL,
      "People you add can open it in Quest; their conversations can't use project docs.",
      DIRECT_EDIT,
      HISTORY,
    ]);
  });

  it('has no approval notes for a public project doc', () => {
    expect(shareDialogNotes(doc({ project_id: 'p1', mode: 'public' }))).toEqual([
      "People you add can open it in Quest; their conversations can't use project docs.",
      HISTORY,
    ]);
  });
});

describe('sharedOwnerSearchText', () => {
  it("lists a shared doc's owner name and email on separate lines", () => {
    expect(sharedOwnerSearchText(doc({ shared_with_me: true, owner: ana }))).toBe('Ana\nana@x.test');
    expect(
      sharedOwnerSearchText(doc({ shared_with_me: true, owner: { id: 2, name: null, email: 'a@x.test' } })),
    ).toBe('a@x.test');
  });

  it("is empty for the viewer's own doc", () => {
    expect(sharedOwnerSearchText(doc())).toBe('');
    expect(sharedOwnerSearchText(doc({ owner: ana }))).toBe('');
  });
});
