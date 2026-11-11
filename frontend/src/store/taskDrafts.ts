/**
 * Unsent composer text of the Quest Tasks composers, per draft key
 * (TASK_DRAFT_KEY for the New Task page, taskDraftKey(taskId) for a thread;
 * constants/drafts.ts), so leaving a task and coming back keeps what was
 * typed there.
 *
 * Held in memory and mirrored to sessionStorage, so a tab reload keeps the
 * drafts too while other tabs and sessions never see them. Every storage
 * access is guarded: without storage (private mode, quota) the in-memory map
 * still works for the life of the page. An empty text removes the entry.
 */

const STORAGE_KEY = 'quest_task_drafts';

let drafts: Map<string, string> | null = null;

function load(): Map<string, string> {
  if (drafts) return drafts;
  drafts = new Map();
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === 'object') {
      for (const [key, text] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof text === 'string' && text) drafts.set(key, text);
      }
    }
  } catch {
    // Unreadable or unavailable storage: start empty.
  }
  return drafts;
}

function persist(map: Map<string, string>): void {
  try {
    if (map.size === 0) sessionStorage.removeItem(STORAGE_KEY);
    else sessionStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(map)));
  } catch {
    // Quota or unavailable storage: the in-memory map still has the draft.
  }
}

/** The unsent text kept for `draftKey`, or ''. */
export function getTaskDraft(draftKey: string): string {
  return load().get(draftKey) ?? '';
}

/** Keep `text` for `draftKey`; an empty text drops the entry. */
export function setTaskDraft(draftKey: string, text: string): void {
  const map = load();
  if ((map.get(draftKey) ?? '') === text) return;
  if (text) map.set(draftKey, text);
  else map.delete(draftKey);
  persist(map);
}
