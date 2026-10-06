/**
 * Pure helpers for how a doc's sharing state reads in the UI (the viewer
 * header's share chip, the Share dialog, All Docs' "Shared with you" rows).
 */

import type { Doc, DocShare, DocSharePermission } from '../api/types';

/** True when the roster holds the "everyone on this install" grant. */
export function hasEveryoneShare(shares: DocShare[] | undefined): boolean {
  return (shares ?? []).some((share) => share.user_id === null);
}

/** The owner's chip text ("Shared with 2 people" / "Shared with everyone"), or null. */
export function ownerShareSummary(doc: Doc): string | null {
  const shares = doc.shares ?? [];
  if (shares.length === 0) return null;
  if (hasEveryoneShare(shares)) return 'Shared with everyone';
  const n = shares.length;
  return `Shared with ${n} ${n === 1 ? 'person' : 'people'}`;
}

/** Display name of whoever shared the doc with the viewer. */
export function docOwnerName(doc: Doc): string {
  return doc.owner?.name || doc.owner?.email || 'someone';
}

/** A recipient's chip text ("Shared by Ana · Can edit"), or null for the owner. */
export function recipientShareSummary(doc: Doc): string | null {
  if (!doc.shared_with_me) return null;
  return `Shared by ${docOwnerName(doc)} · ${doc.access.can_edit ? 'Can edit' : 'Can view'}`;
}

/** The share chip's tooltip for the owner: every grant, one per line. */
export function shareChipTitle(doc: Doc): string {
  const lines = (doc.shares ?? []).map((share) => {
    const who = share.user_id === null
      ? 'Everyone on this install'
      : share.user?.email || share.user?.name || `User ${share.user_id}`;
    return `${who}: ${share.permission === 'write' ? 'can edit' : 'can view'}`;
  });
  return lines.join('\n');
}

// --- Share dialog + All Docs' "Shared with you" ---------------------------

/** The "everyone on this install" grant, or null. */
export function everyoneShare(shares: DocShare[] | undefined): DocShare | null {
  return (shares ?? []).find((share) => share.user_id === null) ?? null;
}

/** The per-person grants (the roster), in the order the server sent them. */
export function directShares(shares: DocShare[] | undefined): DocShare[] {
  return (shares ?? []).filter((share) => share.user_id !== null);
}

/** "Can view" / "Can edit" (the permission selects' option labels). */
export function permissionLabel(permission: DocSharePermission): string {
  return permission === 'write' ? 'Can edit' : 'Can view';
}

/**
 * The recipient's effective permission: `permission` when the server sent
 * it, else derived from the UI write verdict.
 */
export function recipientPermission(doc: Doc): DocSharePermission {
  return doc.permission ?? (doc.access?.can_edit ? 'write' : 'read');
}

/** All Docs' scope cell for a doc someone shared with the viewer. */
export function sharedByLabel(doc: Doc): string {
  return `Shared by ${docOwnerName(doc)}`;
}

/** Its tooltip: "Shared by Ana · can edit". */
export function sharedByTitle(doc: Doc): string {
  return `${sharedByLabel(doc)} · ${permissionLabel(recipientPermission(doc)).toLowerCase()}`;
}

/**
 * Roster line text for a grant: the name, else the email, else "Deleted
 * user" (the server sends `{id, name: null, email: null}` for an account
 * that is gone).
 */
export function shareDisplayName(share: DocShare): string {
  if (share.user_id === null) return 'Everyone on this install';
  return share.user?.name || share.user?.email || 'Deleted user';
}

/**
 * What sharing does for this doc, as the owner's Share dialog explains it
 * (the shipped access matrix): a private doc's conversation writes need
 * approval once it is shared, while a person with edit access changes it
 * directly (a user doc is always private); recipients' conversations can use
 * a shared user doc but never a project doc; History is for editors only
 * (owner and write shares) and reaches back before the share -- always said,
 * since any grant may become a write grant.
 */
export function shareDialogNotes(doc: Doc): string[] {
  const notes: string[] = [];
  const isPrivate = !doc.project_id || doc.mode === 'private';
  if (isPrivate) {
    notes.push('Once shared, Quest asks for approval before any conversation changes this doc.');
  }
  notes.push(
    doc.project_id
      ? "People you add can open it in Quest; their conversations can't use project docs."
      : 'People you add can open it in Quest and use it in their own conversations.',
  );
  if (isPrivate) {
    notes.push(
      'People with edit access can change it directly in Quest; only changes proposed by conversations need approval.',
    );
  }
  notes.push(
    'People with edit access can also see and restore earlier versions, including ones from before you shared it (kept for 30 days).',
  );
  return notes;
}

/**
 * The text a shared row is also searchable by: its owner's name and email,
 * newline-separated so a (single-line) query never matches across the two;
 * empty for the viewer's own docs.
 */
export function sharedOwnerSearchText(doc: Doc): string {
  if (!doc.shared_with_me || !doc.owner) return '';
  return [doc.owner.name, doc.owner.email].filter(Boolean).join('\n');
}
