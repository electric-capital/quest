/**
 * Review helpers for doc note suggestions, shared by every surface that
 * accepts them (the composer bubble in MemorySuggestions.tsx, the doc
 * viewer's Notes panel, the inbox's Memories tab): the caps as the server
 * counts them, when Accept is allowed and what it sends, and how a row is
 * re-based on the current instructions a 409 `stale_instructions` carries.
 */

import {
  DOC_INSTRUCTIONS_MAX_CHARS,
  DOC_MEMORY_MAX_CHARS,
  type AcceptDocNoteSuggestionOptions,
} from '../api/docsApi';
import type {
  DocNoteKind,
  DocNoteSuggestion,
  MemorySuggestion,
  StaleInstructionsCurrent,
} from '../api/types';

/** A doc note row (it has `note_kind`), not a personal or project memory row. */
export function isDocNoteSuggestion(
  s: MemorySuggestion | DocNoteSuggestion,
): s is DocNoteSuggestion {
  return 'note_kind' in s;
}

/** The server's character cap for one kind of doc note. */
export function docNoteMaxChars(noteKind: DocNoteKind): number {
  return noteKind === 'instructions' ? DOC_INSTRUCTIONS_MAX_CHARS : DOC_MEMORY_MAX_CHARS;
}

/** Characters as the server counts them: code points of the trimmed text. */
export function docNoteCharCount(text: string): number {
  return [...text.trim()].length;
}

/**
 * Whether the row's doc is still there for the user: a list row's
 * `doc_title` is "" when the doc was deleted or is no longer visible.
 */
export function isDocNoteDocAvailable(
  s: DocNoteSuggestion,
): s is DocNoteSuggestion & { doc_title: string } {
  return Boolean(s.doc_title);
}

/**
 * Whether Accept may send this draft (undefined = the proposed text): the
 * doc is available, and the text is not blank (an empty instructions text
 * would clear the doc's instructions) and within the kind's cap.
 */
export function canAcceptDocNote(s: DocNoteSuggestion, draft: string | undefined): boolean {
  if (!isDocNoteDocAvailable(s)) return false;
  const count = docNoteCharCount(draft ?? s.content);
  return count > 0 && count <= docNoteMaxChars(s.note_kind);
}

/**
 * What an Accept sends for this row and draft (undefined = never edited):
 * the trimmed draft as `content` only when it differs from the proposal, and
 * for an instructions row the `current_instructions_version` its diff was
 * rendered against, so the accept is refused (409 `stale_instructions`)
 * rather than overwriting instructions the user has not seen.
 */
export function docNoteAcceptOptions(
  s: DocNoteSuggestion,
  draft: string | undefined,
): AcceptDocNoteSuggestionOptions {
  const opts: AcceptDocNoteSuggestionOptions = {};
  if (draft !== undefined && draft.trim() !== s.content.trim()) {
    opts.content = draft.trim();
  }
  if (s.note_kind === 'instructions' && typeof s.current_instructions_version === 'number') {
    opts.expectedInstructionsVersion = s.current_instructions_version;
  }
  return opts;
}

/**
 * The row re-based on a 409 `stale_instructions`'s `current`: its diff
 * starts from the current text, the next Accept sends the current version,
 * and `instructions_moved` says whether the instructions moved past the
 * version the proposal was written against.
 */
export function withCurrentInstructions(
  s: DocNoteSuggestion,
  current: StaleInstructionsCurrent,
): DocNoteSuggestion {
  return {
    ...s,
    current_instructions: current.instructions,
    current_instructions_version: current.instructions_version,
    instructions_moved:
      s.base_instructions_version !== null
      && s.base_instructions_version < current.instructions_version,
  };
}
