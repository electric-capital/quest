/**
 * DocEditor -- the doc viewer's body editor (`edit` mode, offered to the
 * owner and to write-share recipients: `access.can_edit`).
 *
 * A toolbar (Image upload, the preview control, Cancel and Save -- also
 * Ctrl/Cmd+S while focus is in the editor or on the bare page) over a
 * monospace textarea and a live preview rendered exactly like DocViewer's
 * body (same remark / rehype plugins, the chat's `markdownComponents`,
 * `assets/<name>` resolved through MarkdownWorkspaceContext.assetBase).
 * Desktop shows the two side by side (stacked when the editor is narrow; the
 * preview can be hidden, remembered per browser); phones switch between
 * Write and Preview. The preview follows the draft 250 ms after typing stops
 * (no asset requests for half-typed names); above ~200 KB it updates only on
 * "Refresh preview".
 *
 * Textarea keys: Tab inserts two spaces, or indents every line a selection
 * touches; Shift+Tab outdents; Escape releases the Tab trap so the next Tab
 * moves focus on (and a read-only textarea never traps Tab). Programmatic
 * edits go through `document.execCommand('insertText')` while the textarea
 * has focus, so the browser's undo keeps working; otherwise they set the
 * value directly.
 *
 * Concurrency token (`base`): the doc's `updated_at` when editing started,
 * sent as `expected_updated_at` on save; `baseContent` is the body that
 * token stands for (unknown after restoring a backup). On a 409 the editor
 * re-reads the doc and tells the viewer (`onStale`): when the current body
 * is still `baseContent` -- a rename or someone's image upload / delete only
 * bumped the token -- it saves once more with the fresh token and no banner
 * (unless an image the draft added is gone: then it says so and keeps the
 * draft); otherwise a conflict banner offers to overwrite (the replaced version
 * stays in History) or to discard and leave. An image upload (the Image
 * button, or pasting / dropping an image file into the textarea) puts an
 * `![Uploading <name>…]()` placeholder at the cursor, swaps in the returned
 * markdown when it lands (appended at the end, with a notice, if the
 * placeholder was deleted meanwhile) and removes it on failure; the
 * response's `updated_at` is adopted -- as the token and as a pending
 * conflict's token -- iff its `previous_updated_at` is the one held. Uploads
 * run one at a time so that chain holds. When the `doc` prop's BODY moves
 * past `base` (someone else wrote), a dirty editor shows a non-blocking
 * "changed" banner (not while an upload or save is in flight); an untouched
 * editor simply follows the new version.
 *
 * Unsaved changes (the draft differs from the body editing started from, or
 * an upload is in flight) are guarded by the browser's leave prompt on
 * unload, and by one "Discard your unsaved changes?" confirm before Cancel,
 * before following an in-app `<a href>` (useUnsavedChangesGuard) and behind
 * both banners' "Discard my changes". LIMITATION: browser back / forward and
 * button-driven navigation (e.g. the Sidebar's rows, which call navigate())
 * cannot be intercepted under a plain BrowserRouter (no useBlocker). Those
 * are covered by a per-user draft backup in localStorage
 * (utils/docDraftBackup.ts), written ~500 ms after the last change and when
 * the editor unmounts or the page is hidden, cleared on save or discard.
 * Opening the editor with a backup that differs from the doc offers "Restore
 * unsaved draft from <time>" (confirmed when the editor already has
 * changes); until the user decides, the offer stays in memory, the key backs
 * up the live draft, and leaving puts the offered draft back. Restoring also
 * restores the backup's `base`, so saving over a doc that has moved on goes
 * through the conflict flow instead of overwriting it.
 */

import {
  memo,
  useCallback,
  useDeferredValue,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent as ReactClipboardEvent,
  type DragEvent as ReactDragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import { Eye, EyeOff, ImagePlus, RefreshCw, TriangleAlert, X } from 'lucide-react';
import {
  docAssetBase,
  fetchDoc,
  isStaleUpdateError,
  staleUpdateCurrent,
  updateDocContent,
  uploadDocAsset,
} from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type { DocContentResponse, DocDetail } from '../../api/types';
import { useAuth } from '../../contexts/AuthContext';
import { useIsMobile } from '../../hooks/useIsMobile';
import { useUnsavedChangesGuard } from '../../hooks/useUnsavedChangesGuard';
import {
  docDraftScope,
  pruneDocDrafts,
  readDocDraft,
  removeDocDraft,
  writeDocDraft,
  type DocDraftBackup,
} from '../../utils/docDraftBackup';
import { formatTimestamp } from '../../utils/formatters';
import remarkMathCurrencyGuard from '../../utils/remarkMathCurrencyGuard';
import { MarkdownWorkspaceContext, markdownComponents } from '../Message';
import { DocConfirmDialog } from './DocConfirmDialog';
import './DocEditor.css';

export interface DocEditorProps {
  doc: DocDetail;
  /** A successful save (row + body as stored). */
  onSaved: (row: DocContentResponse) => void;
  /** Leave the editor without saving (after any discard confirmation). */
  onCancel: () => void;
  /** A save hit a 409: the doc changed on the server, re-fetch it. */
  onStale?: () => void;
}

const INDENT = '  ';
const IMAGE_ACCEPT = 'image/png,image/jpeg,image/gif,image/webp';
/** Server cap on one image (chat/docs/constants.py DOC_MAX_IMAGE_SIZE). */
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const BACKUP_DELAY_MS = 500;
const PREVIEW_DEBOUNCE_MS = 250;
/** Above this many characters the preview updates only on request. */
const LARGE_PREVIEW_CHARS = 200 * 1024;
const PREVIEW_PREF_KEY = 'quest_doc_editor_preview';
const MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta']);

// The same pipeline as DocViewer's body.
const REMARK_PLUGINS = [remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard];
const REHYPE_PLUGINS = [rehypeHighlight, rehypeKatex];

const SAVE_SHORTCUT =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/.test(navigator.userAgent)
    ? '⌘S'
    : 'Ctrl+S';

const FORBIDDEN_MESSAGE = 'You no longer have edit access to this doc.';
const CHANGED_MESSAGE = 'This doc changed since you started editing.';
const DISCARD_TITLE = 'Discard your unsaved changes?';
const BACKUP_FAILED_MESSAGE = "Couldn't back up your draft in this browser";

/** Fallback texts for the upload rejections (the server sends its own). */
const UPLOAD_ERRORS: Record<string, string> = {
  invalid_image: 'Only PNG, JPEG, GIF and WebP images can be added.',
  image_too_large: 'Images can be at most 5 MB.',
  asset_limit: 'This doc has reached its image limit.',
  forbidden: "You can't add images to this doc.",
};

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

function readPreviewShown(): boolean {
  try {
    return localStorage.getItem(PREVIEW_PREF_KEY) !== 'hidden';
  } catch {
    return true;
  }
}

function writePreviewShown(shown: boolean): void {
  try {
    if (shown) localStorage.removeItem(PREVIEW_PREF_KEY);
    else localStorage.setItem(PREVIEW_PREF_KEY, 'hidden');
  } catch {
    // Blocked storage: the choice lasts for this editor only.
  }
}

/** The markdown shown while an image uploads (brackets / breaks dropped from the name). */
function uploadPlaceholder(name: string): string {
  const label = name.replace(/[[\]\r\n]+/g, ' ').trim() || 'image';
  return `![Uploading ${label}…]()`;
}

// --- Text edits --------------------------------------------------------------

/** Replace value[start, end) with `text`, then select [selStart, selEnd). */
interface TextEdit {
  start: number;
  end: number;
  text: string;
  selStart: number;
  selEnd: number;
}

/**
 * The whole lines a selection touches, as [lineStart, blockEnd) (blockEnd
 * excludes the last line's newline). A selection ending right after a
 * newline does not touch the next line.
 */
function selectedLines(value: string, start: number, end: number) {
  const lineStart = start === 0 ? 0 : value.lastIndexOf('\n', start - 1) + 1;
  const last = end > start && value[end - 1] === '\n' ? end - 1 : end;
  const newline = value.indexOf('\n', last);
  return { lineStart, blockEnd: newline === -1 ? value.length : newline };
}

/**
 * Tab: two spaces at a bare cursor; with a selection (one line or many),
 * indent every non-empty line it touches and keep the text selected.
 */
function indentEdit(value: string, start: number, end: number): TextEdit {
  if (start === end) {
    const caret = start + INDENT.length;
    return { start, end, text: INDENT, selStart: caret, selEnd: caret };
  }
  const { lineStart, blockEnd } = selectedLines(value, start, end);
  const lines = value.slice(lineStart, blockEnd).split('\n');
  const indented = lines.map((line) => (line === '' ? line : INDENT + line));
  const added = indented.reduce((sum, line, i) => sum + line.length - lines[i].length, 0);
  const firstAdded = indented[0].length - lines[0].length;
  return {
    start: lineStart,
    end: blockEnd,
    text: indented.join('\n'),
    selStart: start === lineStart ? start : start + firstAdded,
    selEnd: end + added,
  };
}

/** Leading indent Shift+Tab removes from one line: a tab, or up to two spaces. */
function outdentWidth(line: string): number {
  if (line.startsWith('\t')) return 1;
  let width = 0;
  while (width < INDENT.length && line[width] === ' ') width += 1;
  return width;
}

/** Shift+Tab: outdent every line the selection touches; null = nothing to do. */
function outdentEdit(value: string, start: number, end: number): TextEdit | null {
  const { lineStart, blockEnd } = selectedLines(value, start, end);
  const lines = value.slice(lineStart, blockEnd).split('\n');
  const removed = lines.map(outdentWidth);
  if (removed.every((width) => width === 0)) return null;
  // An offset in the old text -> the same spot in the new one (an offset
  // inside a removed indent moves to its line's start).
  const mapOffset = (pos: number): number => {
    let lineAt = lineStart;
    let shift = 0;
    for (let i = 0; i < lines.length && pos >= lineAt; i += 1) {
      shift += Math.min(removed[i], pos - lineAt);
      lineAt += lines[i].length + 1;
    }
    return pos - shift;
  };
  return {
    start: lineStart,
    end: blockEnd,
    text: lines.map((line, i) => line.slice(removed[i])).join('\n'),
    selStart: mapOffset(start),
    selEnd: mapOffset(end),
  };
}

/** A block (image markdown, placeholder) on a line of its own, replacing the selection. */
function blockInsertEdit(value: string, start: number, end: number, block: string): TextEdit {
  const before = start > 0 && value[start - 1] !== '\n' ? '\n' : '';
  const after = end < value.length && value[end] !== '\n' ? '\n' : '';
  const caret = start + before.length + block.length;
  return { start, end, text: before + block + after, selStart: caret, selEnd: caret };
}

/**
 * Replace the first `needle` with `replacement`, keeping the user's
 * selection where it was; an emptied line goes with a removed needle. Null
 * when the needle is gone.
 */
function replaceEdit(
  value: string,
  needle: string,
  replacement: string,
  selStart: number,
  selEnd: number,
): TextEdit | null {
  const at = value.indexOf(needle);
  if (at === -1) return null;
  let start = at;
  let end = at + needle.length;
  if (replacement === '') {
    const ownLine = (start === 0 || value[start - 1] === '\n')
      && (end === value.length || value[end] === '\n');
    if (ownLine && end < value.length) end += 1;
    else if (ownLine && start > 0) start -= 1;
  }
  const delta = replacement.length - (end - start);
  const map = (pos: number) =>
    pos <= start ? pos : pos >= end ? pos + delta : start + replacement.length;
  return { start, end, text: replacement, selStart: map(selStart), selEnd: map(selEnd) };
}

/**
 * Apply an edit to the textarea and return its new value. While the textarea
 * has (or, with `mayFocus`, can take) focus the edit goes through
 * execCommand so it lands on the browser's undo stack; otherwise -- no
 * execCommand, a hidden textarea, focus elsewhere -- the value is set
 * directly.
 */
function applyEdit(textarea: HTMLTextAreaElement, edit: TextEdit, mayFocus: boolean): string {
  const value = textarea.value;
  const expected = value.slice(0, edit.start) + edit.text + value.slice(edit.end);
  if (mayFocus && document.activeElement !== textarea) textarea.focus({ preventScroll: true });
  let applied = false;
  if (document.activeElement === textarea && typeof document.execCommand === 'function') {
    textarea.setSelectionRange(edit.start, edit.end);
    try {
      applied = edit.text === ''
        ? document.execCommand('delete', false)
        : document.execCommand('insertText', false, edit.text);
    } catch {
      applied = false;
    }
  }
  if (!applied || textarea.value !== expected) textarea.value = expected;
  textarea.setSelectionRange(edit.selStart, edit.selEnd);
  return expected;
}

/** Image files among pasted / dropped data (items as a fallback for paste). */
function imageFilesFrom(data: DataTransfer | null): File[] {
  if (!data) return [];
  let files = Array.from(data.files ?? []);
  if (files.length === 0 && data.items) {
    files = Array.from(data.items)
      .filter((item) => item.kind === 'file')
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
  }
  return files.filter((file) => file.type.startsWith('image/'));
}

function carriesFiles(data: DataTransfer | null): boolean {
  return !!data && Array.from(data.types ?? []).includes('Files');
}

/**
 * The `assets/<name>` images a body references. Asset names are
 * `[A-Za-z0-9._-]` and end in an extension, so trailing punctuation of the
 * surrounding prose is dropped (the server's in-use check reads the same).
 */
function assetReferences(body: string): Set<string> {
  const names = new Set<string>();
  for (const match of body.matchAll(/assets\/([A-Za-z0-9._-]+)/g)) {
    const name = match[1].replace(/[._-]+$/, '');
    if (name) names.add(name);
  }
  return names;
}

/**
 * Images this draft added (referenced now, not in the body it started from)
 * that the doc no longer has -- e.g. deleted by the owner meanwhile.
 */
function missingAddedAssets(draft: string, baseBody: string, doc: DocDetail): string[] {
  const before = assetReferences(baseBody);
  const present = new Set(doc.assets.map((asset) => asset.name));
  return [...assetReferences(draft)].filter((name) => !before.has(name) && !present.has(name));
}

/** Ctrl/Cmd+S by `key`, or by `code` on a layout whose key is not a Latin letter. */
function isSaveShortcut(event: KeyboardEvent): boolean {
  if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return false;
  const key = event.key ?? '';
  if (key === 's' || key === 'S') return true;
  return !/^[a-z]$/i.test(key) && event.code === 'KeyS';
}

// --- Preview -----------------------------------------------------------------

/** The rendered draft; memoized so it only re-renders for a new preview text. */
const DocMarkdownPreview = memo(function DocMarkdownPreview({ content }: { content: string }) {
  if (content.trim() === '') {
    return <p className="doc-editor-preview-empty">Nothing to preview yet.</p>;
  }
  return (
    // DocViewer.css's body typography (the chat markdown look, flat).
    <div className="message-content doc-viewer-markdown">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});

// --- Editor ------------------------------------------------------------------

/** What the shared "discard" confirm is open for. */
type ConfirmAction =
  | { kind: 'cancel' }
  | { kind: 'link'; to: string }
  | { kind: 'reload' }
  | { kind: 'conflict' }
  | { kind: 'restore' };

type PendingBackup =
  | { kind: 'live'; content: string; base: string }
  | { kind: 'offer'; backup: DocDraftBackup }
  | { kind: 'remove' };

export function DocEditor({ doc, onSaved, onCancel, onStale }: DocEditorProps) {
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const { userEmail } = useAuth();
  const scope = docDraftScope(userEmail);
  const docId = doc.id;
  const hintId = useId();

  const rootRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // The body editing started from (dirty = the draft differs from it), the
  // concurrency token the draft is edited against, and the server body that
  // token stands for (null = unknown: a restored backup's).
  const [startContent, setStartContent] = useState(doc.content);
  const [draft, setDraft] = useState(doc.content);
  const [base, setBase] = useState(doc.updated_at);
  const [baseContent, setBaseContent] = useState<string | null>(doc.content);
  const dirty = draft !== startContent;

  // A backup left by an earlier session that never saved (back button,
  // closed tab, sidebar navigation), read once and offered until the user
  // decides. Old and legacy backups are pruned first.
  const [restoreOffer, setRestoreOffer] = useState<DocDraftBackup | null>(() => {
    pruneDocDrafts();
    const backup = readDocDraft(scope, doc.id);
    return backup && backup.content !== doc.content ? backup : null;
  });

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  // The server's current token from a 409, while the conflict banner shows.
  const [conflictToken, setConflictToken] = useState<string | null>(null);
  // The `updated_at` the user chose "Keep editing" for.
  const [keptUpdatedAt, setKeptUpdatedAt] = useState<string | null>(null);
  const [uploading, setUploading] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmAction | null>(null);
  const [previewShown, setPreviewShown] = useState(readPreviewShown);
  const [phonePane, setPhonePane] = useState<'write' | 'preview'>('write');
  const [previewText, setPreviewText] = useState(doc.content);
  const [dragOver, setDragOver] = useState(false);
  const [backupFailed, setBackupFailed] = useState(false);

  const savingRef = useRef(false);
  // Set once the editor is done (saved or discarded): no further backups.
  const finishedRef = useRef(false);
  // Escape was pressed: the next Tab moves focus instead of indenting.
  const tabReleasedRef = useRef(false);
  // Uploads run one after another (see the token note in the docstring).
  const uploadChainRef = useRef<Promise<void>>(Promise.resolve());
  // Latest values for callbacks that outlive a render.
  const restoreOfferRef = useRef(restoreOffer);
  restoreOfferRef.current = restoreOffer;
  const titleRef = useRef(doc.title);
  titleRef.current = doc.title;
  const onStaleRef = useRef(onStale);
  onStaleRef.current = onStale;

  // Someone else wrote while this editor had no changes: follow the new
  // version (string order is time order for these equal-shape ISO tokens).
  if (!dirty && !saving && doc.updated_at > base) {
    setBase(doc.updated_at);
    setStartContent(doc.content);
    setBaseContent(doc.content);
    setDraft(doc.content);
    setConflictToken(null);
    setKeptUpdatedAt(null);
  }

  // A 403 on save, or a re-fetched row that no longer grants editing (a
  // share revoked or downgraded while the editor was open).
  const noAccess = forbidden || !doc.access.can_edit;
  const editable = !saving && !noAccess;
  const busy = saving || uploading > 0;
  const canSave = dirty && !busy && !noAccess;
  // The banners only matter while there is a draft to lose; "changed" means
  // a new BODY (a rename or an image only bumps the token), and waits while
  // this editor's own upload or save is in flight.
  const showConflict = conflictToken !== null && dirty && !noAccess;
  const showChanged =
    dirty
    && !busy
    && !showConflict
    && doc.updated_at > base
    && doc.content !== baseContent
    && doc.updated_at !== keptUpdatedAt;

  // --- Draft backup ----------------------------------------------------------

  // What the next flush writes; undefined = nothing pending.
  const pendingBackupRef = useRef<PendingBackup | undefined>(undefined);

  const flushBackup = useCallback(() => {
    const pending = pendingBackupRef.current;
    pendingBackupRef.current = undefined;
    if (!pending || finishedRef.current) return;
    if (pending.kind === 'remove') {
      removeDocDraft(scope, docId);
      return;
    }
    const backup: DocDraftBackup = pending.kind === 'offer'
      ? pending.backup
      : {
          content: pending.content,
          base: pending.base,
          saved_at: new Date().toISOString(),
          title: titleRef.current,
        };
    const stored = writeDocDraft(scope, docId, backup);
    if (pending.kind === 'live') setBackupFailed(!stored);
  }, [scope, docId]);

  // The key backs up the live draft; while clean it holds the undecided
  // offer (if any), else nothing.
  useEffect(() => {
    if (finishedRef.current) return;
    if (dirty) pendingBackupRef.current = { kind: 'live', content: draft, base };
    else if (restoreOffer) pendingBackupRef.current = { kind: 'offer', backup: restoreOffer };
    else pendingBackupRef.current = { kind: 'remove' };
    const timer = window.setTimeout(flushBackup, BACKUP_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [draft, base, dirty, restoreOffer, flushBackup]);

  // Write a pending backup right away when the page is hidden or goes away
  // (after the leave prompt) or the editor unmounts (back button, sidebar
  // navigation, the doc vanishing).
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') flushBackup();
    };
    window.addEventListener('pagehide', flushBackup);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('pagehide', flushBackup);
      document.removeEventListener('visibilitychange', onVisibility);
      flushBackup();
    };
  }, [flushBackup]);

  /**
   * Saved or discarded: stop backing up and drop this session's draft. An
   * undecided restore offer is put back for next time.
   */
  const finish = useCallback(() => {
    finishedRef.current = true;
    pendingBackupRef.current = undefined;
    const offer = restoreOfferRef.current;
    if (offer) writeDocDraft(scope, docId, offer);
    else removeDocDraft(scope, docId);
  }, [scope, docId]);

  // --- Save ------------------------------------------------------------------

  const save = useCallback(
    async (token: string) => {
      if (savingRef.current) return;
      savingRef.current = true;
      setSaving(true);
      setSaveError(null);
      const content = draft;
      const put = (expected: string) =>
        updateDocContent(docId, { content, expected_updated_at: expected });
      const showStale = (err: unknown, current: string | null) => {
        if (current) setConflictToken(current);
        else setSaveError(errorMessage(err, 'This doc changed while you were editing.'));
      };
      try {
        let row: DocContentResponse;
        try {
          row = await put(token);
        } catch (err) {
          if (!isStaleUpdateError(err)) throw err;
          // A rename or someone's image upload / delete bumps the token but
          // leaves the body alone: when the current body is still the one
          // this draft started from, save over it once with the new token.
          let fetched: DocDetail | null = null;
          try {
            fetched = await fetchDoc(docId);
          } catch {
            fetched = null;
          }
          onStaleRef.current?.();
          if (!fetched || baseContent === null || fetched.content !== baseContent) {
            showStale(err, fetched?.updated_at ?? staleUpdateCurrent(err)?.updated_at ?? null);
            return;
          }
          // The bump may have been the deletion of an image this draft adds
          // (the owner may delete one the saved body does not use yet): never
          // save that broken reference silently. The token is kept, so the
          // next save runs this check again.
          const missing = missingAddedAssets(content, baseContent, fetched);
          if (missing.length > 0) {
            setSaveError(
              `An image you added was deleted meanwhile: ${missing.join(', ')}. `
              + 'Re-upload it or remove the reference, then save.',
            );
            return;
          }
          setBase(fetched.updated_at);
          row = await put(fetched.updated_at);
        }
        finish();
        onSaved(row);
      } catch (err) {
        if (isStaleUpdateError(err)) {
          // The one retry lost a race as well.
          onStaleRef.current?.();
          showStale(err, staleUpdateCurrent(err)?.updated_at ?? null);
        } else if (err instanceof ApiClientError && err.statusCode === 403) {
          setForbidden(true);
          setConflictToken(null);
        } else {
          setSaveError(errorMessage(err, 'Failed to save the doc.'));
        }
      } finally {
        savingRef.current = false;
        setSaving(false);
      }
    },
    [docId, draft, baseContent, finish, onSaved],
  );

  // Ctrl/Cmd+S while focus is in the editor or on the bare page -- not in
  // another field (the header's rename input) and not under a dialog.
  const saveShortcutRef = useRef<() => void>(() => {});
  saveShortcutRef.current = () => {
    if (canSave) void save(base);
  };
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!isSaveShortcut(event)) return;
      if (document.querySelector('[role="dialog"]')) return;
      const target = event.target;
      const inEditor = target instanceof Node && !!rootRef.current?.contains(target);
      const onPage =
        target === document.body || target === document.documentElement || target === document;
      if (!inEditor && !onPage) return;
      event.preventDefault();
      saveShortcutRef.current();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  // --- Leaving and the shared confirm ------------------------------------------

  useUnsavedChangesGuard({
    when: dirty || uploading > 0,
    onBlockedNavigation: (to) => setConfirm({ kind: 'link', to }),
  });

  const handleCancel = () => {
    if (saving) return;
    if (dirty || uploading > 0) {
      setConfirm({ kind: 'cancel' });
      return;
    }
    finish();
    onCancel();
  };

  const applyRestore = () => {
    const offer = restoreOfferRef.current;
    if (!offer) return;
    setDraft(offer.content);
    setBase(offer.base);
    // The body behind that token is unknown: no silent re-save on a 409.
    setBaseContent(null);
    setKeptUpdatedAt(null);
    setConflictToken(null);
    setRestoreOffer(null);
  };

  /** The changed banner's discard: start over from the latest version. */
  const reloadLatest = () => {
    setDraft(doc.content);
    setStartContent(doc.content);
    setBaseContent(doc.content);
    setBase(doc.updated_at);
    setKeptUpdatedAt(null);
    setConflictToken(null);
    setSaveError(null);
  };

  const runConfirm = () => {
    const action = confirm;
    setConfirm(null);
    if (!action) return;
    if (action.kind === 'restore') {
      applyRestore();
    } else if (action.kind === 'reload') {
      reloadLatest();
    } else {
      finish();
      // Leave edit mode even when the link keeps this doc on screen.
      onCancel();
      if (action.kind === 'link') navigate(action.to);
    }
  };

  const handleRestore = () => {
    if (dirty) setConfirm({ kind: 'restore' });
    else applyRestore();
  };

  const discardOffer = () => {
    setRestoreOffer(null);
    // While clean the key still holds the offered draft; a dirty editor's
    // key already holds the live one.
    if (!dirty) removeDocDraft(scope, docId);
  };

  // --- Images ------------------------------------------------------------------

  // Take focus back from the editor's own toolbar (keeps undo), but never
  // from an input elsewhere, nor on a phone (it would pop the keyboard).
  const mayFocusTextarea = useCallback(() => {
    const active = document.activeElement;
    return !isMobile
      && (active === null || active === document.body || !!rootRef.current?.contains(active));
  }, [isMobile]);

  const insertBlock = useCallback(
    (block: string) => {
      const textarea = textareaRef.current;
      if (!textarea || finishedRef.current) return;
      const { value, selectionStart, selectionEnd } = textarea;
      const edit = blockInsertEdit(value, selectionStart, selectionEnd, block);
      setDraft(applyEdit(textarea, edit, mayFocusTextarea()));
    },
    [mayFocusTextarea],
  );

  /** Swap an upload placeholder for `replacement`; false when it was deleted. */
  const replacePlaceholder = useCallback(
    (placeholder: string, replacement: string): boolean => {
      const textarea = textareaRef.current;
      if (!textarea || finishedRef.current) return false;
      const { value, selectionStart, selectionEnd } = textarea;
      const edit = replaceEdit(value, placeholder, replacement, selectionStart, selectionEnd);
      if (!edit) return false;
      setDraft(applyEdit(textarea, edit, mayFocusTextarea()));
      return true;
    },
    [mayFocusTextarea],
  );

  const appendBlock = useCallback(
    (block: string) => {
      const textarea = textareaRef.current;
      if (!textarea || finishedRef.current) return;
      const { value, selectionStart, selectionEnd } = textarea;
      const separator = value === '' || value.endsWith('\n') ? '' : '\n';
      const edit = {
        start: value.length,
        end: value.length,
        text: separator + block,
        selStart: selectionStart,
        selEnd: selectionEnd,
      };
      setDraft(applyEdit(textarea, edit, mayFocusTextarea()));
    },
    [mayFocusTextarea],
  );

  const uploadImages = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      setUploadError(null);
      setUploadNotice(null);
      for (const file of files) {
        const name = file.name || 'image';
        if (file.size > MAX_IMAGE_BYTES) {
          setUploadError(`Could not add ${name}: ${UPLOAD_ERRORS.image_too_large}`);
          continue;
        }
        const placeholder = uploadPlaceholder(name);
        insertBlock(placeholder);
        setUploading((count) => count + 1);
        uploadChainRef.current = uploadChainRef.current.then(async () => {
          try {
            const result = await uploadDocAsset(docId, file);
            // The upload bumped the row. Adopt the new token -- also for a
            // pending conflict -- only when it was taken over the one held;
            // otherwise someone else wrote first.
            const adopt = (held: string) =>
              held === result.previous_updated_at ? result.updated_at : held;
            setBase(adopt);
            setConflictToken((held) => (held === null ? null : adopt(held)));
            if (!replacePlaceholder(placeholder, result.markdown)) {
              appendBlock(result.markdown);
              setUploadNotice(
                `${name} was added at the end of the doc because its placeholder was removed.`,
              );
            }
          } catch (err) {
            replacePlaceholder(placeholder, '');
            const code = err instanceof ApiClientError ? err.errorCode ?? '' : '';
            const message = errorMessage(err, UPLOAD_ERRORS[code] ?? 'Failed to upload the image.');
            setUploadError(`Could not add ${name}: ${message}`);
          } finally {
            setUploading((count) => count - 1);
          }
        });
      }
    },
    [docId, insertBlock, replacePlaceholder, appendBlock],
  );

  const handleFilePicked = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    // Picking the same file again must fire `change` again.
    event.target.value = '';
    uploadImages(files);
  };

  const handlePaste = (event: ReactClipboardEvent<HTMLTextAreaElement>) => {
    if (!editable) return;
    const files = imageFilesFrom(event.clipboardData);
    if (files.length === 0) return;
    // Copying from an office app puts both the text and a picture of it on
    // the clipboard: the text is what the user meant to paste.
    if (event.clipboardData.getData('text/plain')) return;
    event.preventDefault();
    uploadImages(files);
  };

  const handleDragOver = (event: ReactDragEvent<HTMLTextAreaElement>) => {
    if (!editable || !carriesFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    if (!dragOver) setDragOver(true);
  };

  const handleDrop = (event: ReactDragEvent<HTMLTextAreaElement>) => {
    setDragOver(false);
    if (!editable || !carriesFiles(event.dataTransfer)) return;
    // Never let the browser open a dropped file in place of the app.
    event.preventDefault();
    const files = imageFilesFrom(event.dataTransfer);
    if (files.length === 0) {
      setUploadError(UPLOAD_ERRORS.invalid_image);
      return;
    }
    uploadImages(files);
  };

  // --- Textarea keys -------------------------------------------------------------

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === 'Escape') {
      tabReleasedRef.current = true;
      return;
    }
    if (event.key !== 'Tab' || event.ctrlKey || event.altKey || event.metaKey) {
      if (!MODIFIER_KEYS.has(event.key)) tabReleasedRef.current = false;
      return;
    }
    if (tabReleasedRef.current) {
      // Escape, then Tab: let focus move on (no keyboard trap).
      tabReleasedRef.current = false;
      return;
    }
    // Read-only (saving, no access): Tab moves focus as usual.
    if (!editable) return;
    event.preventDefault();
    const textarea = event.currentTarget;
    const { value, selectionStart, selectionEnd } = textarea;
    const edit = event.shiftKey
      ? outdentEdit(value, selectionStart, selectionEnd)
      : indentEdit(value, selectionStart, selectionEnd);
    if (edit) setDraft(applyEdit(textarea, edit, true));
  };

  // --- Preview -----------------------------------------------------------------

  const showInput = !isMobile || phonePane === 'write';
  const showPreview = isMobile ? phonePane === 'preview' : previewShown;
  const split = !isMobile && previewShown;
  const largeDraft = draft.length > LARGE_PREVIEW_CHARS;

  // Follow the draft 250 ms after typing stops (small docs only).
  useEffect(() => {
    if (!showPreview || largeDraft || previewText === draft) return;
    const timer = window.setTimeout(() => setPreviewText(draft), PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [draft, previewText, showPreview, largeDraft]);

  const deferredPreview = useDeferredValue(previewText);
  const markdownCtx = useMemo(() => ({ assetBase: docAssetBase(docId) }), [docId]);
  const previewStale = largeDraft && previewText !== draft;

  const togglePreview = () => {
    const next = !previewShown;
    setPreviewShown(next);
    writePreviewShown(next);
    if (next) setPreviewText(draft);
  };

  const showPhonePane = (pane: 'write' | 'preview') => {
    setPhonePane(pane);
    if (pane === 'preview') setPreviewText(draft);
  };

  // --- Render --------------------------------------------------------------------

  let status = '';
  if (uploading > 0) status = uploading === 1 ? 'Uploading image...' : `Uploading ${uploading} images...`;
  else if (dirty && backupFailed) status = BACKUP_FAILED_MESSAGE;
  else if (dirty && !saving) status = 'Unsaved changes';

  const restoreTime = restoreOffer ? formatTimestamp(restoreOffer.saved_at) : '';
  // Banners without role=alert are announced through one always-present
  // polite region (a region inserted with its text is often not read).
  let announcement = '';
  if (showChanged) announcement = CHANGED_MESSAGE;
  else if (restoreOffer) announcement = `Restore unsaved draft from ${restoreTime}?`;
  else if (uploadNotice) announcement = uploadNotice;

  let confirmTitle = DISCARD_TITLE;
  let confirmLabel = 'Discard changes';
  let confirmBody = 'Your edits to this doc have not been saved.';
  if (confirm?.kind === 'restore') {
    confirmTitle = 'Replace your changes with the saved draft?';
    confirmLabel = 'Restore draft';
    confirmBody = `What you typed since opening the editor is replaced by the draft from ${restoreTime}.`;
  } else if (confirm?.kind === 'reload') {
    confirmBody = 'The editor then shows the latest version of this doc.';
  } else if (confirm?.kind === 'conflict') {
    confirmBody = 'You leave the editor and the doc keeps its current version.';
  } else if (uploading > 0) {
    confirmBody = 'Your edits to this doc have not been saved, and an image is still uploading.';
  }

  return (
    <div ref={rootRef} className={`doc-editor${isMobile ? ' doc-editor--phone' : ''}`}>
      <div className="doc-editor-toolbar">
        <div className="doc-editor-toolbar-group">
          {isMobile && (
            <div className="doc-editor-segmented" role="group" aria-label="Editor view">
              <button
                type="button"
                className="doc-editor-segment"
                aria-pressed={phonePane === 'write'}
                onClick={() => showPhonePane('write')}
              >
                Write
              </button>
              <button
                type="button"
                className="doc-editor-segment"
                aria-pressed={phonePane === 'preview'}
                onClick={() => showPhonePane('preview')}
              >
                Preview
              </button>
            </div>
          )}
          <button
            type="button"
            className="doc-editor-button"
            onClick={() => fileInputRef.current?.click()}
            disabled={!editable}
            title="Add an image (PNG, JPEG, GIF or WebP, up to 5 MB)"
            aria-label="Add image"
          >
            <ImagePlus size={16} aria-hidden="true" />
            {!isMobile && <span>Image</span>}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept={IMAGE_ACCEPT}
            multiple
            hidden
            onChange={handleFilePicked}
          />
          {!isMobile && (
            <button
              type="button"
              className="doc-editor-button doc-editor-toggle"
              aria-pressed={previewShown}
              onClick={togglePreview}
              title={previewShown ? 'Hide the preview' : 'Show the preview'}
            >
              {previewShown ? <Eye size={16} aria-hidden="true" /> : <EyeOff size={16} aria-hidden="true" />}
              <span>Preview</span>
            </button>
          )}
        </div>
        <span className="doc-editor-status" role="status">
          {status}
        </span>
        <div className="doc-editor-toolbar-group doc-editor-toolbar-end">
          <button
            type="button"
            className="doc-editor-button"
            onClick={handleCancel}
            disabled={saving}
          >
            Cancel
          </button>
          <button
            type="button"
            className="doc-editor-button doc-editor-button--primary"
            onClick={() => void save(base)}
            disabled={!canSave}
            title={`Save (${SAVE_SHORTCUT})`}
            aria-keyshortcuts="Control+S Meta+S"
          >
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>

      <div className="doc-editor-sr-only" role="status">
        {announcement}
      </div>

      {noAccess && (
        <div className="doc-editor-banner doc-editor-banner--error" role="alert">
          <div className="doc-editor-banner-text">
            <p>{FORBIDDEN_MESSAGE}</p>
          </div>
        </div>
      )}

      {showConflict && conflictToken !== null && (
        <div className="doc-editor-banner doc-editor-banner--warning" role="alert">
          <TriangleAlert size={16} className="doc-editor-banner-icon" aria-hidden="true" />
          <div className="doc-editor-banner-text">
            <p>Someone changed this doc while you were editing. Your changes are still here.</p>
            <p className="doc-editor-banner-note">
              Overwriting keeps the version you replace in History.
            </p>
          </div>
          <div className="doc-editor-banner-actions">
            <button
              type="button"
              className="doc-editor-button doc-editor-button--primary"
              onClick={() => void save(conflictToken)}
              disabled={busy}
            >
              {saving ? 'Overwriting...' : 'Overwrite with my version'}
            </button>
            <button
              type="button"
              className="doc-editor-button"
              onClick={() => setConfirm({ kind: 'conflict' })}
              disabled={saving}
            >
              Discard my changes
            </button>
          </div>
        </div>
      )}

      {showChanged && (
        <div className="doc-editor-banner doc-editor-banner--warning">
          <TriangleAlert size={16} className="doc-editor-banner-icon" aria-hidden="true" />
          <div className="doc-editor-banner-text">
            <p>{CHANGED_MESSAGE}</p>
          </div>
          <div className="doc-editor-banner-actions">
            <button
              type="button"
              className="doc-editor-button"
              onClick={() => setConfirm({ kind: 'reload' })}
            >
              Discard my changes
            </button>
            <button
              type="button"
              className="doc-editor-button"
              onClick={() => setKeptUpdatedAt(doc.updated_at)}
            >
              Keep editing
            </button>
          </div>
        </div>
      )}

      {restoreOffer && (
        <div className="doc-editor-banner doc-editor-banner--info">
          <div className="doc-editor-banner-text">
            <p>Restore unsaved draft from {restoreTime}?</p>
          </div>
          <div className="doc-editor-banner-actions">
            <button
              type="button"
              className="doc-editor-button doc-editor-button--primary"
              onClick={handleRestore}
              disabled={saving}
            >
              Restore
            </button>
            <button type="button" className="doc-editor-button" onClick={discardOffer}>
              Discard
            </button>
          </div>
        </div>
      )}

      {saveError && (
        <div className="doc-editor-banner doc-editor-banner--error" role="alert">
          <div className="doc-editor-banner-text">
            <p>{saveError}</p>
          </div>
        </div>
      )}

      {uploadError && (
        <div className="doc-editor-banner doc-editor-banner--error" role="alert">
          <div className="doc-editor-banner-text">
            <p>{uploadError}</p>
          </div>
          <button
            type="button"
            className="doc-editor-banner-dismiss"
            onClick={() => setUploadError(null)}
            aria-label="Dismiss"
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      )}

      {uploadNotice && (
        <div className="doc-editor-banner doc-editor-banner--info">
          <div className="doc-editor-banner-text">
            <p>{uploadNotice}</p>
          </div>
          <button
            type="button"
            className="doc-editor-banner-dismiss"
            onClick={() => setUploadNotice(null)}
            aria-label="Dismiss"
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      )}

      <div className={`doc-editor-panes${split ? ' doc-editor-panes--split' : ''}`}>
        <textarea
          ref={textareaRef}
          className={
            'doc-editor-input'
            + (showInput ? '' : ' doc-editor-input--hidden')
            + (dragOver ? ' doc-editor-input--drop' : '')
          }
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => {
            tabReleasedRef.current = false;
          }}
          onPaste={handlePaste}
          onDragOver={handleDragOver}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          readOnly={saving}
          aria-busy={saving}
          aria-label="Doc markdown"
          aria-describedby={hintId}
          placeholder="Write markdown..."
          spellCheck
          // Phones never auto-focus (it would pop the software keyboard).
          autoFocus={!isMobile}
        />
        {showPreview && (
          <section className="doc-editor-preview" aria-label="Preview">
            {largeDraft && (
              <div className="doc-editor-preview-bar">
                <span>Large doc: the preview updates when you refresh it.</span>
                <button
                  type="button"
                  className="doc-editor-button"
                  onClick={() => setPreviewText(draft)}
                  disabled={!previewStale}
                >
                  <RefreshCw size={14} aria-hidden="true" />
                  <span>Refresh preview</span>
                </button>
              </div>
            )}
            <MarkdownWorkspaceContext.Provider value={markdownCtx}>
              <DocMarkdownPreview content={deferredPreview} />
            </MarkdownWorkspaceContext.Provider>
          </section>
        )}
      </div>
      <p id={hintId} className="doc-editor-sr-only">
        Tab indents and Shift+Tab outdents. Press Escape, then Tab, to move focus out of the editor.
      </p>

      <DocConfirmDialog
        isOpen={confirm !== null}
        title={confirmTitle}
        confirmLabel={confirmLabel}
        tone="danger"
        busy={false}
        error={null}
        onConfirm={runConfirm}
        onClose={() => setConfirm(null)}
      >
        <p>{confirmBody}</p>
      </DocConfirmDialog>
    </div>
  );
}
