// Unsent AutoProjects composer text, per draft key: kept in memory, mirrored
// to sessionStorage (a reload keeps it), and still working when storage is
// blocked. The store is a module singleton, so each test loads it fresh.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AUTOPROJECT_DRAFT_KEY, autoprojectDraftKey } from '../constants/drafts';

const STORAGE_KEY = 'quest_autoproject_drafts';

async function loadStore() {
  vi.resetModules();
  return import('./autoprojectDrafts');
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});

describe('autoprojectDrafts', () => {
  it('keeps one draft per key: the New AutoProject page and each AutoProject', async () => {
    const store = await loadStore();
    const a = autoprojectDraftKey('ap1');
    const b = autoprojectDraftKey('ap2');
    expect(store.getAutoProjectDraft(a)).toBe('');

    store.setAutoProjectDraft(AUTOPROJECT_DRAFT_KEY, 'a new goal');
    store.setAutoProjectDraft(a, 'next for ap1');
    expect(store.getAutoProjectDraft(AUTOPROJECT_DRAFT_KEY)).toBe('a new goal');
    expect(store.getAutoProjectDraft(a)).toBe('next for ap1');
    expect(store.getAutoProjectDraft(b)).toBe('');
  });

  it('survives a reload through sessionStorage', async () => {
    const first = await loadStore();
    first.setAutoProjectDraft(autoprojectDraftKey('ap1'), 'half typed');
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({
      [autoprojectDraftKey('ap1')]: 'half typed',
    });

    const reloaded = await loadStore();
    expect(reloaded.getAutoProjectDraft(autoprojectDraftKey('ap1'))).toBe('half typed');
  });

  it('drops the entry for an empty text and removes the storage key when none is left', async () => {
    const store = await loadStore();
    const key = autoprojectDraftKey('ap1');
    store.setAutoProjectDraft(key, 'x');
    store.setAutoProjectDraft(key, '');
    expect(store.getAutoProjectDraft(key)).toBe('');
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('ignores unreadable or malformed stored drafts', async () => {
    sessionStorage.setItem(STORAGE_KEY, '{not json');
    const broken = await loadStore();
    expect(broken.getAutoProjectDraft('k')).toBe('');

    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ k: 42, j: '', ok: 'kept' }));
    const mixed = await loadStore();
    expect(mixed.getAutoProjectDraft('k')).toBe('');
    expect(mixed.getAutoProjectDraft('j')).toBe('');
    expect(mixed.getAutoProjectDraft('ok')).toBe('kept');
  });

  it('keeps drafts in memory when storage throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError');
    });
    const store = await loadStore();
    expect(() => store.setAutoProjectDraft('k', 'text')).not.toThrow();
    expect(store.getAutoProjectDraft('k')).toBe('text');
  });

  it('does not write storage when the text is unchanged', async () => {
    const store = await loadStore();
    store.setAutoProjectDraft('k', 'same');
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    store.setAutoProjectDraft('k', 'same');
    expect(setItem).not.toHaveBeenCalled();
  });
});
