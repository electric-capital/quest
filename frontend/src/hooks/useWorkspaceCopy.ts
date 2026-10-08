/**
 * Copy / Move between the two file spaces of a project conversation: its own
 * workspace (the "Chat Files" card) and its project's shared workspace (the
 * "Project Files" card). Supplies the `rowActions` of both cards, the
 * overwrite prompt for a 409 `destination_exists`, and a per-card notice for
 * failures and for a move whose source could not be removed.
 *
 * Only a project conversation has two spaces: with no conversation (the home
 * composer drilled into a project) or no project (a standalone chat) the hook
 * offers no actions at all. Rows refresh from the `file_list_changed` events
 * the copy routes publish for each scope, so nothing is refetched here.
 *
 * A folder copy includes dot-named entries only while the source card shows
 * them (`includeHidden` follows the card's Eye toggle); entries the server
 * skipped are reported in a notice. The conversation scratch roots the
 * server refuses as a copy-to-project source get no to-project actions.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  copyFileFromProject,
  copyFileToProject,
  isDestinationExistsError,
  type FileSource,
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
  path: string;
  name: string;
  isFolder: boolean;
  move: boolean;
  includeHidden: boolean;
}

export interface WorkspaceCopy {
  /** Row actions for the Chat Files card; undefined outside a project conversation. */
  chatRowActions?: (row: FileRowContext) => FileRowAction[];
  /** Row actions for the Project Files card; undefined outside a project conversation. */
  projectRowActions?: (row: FileRowContext) => FileRowAction[];
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
};

const OTHER: Record<CopyCard, CopyCard> = {
  conversation: 'project',
  project: 'conversation',
};

const NO_NOTICES: Record<CopyCard, WorkspaceCopyNotice | null> = {
  conversation: null,
  project: null,
};

function errorMessage(err: unknown): string {
  return err instanceof Error && err.message ? err.message : 'Unknown error';
}

/**
 * Top-level conversation scratch folders the copy-to-project route refuses
 * as a source (`is_scratch_source()` in chat/file_storage.py, mirrored).
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
  name: string,
  move: boolean,
  result: CopyEntryResponse,
): WorkspaceCopyNotice | null {
  const dest = SPACE_LABEL[OTHER[from]];
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
  projectId: string | null,
): WorkspaceCopy {
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

  const enabled = !!conversationId && !!projectId;

  const setNotice = useCallback((card: CopyCard, notice: WorkspaceCopyNotice | null) => {
    setNotices((current) => ({ ...current, [card]: notice }));
  }, []);

  const dismissNotice = useCallback((card: CopyCard) => setNotice(card, null), [setNotice]);

  const runCopy = useCallback(
    (cid: string, from: CopyCard, path: string, move: boolean, includeHidden: boolean, overwrite: boolean) => {
      const call = from === 'conversation' ? copyFileToProject : copyFileFromProject;
      return call(cid, overwrite ? { path, move, includeHidden, overwrite: true } : { path, move, includeHidden });
    },
    [],
  );

  const startCopy = useCallback(
    async (from: CopyCard, row: FileRowContext, move: boolean) => {
      if (!conversationId) return;
      const cid = conversationId;
      const key = `${from}:${row.path}`;
      const name = row.entry.name;
      const includeHidden = row.showHidden;
      setNotice(from, null);
      setInFlight((current) => new Set(current).add(key));
      try {
        const result = await runCopy(cid, from, row.path, move, includeHidden, false);
        if (conversationRef.current !== cid) return;
        setNotice(from, copyResultNotice(from, name, move, result));
      } catch (err) {
        if (conversationRef.current !== cid) return;
        if (isDestinationExistsError(err)) {
          setOverwriteError(null);
          setOverwritePrompt({
            from, path: row.path, name, isFolder: row.entry.type === 'folder', move, includeHidden,
          });
        } else {
          setNotice(from, {
            tone: 'error',
            message: `Could not ${move ? 'move' : 'copy'} "${name}" to ${SPACE_LABEL[OTHER[from]]}: ${errorMessage(err)}`,
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
    [conversationId, runCopy, setNotice],
  );

  const confirmOverwriteAsync = useCallback(async () => {
    if (!conversationId || !overwritePrompt || overwriteBusy) return;
    const cid = conversationId;
    const prompt = overwritePrompt;
    setOverwriteBusy(true);
    setOverwriteError(null);
    try {
      const result = await runCopy(cid, prompt.from, prompt.path, prompt.move, prompt.includeHidden, true);
      if (conversationRef.current !== cid) return;
      setOverwritePrompt(null);
      setNotice(prompt.from, copyResultNotice(prompt.from, prompt.name, prompt.move, result));
    } catch (err) {
      if (conversationRef.current !== cid) return;
      setOverwriteError(errorMessage(err));
    } finally {
      if (conversationRef.current === cid) setOverwriteBusy(false);
    }
  }, [conversationId, overwritePrompt, overwriteBusy, runCopy, setNotice]);

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

  const chatRowActions = useCallback(
    (row: FileRowContext): FileRowAction[] => {
      if (isScratchPath(row.path)) return [];
      const disabled = promptOpen || inFlight.has(`conversation:${row.path}`);
      return [
        { key: 'copy-to-project', label: 'Copy to project', disabled, onSelect: () => void startCopy('conversation', row, false) },
        { key: 'move-to-project', label: 'Move to project', disabled, onSelect: () => void startCopy('conversation', row, true) },
      ];
    },
    [inFlight, promptOpen, startCopy],
  );

  const projectRowActions = useCallback(
    (row: FileRowContext): FileRowAction[] => {
      const disabled = promptOpen || inFlight.has(`project:${row.path}`);
      return [
        { key: 'copy-to-chat', label: 'Copy to chat', disabled, onSelect: () => void startCopy('project', row, false) },
        { key: 'move-to-chat', label: 'Move to chat', disabled, onSelect: () => void startCopy('project', row, true) },
      ];
    },
    [inFlight, promptOpen, startCopy],
  );

  return {
    chatRowActions: enabled ? chatRowActions : undefined,
    projectRowActions: enabled ? projectRowActions : undefined,
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
  const dest = SPACE_LABEL[OTHER[prompt.from]];
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
