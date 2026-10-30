// Suggestion review helpers: the edited text an Accept sends for any kind
// (never for an archive-only row); when a memory row may be accepted (an
// archive-only one always, any other unless its text is blank); for doc
// notes what an Accept sends (the edited text only when it changed, the
// instructions version only for an instructions row that has one), the
// re-base on a 409's current instructions (never rolling a newer row back),
// the caps counted like the server (code points of the trimmed text) and
// when Accept is allowed (never for an unavailable doc).
import { describe, expect, it } from 'vitest';
import type { DocNoteSuggestion } from '../api/types';
import {
  canAcceptDocNote,
  canAcceptMemory,
  docNoteAcceptOptions,
  docNoteCharCount,
  docNoteMaxChars,
  editedText,
  instructionsCaughtUp,
  isDocNoteDocAvailable,
  rebaseOnStale,
  withCurrentInstructions,
} from './docNoteReview';

const docNote = (extra: Partial<DocNoteSuggestion> = {}): DocNoteSuggestion => ({
  id: 'dn1',
  user_id: 1,
  doc_id: 'doc-1',
  conversation_id: 'c1',
  note_kind: 'instructions',
  content: 'Prices are in EUR.',
  reasoning: null,
  base_instructions_version: 3,
  replaces_memory_id: null,
  status: 'pending',
  memory_id: null,
  created_at: '2026-10-29T00:00:00Z',
  resolved_at: null,
  doc_title: 'Quarterly plan',
  replaces_content: null,
  current_instructions: 'Prices are in USD.',
  current_instructions_version: 3,
  instructions_moved: false,
  ...extra,
});

const memoryRow = (extra: Partial<DocNoteSuggestion> = {}): DocNoteSuggestion => docNote({
  note_kind: 'memory',
  content: 'The board meets monthly.',
  base_instructions_version: null,
  current_instructions: null,
  current_instructions_version: null,
  ...extra,
});

describe('editedText', () => {
  it('is the trimmed draft once it differs from the proposal', () => {
    expect(editedText({ content: 'Likes tea' }, undefined)).toBeUndefined();
    expect(editedText({ content: 'Likes tea' }, ' Likes tea\n')).toBeUndefined();
    expect(editedText({ content: 'Likes tea' }, ' Likes green tea ')).toBe('Likes green tea');
    expect(editedText({ content: 'Likes tea' }, '   ')).toBe('');
  });

  it('is never sent for an archive-only row, whatever the draft', () => {
    expect(editedText({ content: null }, undefined)).toBeUndefined();
    expect(editedText({ content: null }, 'typed')).toBeUndefined();
  });
});

describe('canAcceptMemory', () => {
  it('accepts the proposed or edited text unless it is blank', () => {
    expect(canAcceptMemory({ content: 'Likes tea' }, undefined)).toBe(true);
    expect(canAcceptMemory({ content: 'Likes tea' }, ' Likes green tea ')).toBe(true);
    expect(canAcceptMemory({ content: 'Likes tea' }, '')).toBe(false);
    expect(canAcceptMemory({ content: 'Likes tea' }, ' \n ')).toBe(false);
    // A blank proposal (never served, but never sent either).
    expect(canAcceptMemory({ content: '  ' }, undefined)).toBe(false);
  });

  it('always accepts an archive-only row, which sends no text', () => {
    expect(canAcceptMemory({ content: null }, undefined)).toBe(true);
    expect(canAcceptMemory({ content: null }, '')).toBe(true);
    expect(canAcceptMemory({ content: null }, '   ')).toBe(true);
  });
});

describe('docNoteAcceptOptions', () => {
  it('sends no content while the draft keeps the proposal', () => {
    expect(docNoteAcceptOptions(memoryRow(), undefined)).toEqual({});
    expect(docNoteAcceptOptions(memoryRow(), '  The board meets monthly.\n')).toEqual({});
  });

  it('sends the trimmed draft once it differs', () => {
    expect(docNoteAcceptOptions(memoryRow(), ' The board meets weekly. '))
      .toEqual({ content: 'The board meets weekly.' });
  });

  it('sends the version an instructions row was diffed against', () => {
    expect(docNoteAcceptOptions(docNote({ current_instructions_version: 7 }), undefined))
      .toEqual({ expectedInstructionsVersion: 7 });
    expect(docNoteAcceptOptions(docNote({ current_instructions_version: 7 }), 'Prices are in GBP.'))
      .toEqual({ content: 'Prices are in GBP.', expectedInstructionsVersion: 7 });
  });

  it('never sends a version for a memory row', () => {
    const opts = docNoteAcceptOptions(memoryRow({ current_instructions_version: 5 }), undefined);
    expect(opts).toEqual({});
    expect('expectedInstructionsVersion' in opts).toBe(false);
  });

  it('omits a null or missing version', () => {
    for (const version of [null, undefined]) {
      const opts = docNoteAcceptOptions(
        docNote({ current_instructions_version: version }),
        undefined,
      );
      expect('expectedInstructionsVersion' in opts).toBe(false);
    }
  });
});

describe('withCurrentInstructions', () => {
  it('re-bases the diff and the accept token on the current instructions', () => {
    const row = docNote();
    const rebased = withCurrentInstructions(row, {
      instructions: 'Prices are in GBP.',
      instructions_version: 4,
    });
    expect(rebased).toEqual({
      ...row,
      current_instructions: 'Prices are in GBP.',
      current_instructions_version: 4,
      instructions_moved: true,
    });
    // The input row is left as it was.
    expect(row.current_instructions).toBe('Prices are in USD.');
    expect(docNoteAcceptOptions(rebased, undefined)).toEqual({ expectedInstructionsVersion: 4 });
  });

  it('is not moved while the proposal base is current, or without a base', () => {
    const current = { instructions: 'Prices are in USD.', instructions_version: 3 };
    expect(withCurrentInstructions(docNote({ instructions_moved: true }), current).instructions_moved)
      .toBe(false);
    expect(withCurrentInstructions(docNote({ base_instructions_version: null }), {
      ...current,
      instructions_version: 9,
    }).instructions_moved).toBe(false);
  });
});

describe('rebaseOnStale', () => {
  const current = { instructions: 'Prices are in GBP.', instructions_version: 4 };

  it("re-bases a row listed before the 409's version", () => {
    const row = docNote({ current_instructions_version: 3 });
    expect(instructionsCaughtUp(row, current)).toBe(false);
    expect(rebaseOnStale(row, current)).toEqual(withCurrentInstructions(row, current));
  });

  it('keeps a row a later read brought to the same or a newer version', () => {
    for (const version of [4, 5]) {
      const row = docNote({ current_instructions: 'Prices are in CHF.', current_instructions_version: version });
      expect(instructionsCaughtUp(row, current)).toBe(true);
      expect(rebaseOnStale(row, current)).toBe(row);
    }
  });

  it('re-bases a row without a listed version', () => {
    for (const version of [null, undefined]) {
      const row = docNote({ current_instructions_version: version });
      expect(instructionsCaughtUp(row, current)).toBe(false);
      expect(rebaseOnStale(row, current).current_instructions_version).toBe(4);
    }
  });

  it("keeps the row's own type", () => {
    const inboxRow = { ...docNote(), kind: 'doc' as const, conversation_title: 'Chat' };
    const rebased = rebaseOnStale(inboxRow, current);
    expect(rebased.kind).toBe('doc');
    expect(rebased.conversation_title).toBe('Chat');
  });
});

describe('caps and guards', () => {
  it('counts code points of the trimmed text against the kind cap', () => {
    expect(docNoteCharCount('  abc \n')).toBe(3);
    expect(docNoteCharCount('😀😀')).toBe(2);
    expect(docNoteMaxChars('instructions')).toBe(4000);
    expect(docNoteMaxChars('memory')).toBe(1000);
  });

  it('accepts a non-blank draft within the cap', () => {
    expect(canAcceptDocNote(memoryRow(), undefined)).toBe(true);
    expect(canAcceptDocNote(memoryRow(), '   ')).toBe(false);
    expect(canAcceptDocNote(memoryRow(), '😀'.repeat(1000))).toBe(true);
    expect(canAcceptDocNote(memoryRow(), '😀'.repeat(1001))).toBe(false);
    expect(canAcceptDocNote(docNote(), 'x'.repeat(4000))).toBe(true);
    expect(canAcceptDocNote(docNote(), 'x'.repeat(4001))).toBe(false);
  });

  it('never accepts for an unavailable doc', () => {
    expect(isDocNoteDocAvailable(docNote())).toBe(true);
    for (const docTitle of ['', undefined]) {
      expect(isDocNoteDocAvailable(memoryRow({ doc_title: docTitle }))).toBe(false);
      expect(canAcceptDocNote(memoryRow({ doc_title: docTitle }), undefined)).toBe(false);
    }
  });
});
