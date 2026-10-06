// docDraftBackup: per-user keys, read / write / remove, write failures,
// logout clearing, pruning (legacy unscoped, unreadable, month-old) and the
// download file name.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  clearAllDocDrafts,
  docDraftFileName,
  docDraftKey,
  docDraftScope,
  pruneDocDrafts,
  readDocDraft,
  removeDocDraft,
  writeDocDraft,
} from './docDraftBackup';

const NOW = Date.parse('2026-10-06T12:00:00Z');
const DAY = 24 * 60 * 60 * 1000;

function draft(content: string, savedAt = new Date(NOW).toISOString()) {
  return { content, base: '2026-10-06T10:00:00', saved_at: savedAt, title: 'Roadmap' };
}

describe('docDraftBackup', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('scopes keys per user (lower-cased, URI-encoded email); no scope while signed out', () => {
    const scope = docDraftScope(' Me@Example.com ');
    expect(scope).toBe('me%40example.com');
    expect(docDraftKey(scope!, 'd1')).toBe('quest_doc_draft:me%40example.com:d1');
    expect(docDraftScope(null)).toBeNull();
    expect(docDraftScope('')).toBeNull();
  });

  it('writes, reads and removes one doc draft per user', () => {
    expect(writeDocDraft('me', 'd1', draft('Mine'))).toBe(true);
    expect(readDocDraft('me', 'd1')).toEqual(draft('Mine'));
    expect(readDocDraft('other', 'd1')).toBeNull();
    expect(readDocDraft(null, 'd1')).toBeNull();
    removeDocDraft('me', 'd1');
    expect(readDocDraft('me', 'd1')).toBeNull();
  });

  it('ignores unreadable values', () => {
    localStorage.setItem('quest_doc_draft:me:d1', '{nope');
    expect(readDocDraft('me', 'd1')).toBeNull();
    localStorage.setItem('quest_doc_draft:me:d1', JSON.stringify({ content: 1 }));
    expect(readDocDraft('me', 'd1')).toBeNull();
  });

  it('reports a refused write instead of throwing', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError');
    });
    expect(writeDocDraft('me', 'd1', draft('Mine'))).toBe(false);
    expect(writeDocDraft(null, 'd1', draft('Mine'))).toBe(false);
  });

  it('logout clears every user\'s drafts and nothing else', () => {
    writeDocDraft('me', 'd1', draft('Mine'));
    writeDocDraft('other', 'd2', draft('Theirs'));
    localStorage.setItem('quest_theme', 'dark');
    clearAllDocDrafts();
    expect(readDocDraft('me', 'd1')).toBeNull();
    expect(readDocDraft('other', 'd2')).toBeNull();
    expect(localStorage.getItem('quest_theme')).toBe('dark');
  });

  it('prunes legacy unscoped keys, unreadable values and drafts older than 30 days', () => {
    localStorage.setItem('quest_doc_draft:d1', JSON.stringify(draft('Legacy')));
    localStorage.setItem('quest_doc_draft:me:bad', 'not json');
    writeDocDraft('me', 'old', draft('Old', new Date(NOW - 31 * DAY).toISOString()));
    writeDocDraft('me', 'recent', draft('Recent', new Date(NOW - 29 * DAY).toISOString()));
    pruneDocDrafts(NOW);
    expect(localStorage.getItem('quest_doc_draft:d1')).toBeNull();
    expect(localStorage.getItem('quest_doc_draft:me:bad')).toBeNull();
    expect(readDocDraft('me', 'old')).toBeNull();
    expect(readDocDraft('me', 'recent')?.content).toBe('Recent');
  });

  it('names a download after the title, without path-hostile characters', () => {
    expect(docDraftFileName('Q3 plan: v2/final?')).toBe('Q3 plan- v2-final-.md');
    expect(docDraftFileName('')).toBe('doc.md');
    expect(docDraftFileName(undefined)).toBe('doc.md');
  });
});
