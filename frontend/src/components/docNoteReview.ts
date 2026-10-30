/**
 * Review helpers for suggestions, shared by every surface that accepts them
 * (the composer bubble in MemorySuggestions.tsx, the doc viewer's Notes
 * panel, the inbox's Memories tab): the edited text an Accept sends for any
 * kind, when a personal or project memory row may be accepted, and for doc
 * notes the caps as the server counts them, when Accept is allowed and what
 * it sends, and how a row is re-based on the current instructions a 409
 * `stale_instructions` carries.
 */

import {
  DOC_INSTRUCTIONS_MAX_CHARS,
  DOC_MEMORY_MAX_CHARS,
  type AcceptDocNoteSuggestionOptions,
} from '../api/docsApi';
import type {
  DocNoteKind,
  DocNoteSuggestion,
  StaleInstructionsCurrent,
} from '../api/types';

/**
 * The edited text an Accept sends: the trimmed draft once it differs from
 * the proposal, else undefined (the draft is untouched or only re-spaced,
 * so the proposed text is saved as it is). An archive-only memory row
 * (`content` null, a dream's proposal to retire a memory) has no text to
 * edit, so it never sends one.
 */
export function editedText(
  s: { content: string | null },
  draft: string | undefined,
): string | undefined {
  if (s.content === null) return undefined;
  if (draft === undefined || draft.trim() === s.content.trim()) return undefined;
  return draft.trim();
}

/**
 * Whether Accept may send this draft of a personal or project memory row
 * (undefined = the proposed text): an archive-only row (`content` null)
 * always, as it saves no text; any other only while the text is not blank.
 * The row's Accept and every Accept all follow it.
 */
export function canAcceptMemory(
  s: { content: string | null },
  draft: string | undefined,
): boolean {
  return s.content === null || !!(draft ?? s.content).trim();
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
  const content = editedText(s, draft);
  if (content !== undefined) opts.content = content;
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
export function withCurrentInstructions<S extends DocNoteSuggestion>(
  s: S,
  current: StaleInstructionsCurrent,
): S {
  return {
    ...s,
    current_instructions: current.instructions,
    current_instructions_version: current.instructions_version,
    instructions_moved:
      s.base_instructions_version !== null
      && s.base_instructions_version < current.instructions_version,
  };
}

/**
 * Whether the row already carries instructions at least as new as a 409's
 * `current` (a list read that landed after the refused accept), so a
 * re-base on that 409 would roll it back.
 */
export function instructionsCaughtUp(
  s: DocNoteSuggestion,
  current: StaleInstructionsCurrent,
): boolean {
  const listed = s.current_instructions_version;
  return typeof listed === 'number' && listed >= current.instructions_version;
}

/**
 * The row re-based on a 409 `stale_instructions`'s `current`
 * (withCurrentInstructions), or the row itself when it is already as new
 * (instructionsCaughtUp).
 */
export function rebaseOnStale<S extends DocNoteSuggestion>(
  s: S,
  current: StaleInstructionsCurrent,
): S {
  return instructionsCaughtUp(s, current) ? s : withCurrentInstructions(s, current);
}
