/**
 * TableHeader -- the title unit at the top of the table viewer (/tables/<id>),
 * modeled on DocHeader (same dropdown, inline rename, chips, notice).
 *
 * The title plus a chevron open the menu. Items follow the row's
 * server-computed `access` flags (never a role recomputed here):
 * - Rename (can_rename, owner): inline input, PUT title with
 *   `expected_updated_at` (one retry when only rows moved meanwhile);
 * - Columns (everyone): opens the Columns panel through `onOpenColumns`
 *   (the viewer passes it; the panel is read-only for non-owners; a host
 *   that passes none gets no item);
 * - History (can_edit, editors): opens the History drawer through
 *   `onOpenHistory` (the viewer passes it; no item without it);
 * - Download CSV, Download SQLite (everyone) and Download SQLite with history
 *   (can_download_history, editors): plain `<a href download>` links;
 * - "Require approval for agent writes" (can_require_approval, owner): PUT
 *   require_approval without a concurrency token (row writes move
 *   updated_at); the menu stays open so the new state is visible;
 * - Share (can_share, owner): a reserved slot, disabled until a host passes
 *   `onOpenShare` (the sharing dialog is phase C2; the viewer passes none).
 *   The disabled item says why in visible secondary text
 *   (SHARE_UNAVAILABLE_NOTE), linked with aria-describedby: a `title` alone
 *   is neither shown on touch screens nor read on a disabled button;
 * - Delete (can_delete, owner): DocConfirmDialog naming the row count, then
 *   navigates to All Tables (the project's list for a project table).
 * `menuButtonRef` hands the host the title (menu) button, so focus can
 * return to it when a panel the menu opened closes.
 *
 * Keys: while the menu is open, single keys act on the items, mirroring
 * DocHeader -- R rename, C columns, H history, A the approval switch, S
 * share, D delete, Escape closes the menu. The key hint shows on an enabled
 * item only, and a key acts only when its item is shown and enabled. No key
 * acts (Escape still closes the menu) while focus is in a text field, a
 * select or contenteditable, or while a dialog is open (`menuKeysBlocked`):
 * the menu stays open when focus moves on by keyboard, and typing there
 * must not trigger an item.
 *
 * A stale rename (409 stale_update) applies the server's
 * current row (`onRowApplied`, i.e. useTable.applyRow) -- or asks for a
 * re-fetch (`onStale`) when the error carries none -- and shows a notice;
 * nothing is overwritten blindly. Successful writes report the returned
 * detail through `onDetailApplied` (useTable.applyDetail) instead of waiting
 * for the realtime re-fetch. The app has no toast system: notices show in a
 * transient status line under the header, as in DocHeader.
 *
 * Beside the title: the mode badge (a public table only, as for docs), an
 * "Approval required" chip while the switch is on, the project chip (a link
 * to that project's tables when the project is the viewer's own, plain text
 * otherwise) and the share chips -- for the owner one read-only chip per
 * grant ("Everyone" for the everyone grant, "· can edit" for write), for a
 * recipient who shared it and whether they can edit.
 */

import { useCallback, useEffect, useId, useRef, useState, type Ref } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ChevronDown,
  Columns3,
  Database,
  DatabaseBackup,
  FileDown,
  Folder,
  History,
  Pencil,
  ShieldCheck,
  Square,
  SquareCheck,
  Trash2,
  Users,
} from 'lucide-react';
import {
  deleteTable,
  isStaleUpdateError,
  staleUpdateCurrent,
  tableDownloadUrl,
  updateTable,
} from '../../api/tablesApi';
import { ApiClientError } from '../../api/request';
import type { TableDetail, TableListRow, TableShare } from '../../api/types';
import { useProjects } from '../../contexts/ProjectsContext';
import { shouldShowDocModeBadge } from '../../utils/docMode';
import { tablesListPath } from '../../utils/tablesRoute';
import { DocConfirmDialog } from '../docs/DocConfirmDialog';
import { DocModeBadge } from '../docs/DocModeBadge';
import '../docs/DocHeader.css';
import './TableHeader.css';

/** Server cap on a table title. */
const TITLE_MAX_LENGTH = 200;
const NOTICE_MS = 5000;
/** Owner share chips shown before collapsing the rest into "+N". */
const MAX_SHARE_CHIPS = 3;
export const STALE_TABLE_NOTICE = 'This table changed elsewhere; it was reloaded. Try again if needed.';
const REQUIRE_APPROVAL_ON_NOTICE = 'Agents now need your approval before changing this table.';
const REQUIRE_APPROVAL_OFF_NOTICE = 'Agents can change this table without approval again.';
export const TABLE_APPROVAL_CHIP_TITLE =
  "Conversations need the owner's approval before changing this table.";
/** Why the disabled Share slot does nothing (sharing UI: phase C2). */
export const SHARE_UNAVAILABLE_NOTE = "Sharing isn't available here yet";
/** Rename rejections the user fixes by editing the typed title. */
const KEEP_EDITING_ERRORS = new Set(['duplicate_title', 'invalid_title']);

/**
 * True when the menu's single-key shortcuts must not act on `event`: its
 * target is a text field, a select or contenteditable, or a dialog is open
 * (any `[role=dialog]` / `[aria-modal=true]` element in the document, or the
 * target sits inside one). Escape is the caller's to handle either way.
 */
export function menuKeysBlocked(event: KeyboardEvent): boolean {
  const target = event.target instanceof Element ? event.target : null;
  if (target) {
    if (target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]')) return true;
    if (target instanceof HTMLElement && target.isContentEditable) return true;
    if (target.closest('[role="dialog"], [role="alertdialog"]')) return true;
  }
  return document.querySelector('[role="dialog"], [role="alertdialog"], [aria-modal="true"]') !== null;
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

function rowsPhrase(count: number): string {
  return `${count} ${count === 1 ? 'row' : 'rows'}`;
}

/** Display label of one owner-side share grant. */
export function tableShareLabel(share: TableShare): string {
  const who =
    share.user_id === null
      ? 'Everyone'
      : share.user?.name || share.user?.email || 'a deleted user';
  return share.permission === 'write' ? `${who} · can edit` : who;
}

export interface TableHeaderProps {
  table: TableDetail;
  /** A detail returned by PUT /tables/{id} (useTable.applyDetail). */
  onDetailApplied: (detail: TableDetail) => void;
  /** The `current` row of a stale_update (useTable.applyRow). */
  onRowApplied: (row: TableListRow) => void;
  /** Re-fetch the table (a stale_update without a `current` row). */
  onStale?: () => void;
  /** Open the Columns panel; no Columns item without it. */
  onOpenColumns?: () => void;
  /** Open the History drawer (editors); no History item without it. */
  onOpenHistory?: () => void;
  /** Open the Share dialog (owner; phase C2); a disabled slot without it. */
  onOpenShare?: () => void;
  /** The title (menu) button, for the host to return focus to. */
  menuButtonRef?: Ref<HTMLButtonElement>;
}

export function TableHeader({
  table,
  onDetailApplied,
  onRowApplied,
  onStale,
  onOpenColumns,
  onOpenHistory,
  onOpenShare,
  menuButtonRef,
}: TableHeaderProps) {
  const { projects } = useProjects();
  const navigate = useNavigate();
  const access = table.access;
  const canRename = access.can_rename;
  const canDelete = access.can_delete;
  const canRequireApproval = access.can_require_approval;
  const canDownloadHistory = access.can_download_history;
  const showShare = access.can_share;
  const columnsEnabled = Boolean(onOpenColumns);
  const historyEnabled = access.can_edit && Boolean(onOpenHistory);
  const shareEnabled = showShare && Boolean(onOpenShare);
  const shareNoteId = useId();
  const showModeBadge = shouldShowDocModeBadge(table.mode);

  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const renameInputRef = useRef<HTMLInputElement>(null);
  const renameSubmittedRef = useRef(false);
  const rejectedTitleRef = useRef<string | null>(null);

  const [notice, setNotice] = useState<{ text: string; seq: number } | null>(null);
  const noticeSeqRef = useRef(0);
  const showNotice = useCallback((text: string) => {
    noticeSeqRef.current += 1;
    setNotice({ text, seq: noticeSeqRef.current });
  }, []);

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [approvalSaving, setApprovalSaving] = useState(false);

  const project = table.project_id ? projects.find((p) => p.id === table.project_id) : undefined;
  const projectName = table.project_id ? project?.name || 'Project' : null;

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!menuOpen) return;
    const close = () => setMenuOpen(false);
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [menuOpen]);

  const toggleMenu = useCallback(() => {
    setNotice(null);
    setMenuOpen((open) => !open);
  }, []);

  /** A stale_update: show the server's row (or re-fetch) and say so. */
  const handleStale = useCallback(
    (err: unknown) => {
      const current = staleUpdateCurrent(err);
      if (current) onRowApplied(current);
      else onStale?.();
      showNotice(STALE_TABLE_NOTICE);
    },
    [onRowApplied, onStale, showNotice],
  );

  const startRename = useCallback(() => {
    if (!canRename) return;
    setMenuOpen(false);
    setNotice(null);
    renameSubmittedRef.current = false;
    rejectedTitleRef.current = null;
    setRenameValue(table.title);
    setRenaming(true);
  }, [canRename, table.title]);

  useEffect(() => {
    if (renaming) {
      renameInputRef.current?.focus();
      renameInputRef.current?.select();
    }
  }, [renaming]);

  const cancelRename = useCallback(() => {
    renameSubmittedRef.current = true;
    rejectedTitleRef.current = null;
    setRenaming(false);
    setRenameValue('');
  }, []);

  const submitRename = useCallback(
    async (trigger: 'enter' | 'blur') => {
      if (renameSubmittedRef.current) return;
      const title = renameValue.trim();
      if (!title || title === table.title) {
        cancelRename();
        return;
      }
      if (trigger === 'blur' && title === rejectedTitleRef.current) {
        cancelRename();
        return;
      }
      renameSubmittedRef.current = true;
      setNotice(null);
      setRenameSaving(true);
      try {
        let detail: TableDetail;
        try {
          detail = await updateTable(table.id, { title, expected_updated_at: table.updated_at });
        } catch (first) {
          // Row writes move updated_at, so during agent activity a rename is
          // often "stale" only because rows changed. When the title itself
          // is unchanged on the server, retry once against the current token.
          const current = isStaleUpdateError(first) ? staleUpdateCurrent(first) : null;
          if (!current || current.title !== table.title) throw first;
          onRowApplied(current);
          detail = await updateTable(table.id, { title, expected_updated_at: current.updated_at });
        }
        onDetailApplied(detail);
        setRenaming(false);
        setRenameValue('');
      } catch (err) {
        if (isStaleUpdateError(err)) {
          // Someone else renamed it meanwhile: show the current table but
          // keep the input open with what the user typed.
          handleStale(err);
          renameSubmittedRef.current = false;
          if (trigger === 'enter') renameInputRef.current?.focus();
        } else if (err instanceof ApiClientError && KEEP_EDITING_ERRORS.has(err.errorCode ?? '')) {
          showNotice(errorMessage(err, 'That title cannot be used.'));
          rejectedTitleRef.current = title;
          renameSubmittedRef.current = false;
          if (trigger === 'enter') renameInputRef.current?.focus();
        } else {
          showNotice(errorMessage(err, 'Failed to rename the table.'));
          setRenaming(false);
          setRenameValue('');
        }
      } finally {
        setRenameSaving(false);
      }
    },
    [cancelRename, handleStale, onDetailApplied, onRowApplied, renameValue, showNotice, table.id, table.title, table.updated_at],
  );

  const toggleRequireApproval = useCallback(async () => {
    if (!canRequireApproval || approvalSaving) return;
    const next = !table.require_approval;
    setNotice(null);
    setApprovalSaving(true);
    try {
      // No expected_updated_at, as in DocHeader: row writes move updated_at,
      // and switching approval on to stop an agent mid-write must not fail
      // as stale. The request sets an explicit value, so there is no lost
      // update to guard against.
      const detail = await updateTable(table.id, { require_approval: next });
      onDetailApplied(detail);
      showNotice(detail.require_approval ? REQUIRE_APPROVAL_ON_NOTICE : REQUIRE_APPROVAL_OFF_NOTICE);
    } catch (err) {
      if (isStaleUpdateError(err)) handleStale(err);
      else showNotice(errorMessage(err, 'Failed to change the approval setting.'));
    } finally {
      setApprovalSaving(false);
    }
  }, [
    approvalSaving, canRequireApproval, handleStale, onDetailApplied, showNotice,
    table.id, table.require_approval,
  ]);

  const openDelete = useCallback(() => {
    if (!canDelete) return;
    setMenuOpen(false);
    setNotice(null);
    setDeleteError(null);
    setDeleteOpen(true);
  }, [canDelete]);

  const projectId = table.project_id;
  const confirmDelete = useCallback(async () => {
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteTable(table.id);
      setDeleting(false);
      setDeleteOpen(false);
      navigate(tablesListPath(projectId));
    } catch (err) {
      setDeleting(false);
      setDeleteError(errorMessage(err, 'Failed to delete the table.'));
    }
  }, [navigate, projectId, table.id]);

  const runColumns = useCallback(() => {
    setMenuOpen(false);
    onOpenColumns?.();
  }, [onOpenColumns]);
  const runHistory = useCallback(() => {
    setMenuOpen(false);
    onOpenHistory?.();
  }, [onOpenHistory]);
  const runShare = useCallback(() => {
    setMenuOpen(false);
    onOpenShare?.();
  }, [onOpenShare]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key === 'escape') { setMenuOpen(false); return; }
      if (e.defaultPrevented || menuKeysBlocked(e)) return;
      if (key === 'r' && canRename) { e.preventDefault(); startRename(); }
      else if (key === 'c' && columnsEnabled) { e.preventDefault(); runColumns(); }
      else if (key === 'h' && historyEnabled) { e.preventDefault(); runHistory(); }
      else if (key === 'a' && canRequireApproval) { e.preventDefault(); void toggleRequireApproval(); }
      else if (key === 's' && shareEnabled) { e.preventDefault(); runShare(); }
      else if (key === 'd' && canDelete) { e.preventDefault(); openDelete(); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [
    menuOpen, canRename, columnsEnabled, historyEnabled, canRequireApproval, shareEnabled, canDelete,
    startRename, runColumns, runHistory, toggleRequireApproval, runShare, openDelete,
  ]);

  const closeMenu = () => setMenuOpen(false);
  const shares = table.shares ?? [];
  const shownShares = shares.slice(0, MAX_SHARE_CHIPS);
  const hiddenShares = shares.slice(MAX_SHARE_CHIPS);
  const ownerName = table.owner?.name || table.owner?.email || 'someone';
  const hasMeta =
    showModeBadge || table.require_approval || Boolean(table.project_id) || shares.length > 0 || table.shared_with_me;

  return (
    <div className="doc-header table-header">
      <div className="doc-header-bar">
        <div className="doc-header-left">
          <div className="doc-header-unit">
            {renaming ? (
              <input
                ref={renameInputRef}
                className="doc-header-rename-input"
                value={renameValue}
                placeholder="Table title"
                maxLength={TITLE_MAX_LENGTH}
                readOnly={renameSaving}
                aria-busy={renameSaving}
                onChange={(e) => setRenameValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); void submitRename('enter'); }
                  else if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
                }}
                onBlur={() => void submitRename('blur')}
                aria-label="Rename table"
              />
            ) : (
              <button
                type="button"
                ref={menuButtonRef}
                className={`doc-header-title-button${menuOpen ? ' open' : ''}`}
                onClick={(e) => {
                  e.stopPropagation();
                  toggleMenu();
                }}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                title={table.title}
              >
                <span className="doc-header-title">{table.title}</span>
                <span className="doc-header-chevron" aria-hidden="true">
                  <ChevronDown size={16} />
                </span>
              </button>
            )}

            {menuOpen && (
              <div className="doc-header-menu" role="menu" onClick={(e) => e.stopPropagation()}>
                {canRename && (
                  <button type="button" className="doc-header-menu-item" role="menuitem" onClick={startRename}>
                    <Pencil size={16} className="doc-header-menu-icon" />
                    <span>Rename</span>
                    <span className="doc-header-menu-key">R</span>
                  </button>
                )}
                {columnsEnabled && (
                  <button type="button" className="doc-header-menu-item" role="menuitem" onClick={runColumns}>
                    <Columns3 size={16} className="doc-header-menu-icon" />
                    <span>Columns</span>
                    <span className="doc-header-menu-key">C</span>
                  </button>
                )}
                {historyEnabled && (
                  <button type="button" className="doc-header-menu-item" role="menuitem" onClick={runHistory}>
                    <History size={16} className="doc-header-menu-icon" />
                    <span>History</span>
                    <span className="doc-header-menu-key">H</span>
                  </button>
                )}
                {canRequireApproval && (
                  <button
                    type="button"
                    className="doc-header-menu-item doc-header-menu-item--checkbox"
                    role="menuitemcheckbox"
                    aria-checked={table.require_approval}
                    aria-busy={approvalSaving}
                    disabled={approvalSaving}
                    onClick={() => void toggleRequireApproval()}
                    title={TABLE_APPROVAL_CHIP_TITLE}
                  >
                    {table.require_approval
                      ? <SquareCheck size={16} className="doc-header-menu-icon" aria-hidden="true" />
                      : <Square size={16} className="doc-header-menu-icon" aria-hidden="true" />}
                    <span>Require approval for agent writes</span>
                    <span className="doc-header-menu-key">A</span>
                  </button>
                )}
                {shareEnabled && (
                  <button type="button" className="doc-header-menu-item" role="menuitem" onClick={runShare}>
                    <Users size={16} className="doc-header-menu-icon" />
                    <span>Share</span>
                    <span className="doc-header-menu-key">S</span>
                  </button>
                )}
                {showShare && !shareEnabled && (
                  // The reason is visible text (touch screens show no
                  // title) and the item's description (a disabled button's
                  // title is not read out).
                  <button
                    type="button"
                    className="doc-header-menu-item table-header-menu-item--unavailable"
                    role="menuitem"
                    disabled
                    aria-label="Share"
                    aria-describedby={shareNoteId}
                    title={SHARE_UNAVAILABLE_NOTE}
                  >
                    <Users size={16} className="doc-header-menu-icon" />
                    <span className="table-header-menu-text">
                      <span>Share</span>
                      <span id={shareNoteId} className="table-header-menu-note">
                        {SHARE_UNAVAILABLE_NOTE}
                      </span>
                    </span>
                  </button>
                )}
                <div className="doc-header-menu-separator" role="separator" />
                <a
                  className="doc-header-menu-item"
                  role="menuitem"
                  href={tableDownloadUrl(table.id, 'csv')}
                  download
                  onClick={closeMenu}
                >
                  <FileDown size={16} className="doc-header-menu-icon" />
                  <span>Download CSV</span>
                </a>
                <a
                  className="doc-header-menu-item"
                  role="menuitem"
                  href={tableDownloadUrl(table.id, 'sqlite')}
                  download
                  onClick={closeMenu}
                >
                  <Database size={16} className="doc-header-menu-icon" />
                  <span>Download SQLite</span>
                </a>
                {canDownloadHistory && (
                  <a
                    className="doc-header-menu-item"
                    role="menuitem"
                    href={tableDownloadUrl(table.id, 'sqlite', true)}
                    download
                    onClick={closeMenu}
                  >
                    <DatabaseBackup size={16} className="doc-header-menu-icon" />
                    <span>Download SQLite with history</span>
                  </a>
                )}
                {canDelete && (
                  <>
                    <div className="doc-header-menu-separator" role="separator" />
                    <button
                      type="button"
                      className="doc-header-menu-item doc-header-menu-item--danger"
                      role="menuitem"
                      onClick={openDelete}
                    >
                      <Trash2 size={16} className="doc-header-menu-icon" />
                      <span>Delete</span>
                      <span className="doc-header-menu-key">D</span>
                    </button>
                  </>
                )}
              </div>
            )}
          </div>

          {hasMeta && (
            <div className="doc-header-meta">
              {showModeBadge && <DocModeBadge mode={table.mode} noun="table" />}
              {table.require_approval && (
                <span
                  className="doc-header-project-chip doc-header-chip--static doc-header-approval-chip"
                  title={TABLE_APPROVAL_CHIP_TITLE}
                >
                  <ShieldCheck size={12} strokeWidth={2.25} aria-hidden="true" />
                  <span className="doc-header-project-name">Approval required</span>
                </span>
              )}
              {table.project_id && project && (
                <Link
                  to={tablesListPath(table.project_id)}
                  className="doc-header-project-chip"
                  title={`All tables in ${projectName}`}
                >
                  <Folder size={12} strokeWidth={2.25} aria-hidden="true" />
                  <span className="doc-header-project-name">{projectName}</span>
                </Link>
              )}
              {table.project_id && !project && (
                <span className="doc-header-project-chip doc-header-chip--static" title="A table from a project">
                  <Folder size={12} strokeWidth={2.25} aria-hidden="true" />
                  <span className="doc-header-project-name">
                    {table.shared_with_me ? 'Project table' : projectName}
                  </span>
                </span>
              )}
              {shownShares.map((share) => (
                <span
                  key={share.id}
                  className="doc-header-project-chip doc-header-chip--static table-header-share-chip"
                  title={share.user?.email ?? (share.user_id === null ? 'Everyone on this install' : undefined)}
                >
                  <Users size={12} strokeWidth={2.25} aria-hidden="true" />
                  <span className="doc-header-project-name">{tableShareLabel(share)}</span>
                </span>
              ))}
              {hiddenShares.length > 0 && (
                <span
                  className="doc-header-project-chip doc-header-chip--static table-header-share-chip"
                  title={hiddenShares.map(tableShareLabel).join('\n')}
                >
                  <span className="doc-header-project-name">+{hiddenShares.length}</span>
                </span>
              )}
              {table.shared_with_me && (
                <span
                  className="doc-header-project-chip doc-header-chip--static table-header-share-chip"
                  title={table.owner?.email ? `Shared by ${table.owner.email}` : undefined}
                >
                  <Users size={12} strokeWidth={2.25} aria-hidden="true" />
                  <span className="doc-header-project-name">
                    Shared by {ownerName} · {access.can_edit ? 'Can edit' : 'Can view'}
                  </span>
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {notice && (
        <div className="doc-header-notice" role="status">
          {notice.text}
        </div>
      )}

      {canDelete && (
        <DocConfirmDialog
          isOpen={deleteOpen}
          title="Delete table?"
          confirmLabel="Delete"
          busyLabel="Deleting..."
          tone="danger"
          busy={deleting}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onClose={() => setDeleteOpen(false)}
        >
          <p>{`Delete "${table.title}" and its ${rowsPhrase(table.row_count)}? This cannot be undone.`}</p>
        </DocConfirmDialog>
      )}
    </div>
  );
}
