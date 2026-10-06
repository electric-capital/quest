/**
 * DocHistory -- the doc viewer's History view (DocViewer mode `history`,
 * rendered in place of the body).
 *
 * History is for editors (owner or write share): every history route 403s
 * `forbidden` for anyone else, since earlier revisions can hold text the
 * owner removed before sharing. Access lost while History is open -- a 403
 * from any call, or `access.can_edit` turning false on the doc -- replaces
 * the view (and closes any dialog) with a "no longer have edit access"
 * state and a Back to doc control.
 *
 * A version list beside the selected version. The list holds "Current
 * version" first (the live body, marked "Current"), then the revision
 * snapshots newest first; each entry says when its body was written, who
 * wrote it (`source_label`, linked to the writing conversation when the
 * server resolved one -- owners only) and its size. The detail renders the
 * version's markdown exactly as the viewer does, with images resolved
 * against the doc's CURRENT assets (an image deleted since shows the
 * missing-image chip). For a revision whose body has loaded it offers:
 * - "Show changes": the server's bounded diff from that revision to the
 *   current body ("Changes since this version": added lines were written
 *   after it), rendered by SkillContentDiffPreview;
 * - "Restore this version": a confirm, then POST .../restore with the
 *   optimistic-concurrency token of the current body AS IT WAS WHEN THE
 *   CONFIRM OPENED, so a write landing behind the dialog is a 409, never a
 *   silent overwrite; the response goes to `onRestored`;
 * - "Copy to a new doc": a title dialog (empty = the server's
 *   "<title> (copy)" default), then `onCopied` with the new row (a private
 *   doc of the viewer's own).
 *
 * The current body and its token come from the doc prop. After a restore's
 * stale_update 409 this tab may have missed the write's realtime events, so
 * History fetches the doc itself and shows THAT body as "Current version"
 * (and uses its token) until the prop catches up: a retry never sends a
 * token for a body the user has not been shown.
 *
 * Data: the list is fetched on mount and whenever the current token moves
 * (every body write adds a revision); a revision that drops out of a
 * refreshed list while selected is reported gone. Revision bodies are
 * immutable: a body fetch does not depend on the token (a fast writer must
 * not starve a large load), and every body that arrives is cached by id
 * (the last BODY_CACHE_SIZE), whatever is selected by then. A diff is cached
 * against the token it was computed for. List fetches, diff fetches and
 * detail errors are numbered; a response whose number is no longer current
 * is dropped, so a slow answer never lands on a newer selection.
 *
 * Phones (useIsMobile): the list alone; tapping a version opens it full
 * width with an "All versions" control back to the list. Escape steps back
 * (phone detail -> list -> doc) while focus is inside History and no dialog
 * is open. DocHistory.css lays the two panes out and, while History is
 * open, widens the viewer column and hides the Assets aside.
 */

import {
  memo,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { Link } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import { ArrowLeft, ChevronLeft, CopyPlus, FileDiff, RotateCcw, X } from 'lucide-react';
import {
  copyDocRevision,
  docAssetBase,
  fetchDoc,
  fetchDocRevision,
  fetchDocRevisions,
  isStaleUpdateError,
  restoreDocRevision,
} from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type {
  Doc,
  DocContentResponse,
  DocDetail,
  DocRevisionsResponse,
  DocVersion,
  DocWriteConversation,
  SkillContentDiff,
} from '../../api/types';
import { useIsMobile } from '../../hooks/useIsMobile';
import { formatDocSize } from '../../utils/allDocsGrouping';
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters';
import remarkMathCurrencyGuard from '../../utils/remarkMathCurrencyGuard';
import { MarkdownWorkspaceContext, markdownComponents } from '../Message';
import { SkillContentDiffPreview } from '../SkillContentDiffPreview';
import { DocConfirmDialog } from './DocConfirmDialog';
import './DocHistory.css';

export interface DocHistoryProps {
  doc: DocDetail;
  /** Back to the doc. */
  onClose: () => void;
  /** A successful restore (row + body as stored). */
  onRestored: (row: DocContentResponse) => void;
  /** A revision copied into a new doc of the viewer's own. */
  onCopied: (row: Doc) => void;
}

// Mirrors DOC_REVISION_MAX_COUNT / DOC_REVISION_RETENTION_DAYS
// (chat/docs/constants.py).
const RETENTION_NOTE =
  'Each change keeps the previous version here: up to 200 earlier versions, each kept for 30 days (the most recent earlier version is always kept).';
const STALE_NOTICE =
  'This doc changed while you were looking at History. The list was refreshed; check the version and try again.';
const GONE_NOTICE = 'That version is no longer available.';
const FORBIDDEN_NOTICE = 'You no longer have edit access to this doc.';

/** Server-side title cap (400 invalid_title beyond it). */
const TITLE_MAX_LENGTH = 200;

/** Revision bodies kept in memory (most recently loaded). */
const BODY_CACHE_SIZE = 10;

/** The body shown as "Current version" and its concurrency token. */
interface CurrentBody {
  content: string;
  updated_at: string;
}

interface CachedBody {
  id: string;
  content: string;
}

/** A cached diff and the current-body token it was computed against. */
interface CachedDiff {
  token: string;
  diff: SkillContentDiff | null;
}

/** A failed detail fetch, for the revision it was for. */
interface DetailError {
  id: string;
  message: string;
  // False for a revision that is gone (retrying cannot help).
  retryable: boolean;
}

/** The revision a Restore confirm is for, frozen when it opened. */
interface RestoreTarget {
  id: string;
  token: string;
  // The current version it replaces, as listed when the confirm opened.
  replaces: DocVersion | null;
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiClientError && err.message ? err.message : fallback;
}

/**
 * 403 from a history route: the viewer lost edit access (`forbidden`).
 * `docs_disabled` (the feature gate) is reported as a plain error.
 */
function isLostAccess(err: unknown): boolean {
  return (
    err instanceof ApiClientError
    && err.statusCode === 403
    && err.errorCode !== 'docs_disabled'
  );
}

/** 404 `revision_not_found`: pruned by retention (or never existed). */
function isRevisionGone(err: unknown): boolean {
  return (
    err instanceof ApiClientError
    && err.statusCode === 404
    && err.errorCode === 'revision_not_found'
  );
}

/** Same path rule as the viewer footer's writer link. */
function conversationPath(conversation: DocWriteConversation): string {
  const id = encodeURIComponent(conversation.id);
  return conversation.project_id
    ? `/projects/${encodeURIComponent(conversation.project_id)}/${id}`
    : `/chats/${id}`;
}

function fullTimestamp(iso: string): string {
  return parseUTCTimestamp(iso).toLocaleString();
}

function headingTimestamp(iso: string): string {
  return parseUTCTimestamp(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

/** The cache with `id`'s body added as the newest entry, bounded. */
function withBody(cache: CachedBody[], id: string, content: string): CachedBody[] {
  return [...cache.filter((entry) => entry.id !== id), { id, content }].slice(-BODY_CACHE_SIZE);
}

/** Relative time with the full local timestamp on hover. */
function TimeAgo({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} title={fullTimestamp(iso)}>
      {formatRelativeTimestamp(iso)}
    </time>
  );
}

/** "by <source_label>", the label linked to its conversation when known. */
function VersionAuthor({ version }: { version: DocVersion }) {
  const conversation = version.conversation;
  return (
    <>
      by{' '}
      {conversation ? (
        <Link to={conversationPath(conversation)} className="doc-history-link">
          {version.source_label || conversation.title || 'a conversation'}
        </Link>
      ) : (
        version.source_label
      )}
    </>
  );
}

/**
 * One version's markdown, rendered like the viewer body. Memoized: the
 * parse of a large body must not rerun on unrelated History state (e.g.
 * typing a copy title).
 */
const VersionBody = memo(function VersionBody({
  content,
  assetBase,
}: {
  content: string;
  assetBase: string;
}) {
  const markdownCtx = useMemo(() => ({ assetBase }), [assetBase]);
  if (content.trim() === '') {
    return <p className="doc-history-muted">This version is empty.</p>;
  }
  return (
    // `message-content` = the chat markdown typography; DocHistory.css
    // strips the bubble chrome like the viewer does.
    <div className="message-content doc-history-markdown">
      <MarkdownWorkspaceContext.Provider value={markdownCtx}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard]}
          rehypePlugins={[rehypeHighlight, rehypeKatex]}
          components={markdownComponents}
        >
          {content}
        </ReactMarkdown>
      </MarkdownWorkspaceContext.Provider>
    </div>
  );
});

function VersionDiff({ diff }: { diff: SkillContentDiff | null }) {
  if (!diff) {
    return <p className="doc-history-muted">The changes could not be computed.</p>;
  }
  if (diff.added === 0 && diff.removed === 0) {
    return <p className="doc-history-muted">No changes: the current version is the same as this one.</p>;
  }
  return <SkillContentDiffPreview diff={diff} />;
}

export function DocHistory({ doc, onClose, onRestored, onCopied }: DocHistoryProps) {
  const docId = doc.id;
  const isMobile = useIsMobile();
  const assetBase = docAssetBase(docId);
  const idPrefix = useId();

  const headingRef = useRef<HTMLHeadingElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const allVersionsRef = useRef<HTMLButtonElement>(null);
  const restoreButtonRef = useRef<HTMLButtonElement>(null);
  const copyButtonRef = useRef<HTMLButtonElement>(null);
  const copyInputRef = useRef<HTMLInputElement>(null);
  const lostAccessBackRef = useRef<HTMLButtonElement>(null);
  // Phone list <-> detail switch: which pane takes focus after it renders.
  const pendingFocusRef = useRef<'detail' | 'list' | null>(null);

  // --- Current body ---------------------------------------------------------
  // The doc prop, unless History fetched a newer doc after a 409 (see the
  // header comment); the prop wins again once it has caught up.
  const [fetchedCurrent, setFetchedCurrent] = useState<CurrentBody | null>(null);
  const current: CurrentBody =
    fetchedCurrent !== null && fetchedCurrent.updated_at > doc.updated_at ? fetchedCurrent : doc;
  const contentToken = current.updated_at;
  const currentSeqRef = useRef(0);

  // Set by a 403 from any history call; sticky until History reopens.
  const [forbidden, setForbidden] = useState(false);
  const lostAccess = forbidden || !doc.access.can_edit;

  const [notice, setNotice] = useState<string | null>(null);

  // --- Version list ---------------------------------------------------------
  const [list, setList] = useState<DocRevisionsResponse | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listReload, setListReload] = useState(0);
  const listSeqRef = useRef(0);
  const reloadList = useCallback(() => setListReload((n) => n + 1), []);

  // null = the current version.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // For the list response handler: the selection when it lands.
  const selectedIdRef = useRef<string | null>(null);
  selectedIdRef.current = selectedId;

  useEffect(() => {
    const seq = ++listSeqRef.current;
    if (lostAccess) return;
    setListError(null);
    fetchDocRevisions(docId).then(
      (response) => {
        if (seq !== listSeqRef.current) return;
        setList(response);
        const selected = selectedIdRef.current;
        if (selected !== null && !response.revisions.some((rev) => rev.id === selected)) {
          // Pruned (or reported gone) since it was picked: say so instead
          // of silently showing the current version.
          setSelectedId(null);
          setNotice(GONE_NOTICE);
        }
      },
      (err: unknown) => {
        if (seq !== listSeqRef.current) return;
        if (isLostAccess(err)) setForbidden(true);
        else setListError(errorMessage(err, 'Failed to load the history.'));
      },
    );
    // contentToken: a new write adds a revision (and changes `current`).
  }, [docId, contentToken, listReload, lostAccess]);

  const selectedVersion: DocVersion | null = list
    ? (selectedId === null
        ? list.current
        : (list.revisions.find((rev) => rev.id === selectedId) ?? list.current))
    : null;
  const revisionId = selectedVersion?.id ?? null;

  const [showChanges, setShowChanges] = useState(false);
  const [phoneDetail, setPhoneDetail] = useState(false);

  // --- Selected revision's body and diff -----------------------------------
  const [bodies, setBodies] = useState<CachedBody[]>([]);
  const [diffs, setDiffs] = useState<Record<string, CachedDiff>>({});
  const [detailError, setDetailError] = useState<DetailError | null>(null);
  const [detailRetry, setDetailRetry] = useState(0);
  const bodySeqRef = useRef(0);
  const diffSeqRef = useRef(0);

  const revisionBody =
    revisionId === null ? undefined : bodies.find((entry) => entry.id === revisionId)?.content;
  const needBody = revisionId !== null && revisionBody === undefined;
  const diffReady = revisionId !== null && diffs[revisionId]?.token === contentToken;
  const needDiff = revisionId !== null && showChanges && !diffReady;
  // The diff (only) is computed against the current body: only it refetches
  // when the token moves.
  const diffToken = needDiff ? contentToken : null;

  const cacheBody = useCallback((id: string, content: string) => {
    setBodies((prev) => withBody(prev, id, content));
  }, []);

  const handleDetailError = useCallback(
    (id: string, err: unknown) => {
      if (isLostAccess(err)) {
        setForbidden(true);
      } else if (isRevisionGone(err)) {
        setNotice(GONE_NOTICE);
        setDetailError({ id, message: GONE_NOTICE, retryable: false });
        reloadList();
      } else {
        setDetailError({
          id,
          message: errorMessage(err, 'Failed to load this version.'),
          retryable: true,
        });
      }
    },
    [reloadList],
  );

  // The body alone. With Show changes on, the diff fetch brings it along.
  useEffect(() => {
    const seq = ++bodySeqRef.current;
    if (lostAccess || revisionId === null || !needBody || needDiff) return;
    const id = revisionId;
    setDetailError(null);
    fetchDocRevision(docId, id).then(
      // Immutable: cached whatever is selected by the time it lands.
      (detail) => cacheBody(id, detail.content),
      (err: unknown) => {
        if (seq === bodySeqRef.current) handleDetailError(id, err);
      },
    );
  }, [docId, revisionId, needBody, needDiff, detailRetry, lostAccess, cacheBody, handleDetailError]);

  useEffect(() => {
    const seq = ++diffSeqRef.current;
    if (lostAccess || revisionId === null || diffToken === null) return;
    const id = revisionId;
    const token = diffToken;
    setDetailError(null);
    fetchDocRevision(docId, id, { diff: true }).then(
      (detail) => {
        cacheBody(id, detail.content);
        if (seq !== diffSeqRef.current) return;
        setDiffs((prev) => ({ ...prev, [id]: { token, diff: detail.diff ?? null } }));
      },
      (err: unknown) => {
        if (seq === diffSeqRef.current) handleDetailError(id, err);
      },
    );
  }, [docId, revisionId, diffToken, detailRetry, lostAccess, cacheBody, handleDetailError]);

  // --- Restore -------------------------------------------------------------
  const [restoreTarget, setRestoreTarget] = useState<RestoreTarget | null>(null);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  const openRestore = useCallback(
    (id: string) => {
      setNotice(null);
      setRestoreError(null);
      setRestoreTarget({ id, token: contentToken, replaces: list?.current ?? null });
    },
    [contentToken, list],
  );

  const closeRestore = useCallback(() => {
    setRestoreTarget(null);
    setRestoreError(null);
    // Back to the trigger (gone when the revision went away: the heading).
    setTimeout(() => (restoreButtonRef.current ?? headingRef.current)?.focus(), 0);
  }, []);

  // After a 409: fetch the doc and show its body as the current version
  // (its token then backs the next confirm), then refresh the list.
  const refreshCurrent = useCallback(() => {
    const seq = ++currentSeqRef.current;
    fetchDoc(docId).then(
      (fresh) => {
        if (seq !== currentSeqRef.current) return;
        if (!fresh.access.can_edit) {
          setForbidden(true);
          return;
        }
        setFetchedCurrent({ content: fresh.content, updated_at: fresh.updated_at });
        reloadList();
      },
      (err: unknown) => {
        if (seq !== currentSeqRef.current) return;
        // The prop body stays on screen, and with it the prop token.
        if (isLostAccess(err)) setForbidden(true);
        else reloadList();
      },
    );
  }, [docId, reloadList]);

  const confirmRestore = useCallback(async () => {
    const target = restoreTarget;
    if (target === null || restoreBusy) return;
    setRestoreBusy(true);
    setRestoreError(null);
    try {
      const row = await restoreDocRevision(docId, target.id, {
        expected_updated_at: target.token,
      });
      setRestoreBusy(false);
      setRestoreTarget(null);
      onRestored(row);
    } catch (err) {
      setRestoreBusy(false);
      if (isStaleUpdateError(err)) {
        setNotice(STALE_NOTICE);
        closeRestore();
        refreshCurrent();
      } else if (isRevisionGone(err)) {
        setNotice(GONE_NOTICE);
        reloadList();
        closeRestore();
      } else if (isLostAccess(err)) {
        setForbidden(true);
      } else {
        setRestoreError(errorMessage(err, 'Failed to restore this version.'));
      }
    }
  }, [docId, restoreTarget, restoreBusy, onRestored, reloadList, closeRestore, refreshCurrent]);

  // --- Copy ----------------------------------------------------------------
  const [copyTarget, setCopyTarget] = useState<string | null>(null);
  const [copyTitle, setCopyTitle] = useState('');
  const [copyBusy, setCopyBusy] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);

  const openCopy = useCallback((id: string) => {
    setNotice(null);
    setCopyTitle('');
    setCopyError(null);
    setCopyTarget(id);
  }, []);

  const closeCopy = useCallback(() => {
    setCopyTarget(null);
    setCopyError(null);
    setTimeout(() => (copyButtonRef.current ?? headingRef.current)?.focus(), 0);
  }, []);

  const confirmCopy = useCallback(async () => {
    if (copyTarget === null || copyBusy) return;
    const title = copyTitle.trim();
    setCopyBusy(true);
    setCopyError(null);
    try {
      const row = await copyDocRevision(docId, copyTarget, title ? { title } : {});
      setCopyBusy(false);
      setCopyTarget(null);
      onCopied(row);
    } catch (err) {
      setCopyBusy(false);
      if (isLostAccess(err)) {
        setForbidden(true);
      } else if (isRevisionGone(err)) {
        setNotice(GONE_NOTICE);
        reloadList();
        closeCopy();
      } else {
        // duplicate_title / invalid_title (and anything else): the dialog
        // stays open with the server's message.
        setCopyError(errorMessage(err, 'Failed to copy this version.'));
      }
    }
  }, [docId, copyTarget, copyTitle, copyBusy, onCopied, reloadList, closeCopy]);

  // Lost access closes whatever dialog was open, for good: the targets are
  // dropped, so nothing reappears if access comes back.
  const restoreOpen = restoreTarget !== null && !lostAccess;
  const copyOpen = copyTarget !== null && !lostAccess;
  const dialogOpen = restoreOpen || copyOpen;

  useEffect(() => {
    if (!lostAccess) return;
    setRestoreTarget(null);
    setRestoreError(null);
    setCopyTarget(null);
    setCopyError(null);
    setNotice(null);
  }, [lostAccess]);

  useEffect(() => {
    if (copyOpen) copyInputRef.current?.focus();
  }, [copyOpen]);

  // --- Focus and keyboard --------------------------------------------------
  // Entering History lands keyboard / screen-reader users on its heading
  // (the menu item that opened it is gone).
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  // Losing access replaces the view: focus its way out.
  useEffect(() => {
    if (lostAccess) lostAccessBackRef.current?.focus();
  }, [lostAccess]);

  // Phone list <-> detail: focus follows the pane that just appeared.
  useEffect(() => {
    const target = pendingFocusRef.current;
    pendingFocusRef.current = null;
    if (target === 'detail') allVersionsRef.current?.focus();
    else if (target === 'list') {
      listRef.current
        ?.querySelector<HTMLButtonElement>('.doc-history-item-select[aria-current="true"]')
        ?.focus();
    }
  }, [phoneDetail]);

  const select = useCallback(
    (id: string | null) => {
      setSelectedId(id);
      if (isMobile) {
        pendingFocusRef.current = 'detail';
        setPhoneDetail(true);
      }
    },
    [isMobile],
  );

  const backToList = useCallback(() => {
    pendingFocusRef.current = 'list';
    setPhoneDetail(false);
  }, []);

  const phoneDetailShown = isMobile && phoneDetail && !lostAccess && list !== null;

  const handleRootKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    // Dialogs are portaled but their key events still bubble through here;
    // their own Escape closes them, not History.
    if (e.key !== 'Escape' || e.defaultPrevented || dialogOpen) return;
    e.preventDefault();
    if (phoneDetailShown) backToList();
    else onClose();
  };

  // Up / Down (and Home / End) move between the list entries.
  const handleListKeyDown = (e: KeyboardEvent<HTMLUListElement>) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
    const buttons = Array.from(
      e.currentTarget.querySelectorAll<HTMLButtonElement>('.doc-history-item-select'),
    );
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (index === -1) return;
    e.preventDefault();
    let next = index;
    if (e.key === 'ArrowDown') next = Math.min(buttons.length - 1, index + 1);
    else if (e.key === 'ArrowUp') next = Math.max(0, index - 1);
    else if (e.key === 'Home') next = 0;
    else next = buttons.length - 1;
    buttons[next].focus();
  };

  // --- Render --------------------------------------------------------------
  const renderEntry = (version: DocVersion, isCurrent: boolean) => {
    const selected = selectedVersion !== null && version.id === selectedVersion.id;
    const key = version.id ?? 'current';
    const metaId = `${idPrefix}-meta-${key}`;
    // Name = the visible relative time between hidden words that make it
    // unique ("Version written 2h ago (<full timestamp>)"); the author and
    // size are its description.
    return (
      <li key={key} className={`doc-history-item${selected ? ' is-selected' : ''}`}>
        <button
          type="button"
          className="doc-history-item-select"
          aria-current={selected ? 'true' : undefined}
          aria-describedby={metaId}
          onClick={() => select(version.id)}
        >
          <span className="doc-history-sr-only">
            {isCurrent ? 'Current version, written ' : 'Version written '}
          </span>
          <span className="doc-history-item-when" title={fullTimestamp(version.written_at)}>
            {formatRelativeTimestamp(version.written_at)}
          </span>
          <span className="doc-history-sr-only">{` (${fullTimestamp(version.written_at)})`}</span>
          {isCurrent && (
            <span className="doc-history-item-badge" aria-hidden="true">
              Current
            </span>
          )}
        </button>
        <div id={metaId} className="doc-history-item-meta">
          <span className="doc-history-item-author">
            <VersionAuthor version={version} />
          </span>
          <span className="doc-history-sep" aria-hidden="true"> · </span>
          <span className="doc-history-item-size">{formatDocSize(version.size, 0)}</span>
        </div>
      </li>
    );
  };

  let content: ReactNode;
  if (lostAccess) {
    content = (
      <div className="doc-history-state" role="alert">
        <p className="doc-history-state-text">{FORBIDDEN_NOTICE}</p>
        <button
          ref={lostAccessBackRef}
          type="button"
          className="doc-history-button"
          onClick={onClose}
        >
          <ArrowLeft size={15} aria-hidden="true" />
          Back to doc
        </button>
      </div>
    );
  } else if (list === null) {
    content = listError === null ? (
      <p className="doc-history-state">Loading versions...</p>
    ) : (
      <div className="doc-history-state">
        <p className="doc-history-state-text">{listError}</p>
        <button type="button" className="doc-history-button" onClick={reloadList}>
          Retry
        </button>
      </div>
    );
  } else {
    const showList = !isMobile || !phoneDetail;
    const showDetail = !isMobile || phoneDetail;
    const version = selectedVersion ?? list.current;
    const revId = version.id;
    const isCurrent = revId === null;
    const error = revId !== null && detailError?.id === revId ? detailError : null;
    const gone = error !== null && !error.retryable;

    let detailBody: ReactNode;
    if (revId === null) {
      detailBody = <VersionBody content={current.content} assetBase={assetBase} />;
    } else if (showChanges ? diffReady : revisionBody !== undefined) {
      detailBody = showChanges ? (
        <section className="doc-history-changes" aria-label="Changes since this version">
          <h4 className="doc-history-changes-caption">Changes since this version</h4>
          <p className="doc-history-changes-hint">
            Lines marked + were written after this version; lines marked - have been removed since.
          </p>
          <VersionDiff diff={diffs[revId]?.diff ?? null} />
        </section>
      ) : (
        <VersionBody content={revisionBody as string} assetBase={assetBase} />
      );
    } else if (error) {
      detailBody = (
        <div className="doc-history-detail-state">
          <p className="doc-history-state-text">{error.message}</p>
          {error.retryable && (
            <button
              type="button"
              className="doc-history-button"
              onClick={() => setDetailRetry((n) => n + 1)}
            >
              Retry
            </button>
          )}
        </div>
      );
    } else {
      detailBody = (
        <p className="doc-history-muted">
          {showChanges ? 'Loading changes...' : 'Loading this version...'}
        </p>
      );
    }

    // Restore / Copy act on a body the user has seen.
    const canAct = revId !== null && revisionBody !== undefined && !gone;

    content = (
      <div className={`doc-history-layout${isMobile ? ' doc-history-layout--phone' : ''}`}>
        {showList && (
          <section className="doc-history-list-pane" aria-label="Versions">
            <ul ref={listRef} className="doc-history-list" onKeyDown={handleListKeyDown}>
              {renderEntry(list.current, true)}
              {list.revisions.map((rev) => renderEntry(rev, false))}
            </ul>
            {list.revisions.length === 0 && (
              <p className="doc-history-empty">No earlier versions yet.</p>
            )}
            <p className="doc-history-retention">{RETENTION_NOTE}</p>
          </section>
        )}
        {showDetail && (
          <article
            className="doc-history-detail"
            aria-label={isCurrent ? 'Current version' : 'Selected version'}
          >
            {isMobile && (
              <button
                ref={allVersionsRef}
                type="button"
                className="doc-history-button doc-history-all-versions"
                onClick={backToList}
              >
                <ChevronLeft size={16} aria-hidden="true" />
                All versions
              </button>
            )}
            <div className="doc-history-detail-head">
              <div className="doc-history-detail-heading">
                <h3 className="doc-history-detail-title">
                  {isCurrent ? 'Current version' : `Version from ${headingTimestamp(version.written_at)}`}
                </h3>
                <p className="doc-history-detail-meta">
                  Written <TimeAgo iso={version.written_at} /> <VersionAuthor version={version} />
                  <span className="doc-history-sep" aria-hidden="true"> · </span>
                  {formatDocSize(version.size, 0)}
                  {version.replaced_at && (
                    <>
                      <span className="doc-history-sep" aria-hidden="true"> · </span>
                      Replaced <TimeAgo iso={version.replaced_at} />
                    </>
                  )}
                </p>
              </div>
              {revId !== null && !gone && (
                <div className="doc-history-actions">
                  <button
                    type="button"
                    className="doc-history-button"
                    aria-pressed={showChanges}
                    onClick={() => setShowChanges((on) => !on)}
                  >
                    <FileDiff size={15} aria-hidden="true" />
                    Show changes
                  </button>
                  {canAct && (
                    <>
                      <button
                        ref={copyButtonRef}
                        type="button"
                        className="doc-history-button"
                        onClick={() => openCopy(revId)}
                      >
                        <CopyPlus size={15} aria-hidden="true" />
                        Copy to a new doc
                      </button>
                      <button
                        ref={restoreButtonRef}
                        type="button"
                        className="doc-history-button doc-history-button--primary"
                        onClick={() => openRestore(revId)}
                      >
                        <RotateCcw size={15} aria-hidden="true" />
                        Restore this version
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
            <div className="doc-history-detail-body">{detailBody}</div>
          </article>
        )}
      </div>
    );
  }

  const replaces = restoreTarget?.replaces ?? null;

  return (
    <section
      className="doc-history"
      aria-labelledby={`${idPrefix}-title`}
      onKeyDown={handleRootKeyDown}
    >
      <div className="doc-history-header">
        <h2 id={`${idPrefix}-title`} ref={headingRef} tabIndex={-1} className="doc-history-title">
          History
        </h2>
        <button type="button" className="doc-history-button" onClick={onClose}>
          <ArrowLeft size={15} aria-hidden="true" />
          Back to doc
        </button>
      </div>
      {notice && !lostAccess && (
        <div className="doc-history-notice" role="alert">
          <span className="doc-history-notice-text">{notice}</span>
          <button
            type="button"
            className="doc-history-notice-dismiss"
            aria-label="Dismiss"
            onClick={() => setNotice(null)}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      )}
      {list !== null && listError !== null && !lostAccess && (
        <div className="doc-history-notice" role="alert">
          <span className="doc-history-notice-text">Couldn't refresh the versions: {listError}</span>
          <button type="button" className="doc-history-notice-action" onClick={reloadList}>
            Retry
          </button>
        </div>
      )}
      {content}
      <DocConfirmDialog
        isOpen={restoreOpen}
        title="Restore this version?"
        confirmLabel="Restore"
        busyLabel="Restoring..."
        busy={restoreBusy}
        error={restoreError}
        onConfirm={() => void confirmRestore()}
        onClose={closeRestore}
      >
        {replaces && (
          <p>
            It replaces the current version, written {formatRelativeTimestamp(replaces.written_at)} by{' '}
            {replaces.source_label}.
          </p>
        )}
        <p>The current version is kept in History, so you can undo this.</p>
      </DocConfirmDialog>
      <DocConfirmDialog
        isOpen={copyOpen}
        title="Copy to a new doc"
        confirmLabel="Copy"
        busyLabel="Copying..."
        busy={copyBusy}
        error={copyError}
        onConfirm={() => void confirmCopy()}
        onClose={closeCopy}
      >
        <p>Creates a new private doc of your own from this version, with the images it uses.</p>
        <label className="doc-history-copy-label" htmlFor={`${idPrefix}-copy-title`}>
          Title
        </label>
        <input
          ref={copyInputRef}
          id={`${idPrefix}-copy-title`}
          type="text"
          className="doc-history-copy-input"
          value={copyTitle}
          placeholder="Leave empty for a default title"
          maxLength={TITLE_MAX_LENGTH}
          readOnly={copyBusy}
          aria-busy={copyBusy}
          onChange={(e) => setCopyTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void confirmCopy();
            }
          }}
        />
      </DocConfirmDialog>
    </section>
  );
}
