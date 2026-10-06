/**
 * Unsaved doc-editor drafts kept in localStorage, so a draft survives what
 * the editor cannot intercept under a plain BrowserRouter (browser back /
 * forward, button-driven navigation, a closed tab) and a doc that vanished
 * while it was being edited (deleted, share revoked).
 *
 * One key per signed-in user and doc: `quest_doc_draft:<user>:<docId>`,
 * where <user> is the URI-encoded, lower-cased sign-in email (GET /me
 * carries no user id). The value is `{content, base, saved_at, title}`:
 * `base` is the concurrency token the draft was edited against, `title` the
 * doc's last known title (for a download after the doc is gone).
 *
 * Every storage access is wrapped: storage can be full, blocked or absent
 * (private windows), and a failed backup must never break editing.
 * Logout clears every draft (clearAllDocDrafts); pruneDocDrafts drops
 * legacy unscoped keys, unreadable values and drafts older than 30 days.
 */

export const DOC_DRAFT_KEY_PREFIX = 'quest_doc_draft:';
export const DOC_DRAFT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

export interface DocDraftBackup {
  content: string;
  /** The concurrency token (`updated_at`) the draft was edited against. */
  base: string;
  /** ISO time of the backup. */
  saved_at: string;
  /** The doc's title when the draft was saved (download file name). */
  title?: string;
}

/** The per-user part of a draft key, or null while signed out. */
export function docDraftScope(userEmail: string | null | undefined): string | null {
  const email = userEmail?.trim().toLowerCase();
  return email ? encodeURIComponent(email) : null;
}

export function docDraftKey(scope: string, docId: string): string {
  return `${DOC_DRAFT_KEY_PREFIX}${scope}:${docId}`;
}

function parseDraft(raw: string | null): DocDraftBackup | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const { content, base, saved_at: savedAt, title } = parsed as Record<string, unknown>;
    if (typeof content !== 'string' || typeof base !== 'string' || typeof savedAt !== 'string') {
      return null;
    }
    return { content, base, saved_at: savedAt, ...(typeof title === 'string' ? { title } : {}) };
  } catch {
    return null;
  }
}

export function readDocDraft(scope: string | null, docId: string): DocDraftBackup | null {
  if (!scope) return null;
  try {
    return parseDraft(localStorage.getItem(docDraftKey(scope, docId)));
  } catch {
    return null;
  }
}

/** Store a draft; false when the browser refused (full / blocked storage). */
export function writeDocDraft(scope: string | null, docId: string, backup: DocDraftBackup): boolean {
  if (!scope) return false;
  try {
    localStorage.setItem(docDraftKey(scope, docId), JSON.stringify(backup));
    return true;
  } catch {
    return false;
  }
}

export function removeDocDraft(scope: string | null, docId: string): void {
  if (!scope) return;
  try {
    localStorage.removeItem(docDraftKey(scope, docId));
  } catch {
    // Blocked storage: nothing to remove.
  }
}

/** Every draft key currently in storage. */
function draftKeys(): string[] {
  const keys: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key?.startsWith(DOC_DRAFT_KEY_PREFIX)) keys.push(key);
    }
  } catch {
    // Blocked storage: no drafts.
  }
  return keys;
}

/** Logout: drop every user's drafts from this browser. */
export function clearAllDocDrafts(): void {
  for (const key of draftKeys()) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Blocked storage.
    }
  }
}

/**
 * Drop legacy unscoped keys (`quest_doc_draft:<docId>`, from before drafts
 * were per user), unreadable values and drafts older than 30 days.
 */
export function pruneDocDrafts(now: number = Date.now()): void {
  for (const key of draftKeys()) {
    try {
      const unscoped = !key.slice(DOC_DRAFT_KEY_PREFIX.length).includes(':');
      const draft = unscoped ? null : parseDraft(localStorage.getItem(key));
      const savedAt = draft ? Date.parse(draft.saved_at) : NaN;
      if (!draft || !(now - savedAt <= DOC_DRAFT_MAX_AGE_MS)) localStorage.removeItem(key);
    } catch {
      // Blocked storage.
    }
  }
}

/** A file name for downloading a draft: the title, minus path-hostile characters. */
export function docDraftFileName(title: string | undefined): string {
  // eslint-disable-next-line no-control-regex
  const base = (title ?? '').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '-').trim().slice(0, 100);
  return `${base || 'doc'}.md`;
}
