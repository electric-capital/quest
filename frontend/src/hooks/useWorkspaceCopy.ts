/**
 * Copy / Move between the two file spaces of a conversation that belongs to
 * a shared space: its own workspace (the "Chat Files" card) and the shared
 * space (`shared`) -- a project conversation's project ("Project Files") or
 * a task run's task ("Task files"). Supplies the `rowActions` of both cards,
 * the overwrite prompt for a 409 `destination_exists`, and a per-card notice
 * for failures and for a move whose source could not be removed.
 *
 * With no conversation (the home composer drilled into a project) or no
 * shared space (a standalone chat) the hook offers no actions at all. Rows
 * refresh from the `file_list_changed` events the copy routes publish for
 * each scope, so nothing is refetched here.
 *
 * A folder copy includes dot-named entries only while the source card shows
 * them (`includeHidden` follows the card's Eye toggle); entries the server
 * skipped are reported in a notice. The conversation scratch roots the
 * server refuses as a source into either shared space get no actions on the
 * Chat Files card.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  copyFileFromProject,
  copyFileFromTask,
  copyFileToProject,
  copyFileToTask,
  isDestinationExistsError,
  type CopyEntryOptions,
  type FileSource,
  type SharedFileSource,
} from '../api/fileApi';
import type { CopyEntryResponse } from '../api/types';
import type { FileRowAction, FileRowContext } from '../components/FileBrowser';

/** Which card a copy starts from (= the source space's kind). */
export type CopyCard = FileSource['kind'];

export interface WorkspaceCopyNotice {
  message: string;
  /** `error`: nothing happened; `warning`: the copy landed but the move did not finish. */
  tone: 'error' | 'warning';
}

/** A copy that hit an existing destination, waiting for the user's overwrite answer. */
export interface OverwritePrompt {
  from: CopyCard;
  to: CopyCard;
  path: string;
  name: string;
  isFolder: boolean;
  move: boolean;
  includeHidden: boolean;
}

export interface WorkspaceCopy {
  /** Row actions for the Chat Files card; undefined without both spaces. */
  chatRowActions?: (row: FileRowContext) => FileRowAction[];
  /** Row actions for the shared space's card (Project Files / Task files); undefined without both spaces. */
  sharedRowActions?: (row: FileRowContext) => FileRowAction[];
  notices: Record<CopyCard, WorkspaceCopyNotice | null>;
  dismissNotice: (card: CopyCard) => void;
  overwritePrompt: OverwritePrompt | null;
  /** True while the overwrite retry is in flight. */
  overwriteBusy: boolean;
  /** Failure of the overwrite retry, shown inside the prompt. */
  overwriteError: string | null;
  confirmOverwrite: () => void;
  cancelOverwrite: () => void;
}

const SPACE_LABEL: Record<CopyCard, string> = {
  conversation: 'Chat Files',
  project: 'Project Files',
  task: 'Task files',
};

/** Row action wording per destination ("Copy to project", "Move to chat"). */
const ACTION_NOUN: Record<CopyCard, string> = {
  conversation: 'chat',
  project: 'project',
  task: 'task',
};

const NO_NOTICES: Record<CopyCard, WorkspaceCopyNotice | null> = {
  conversation: null,
  project: null,
  task: null,
};

type CopyCall = (conversationId: string, options: CopyEntryOptions) => Promise<CopyEntryResponse>;

/** The copy clients of each shared space: into it, and out of it into the conversation. */
const COPY_CALLS: Record<SharedFileSource['kind'], { into: CopyCall; out: CopyCall }> = {
  project: { into: copyFileToProject, out: copyFileFromProject },
  task: { into: copyFileToTask, out: copyFileFromTask },
};

function errorMessage(err: unknown): string {
  return err instanceof Error && err.message ? err.message : 'Unknown error';
}

/**
 * Top-level conversation scratch folders the copy-to-project and
 * copy-to-task routes refuse as a source (`is_scratch_source()` in
 * chat/file_storage.py, mirrored).
 */
const SCRATCH_ROOTS: ReadonlySet<string> = new Set(['.responses', '.subagent_responses', 'pasted']);

/** True when a root-relative path lies in (or is) a conversation scratch root. */
export function isScratchPath(path: string): boolean {
  const first = path.split('/').find((segment) => segment !== '');
  return first !== undefined && SCRATCH_ROOTS.has(first);
}

/**
 * Notice for a copy that landed with a caveat: a move whose source removal
 * failed (`moved: false`), or entries the server skipped (dot-named entries
 * left out of a folder copy, symlinks, special files). Null for a clean copy.
 */
export function copyResultNotice(
  from: CopyCard,
  to: CopyCard,
  name: string,
  move: boolean,
  result: CopyEntryResponse,
): WorkspaceCopyNotice | null {
  const dest = SPACE_LABEL[to];
  const src = SPACE_LABEL[from];
  const skipped = result.skipped > 0
    ? `${result.skipped} hidden or linked ${result.skipped === 1 ? 'entry was' : 'entries were'}`
    : null;
  if (move && !result.moved) {
    return {
      tone: 'warning',
      message: `Copied "${name}" to ${dest}, but it could not be removed from ${src}.`
        + (skipped ? ` ${skipped} not copied.` : ''),
    };
  }
  if (skipped) {
    return {
      tone: 'warning',
      message: move
        ? `Moved "${name}" to ${dest}; ${skipped} left in ${src}.`
        : `Copied "${name}" to ${dest}; ${skipped} not copied.`,
    };
  }
  return null;
}

export function useWorkspaceCopy(
  conversationId: string | null,
  shared: SharedFileSource | null,
): WorkspaceCopy {
  // Primitives for deps: hosts may pass a fresh object every render.
  const sharedKind = shared?.kind ?? null;
  const [notices, setNotices] = useState(NO_NOTICES);
  const [overwritePrompt, setOverwritePrompt] = useState<OverwritePrompt | null>(null);
  const [overwriteBusy, setOverwriteBusy] = useState(false);
  const [overwriteError, setOverwriteError] = useState<string | null>(null);
  // Paths with a copy in flight (keyed `${from}:${path}`): their actions are
  // disabled so a double click cannot start the same copy twice.
  const [inFlight, setInFlight] = useState<ReadonlySet<string>>(new Set());
  // The conversation the current results belong to: a copy that settles after
  // the user switched conversations must not touch the new one's cards.
  const conversationRef = useRef(conversationId);

  useEffect(() => {
    conversationRef.current = conversationId;
    setNotices(NO_NOTICES);
    setOverwritePrompt(null);
    setOverwriteBusy(false);
    setOverwriteError(null);
    setInFlight(new Set());
  }, [conversationId]);

  const enabled = !!conversationId && sharedKind !== null;

  const setNotice = useCallback((card: CopyCard, notice: WorkspaceCopyNotice | null) => {
    setNotices((current) => ({ ...current, [card]: notice }));
  }, []);

  const dismissNotice = useCallback((card: CopyCard) => setNotice(card, null), [setNotice]);

  const runCopy = useCallback(
    (
      cid: string,
      space: SharedFileSource['kind'],
      from: CopyCard,
      path: string,
      move: boolean,
      includeHidden: boolean,
      overwrite: boolean,
    ) => {
      const calls = COPY_CALLS[space];
      const call = from === 'conversation' ? calls.into : calls.out;
      return call(cid, overwrite ? { path, move, includeHidden, overwrite: true } : { path, move, includeHidden });
    },
    [],
  );

  const startCopy = useCallback(
    async (from: CopyCard, row: FileRowContext, move: boolean) => {
      if (!conversationId || !sharedKind) return;
      const cid = conversationId;
      const space = sharedKind;
      const to: CopyCard = from === 'conversation' ? space : 'conversation';
      const key = `${from}:${row.path}`;
      const name = row.entry.name;
      const includeHidden = row.showHidden;
      setNotice(from, null);
      setInFlight((current) => new Set(current).add(key));
      try {
        const result = await runCopy(cid, space, from, row.path, move, includeHidden, false);
        if (conversationRef.current !== cid) return;
        setNotice(from, copyResultNotice(from, to, name, move, result));
      } catch (err) {
        if (conversationRef.current !== cid) return;
        if (isDestinationExistsError(err)) {
          setOverwriteError(null);
          setOverwritePrompt({
            from, to, path: row.path, name, isFolder: row.entry.type === 'folder', move, includeHidden,
          });
        } else {
          setNotice(from, {
            tone: 'error',
            message: `Could not ${move ? 'move' : 'copy'} "${name}" to ${SPACE_LABEL[to]}: ${errorMessage(err)}`,
          });
        }
      } finally {
        if (conversationRef.current === cid) {
          setInFlight((current) => {
            const next = new Set(current);
            next.delete(key);
            return next;
          });
        }
      }
    },
    [conversationId, sharedKind, runCopy, setNotice],
  );

  const confirmOverwriteAsync = useCallback(async () => {
    if (!conversationId || !sharedKind || !overwritePrompt || overwriteBusy) return;
    const cid = conversationId;
    const prompt = overwritePrompt;
    setOverwriteBusy(true);
    setOverwriteError(null);
    try {
      const result = await runCopy(cid, sharedKind, prompt.from, prompt.path, prompt.move, prompt.includeHidden, true);
      if (conversationRef.current !== cid) return;
      setOverwritePrompt(null);
      setNotice(prompt.from, copyResultNotice(prompt.from, prompt.to, prompt.name, prompt.move, result));
    } catch (err) {
      if (conversationRef.current !== cid) return;
      setOverwriteError(errorMessage(err));
    } finally {
      if (conversationRef.current === cid) setOverwriteBusy(false);
    }
  }, [conversationId, sharedKind, overwritePrompt, overwriteBusy, runCopy, setNotice]);

  const confirmOverwrite = useCallback(() => {
    void confirmOverwriteAsync();
  }, [confirmOverwriteAsync]);

  const cancelOverwrite = useCallback(() => {
    if (overwriteBusy) return;
    setOverwritePrompt(null);
    setOverwriteError(null);
  }, [overwriteBusy]);

  // No new copy while the overwrite prompt is up: it would replace the prompt.
  const promptOpen = overwritePrompt !== null;

  // Copy / Move items of a card whose entries go to `to`.
  const copyActions = useCallback(
    (from: CopyCard, to: CopyCard, row: FileRowContext): FileRowAction[] => {
      const disabled = promptOpen || inFlight.has(`${from}:${row.path}`);
      const noun = ACTION_NOUN[to];
      return [
        { key: `copy-to-${noun}`, label: `Copy to ${noun}`, disabled, onSelect: () => void startCopy(from, row, false) },
        { key: `move-to-${noun}`, label: `Move to ${noun}`, disabled, onSelect: () => void startCopy(from, row, true) },
      ];
    },
    [inFlight, promptOpen, startCopy],
  );

  const chatRowActions = useCallback(
    (row: FileRowContext): FileRowAction[] => {
      if (!sharedKind || isScratchPath(row.path)) return [];
      return copyActions('conversation', sharedKind, row);
    },
    [copyActions, sharedKind],
  );

  const sharedRowActions = useCallback(
    (row: FileRowContext): FileRowAction[] => {
      if (!sharedKind) return [];
      return copyActions(sharedKind, 'conversation', row);
    },
    [copyActions, sharedKind],
  );

  return {
    chatRowActions: enabled ? chatRowActions : undefined,
    sharedRowActions: enabled ? sharedRowActions : undefined,
    notices,
    dismissNotice,
    overwritePrompt,
    overwriteBusy,
    overwriteError,
    confirmOverwrite,
    cancelOverwrite,
  };
}

/** Labels for the overwrite prompt (title, body, confirm button). */
export function overwritePromptText(prompt: OverwritePrompt): { title: string; body: string; confirmLabel: string } {
  const dest = SPACE_LABEL[prompt.to];
  const verb = prompt.move ? 'Move' : 'Copy';
  if (prompt.isFolder) {
    return {
      title: `"${prompt.name}" already exists in ${dest}`,
      body: `A folder with this name already exists in ${dest}. ${verb} into it anyway? Files with the same names are replaced; other files in the existing folder are kept.`,
      confirmLabel: `${verb} and replace`,
    };
  }
  return {
    title: `"${prompt.name}" already exists in ${dest}`,
    body: `A file with this name already exists in ${dest}. Replace it?`,
    confirmLabel: `${verb} and replace`,
  };
}
