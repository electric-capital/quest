/**
 * DocHeader -- the title unit at the top of the doc viewer (/docs/<id>).
 *
 * Same interaction model as ConversationHeader: the title plus a chevron
 * open a small dropdown, single-key hints act while it is open, and Rename
 * swaps the title for an inline input. Items follow the viewer's access
 * flags: Rename (can_rename), the two downloads (always), Delete
 * (can_delete, behind a confirm). A non-owner gets only the downloads. There
 * is no mode switch: user docs are always private and a project doc takes
 * its project's mode (`access.can_switch_mode` is always false in v1).
 * Beside the title sit the mode badge (a public doc only, see utils/docMode)
 * and, for a project doc, a folder chip linking to that project's docs; on
 * the right, the Show source / Show rendered toggle.
 *
 * Writes report the returned row through `onRowApplied` (useDoc.applyRow)
 * rather than waiting for the realtime re-fetch. View-only: no content
 * editing anywhere here.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ChevronDown,
  Code,
  Eye,
  FileArchive,
  FileDown,
  Folder,
  Pencil,
  Trash2,
} from 'lucide-react';
import {
  deleteDoc,
  docDownloadUrl,
  isStaleUpdateError,
  staleUpdateCurrent,
  updateDoc,
} from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type { Doc, DocDetail } from '../../api/types';
import { useProjects } from '../../contexts/ProjectsContext';
import { shouldShowDocModeBadge } from '../../utils/docMode';
import { docsListPath } from '../../utils/docsRoute';
import { DocConfirmDialog } from './DocConfirmDialog';
import { DocModeBadge } from './DocModeBadge';
import './DocHeader.css';

/** Server cap on a doc title (chat/docs/constants.py DOC_TITLE_MAX_LEN). */
const TITLE_MAX_LENGTH = 200;
const NOTICE_MS = 5000;
const STALE_RENAME_NOTICE = 'This doc changed elsewhere; the title was reloaded.';
/** Rename rejections the user fixes by editing the typed title. */
const KEEP_EDITING_ERRORS = new Set(['duplicate_title', 'invalid_title']);

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export interface DocHeaderProps {
  doc: DocDetail;
  showSource: boolean;
  onToggleSource: () => void;
  /** A row returned by the rename endpoint (or a stale_update's `current`). */
  onRowApplied: (row: Doc) => void;
  onDeleted: () => void;
}

export function DocHeader({ doc, showSource, onToggleSource, onRowApplied, onDeleted }: DocHeaderProps) {
  const { projects } = useProjects();
  const showModeBadge = shouldShowDocModeBadge(doc.mode);
  const { can_rename: canRename, can_delete: canDelete } = doc.access;

  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const renameInputRef = useRef<HTMLInputElement>(null);
  // Guards the blur-after-Enter double submit and the blur while saving.
  const renameSubmittedRef = useRef(false);
  // The last title the server refused while the input stayed open: blurring
  // away from it gives up instead of resubmitting the same value.
  const rejectedTitleRef = useRef<string | null>(null);

  // Transient notice under the header; `seq` restarts the timer when the
  // same text is shown again.
  const [notice, setNotice] = useState<{ text: string; seq: number } | null>(null);
  const noticeSeqRef = useRef(0);
  const showNotice = useCallback((text: string) => {
    noticeSeqRef.current += 1;
    setNotice({ text, seq: noticeSeqRef.current });
  }, []);

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const projectName = doc.project_id
    ? projects.find((p) => p.id === doc.project_id)?.name || 'Project'
    : null;

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // Close the menu on any outside click (the toggle stops propagation).
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

  const startRename = useCallback(() => {
    if (!canRename) return;
    setMenuOpen(false);
    setNotice(null);
    renameSubmittedRef.current = false;
    rejectedTitleRef.current = null;
    setRenameValue(doc.title);
    setRenaming(true);
  }, [canRename, doc.title]);

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
      if (!title || title === doc.title) {
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
        const row = await updateDoc(doc.id, { title, expected_updated_at: doc.updated_at });
        onRowApplied(row);
        setRenaming(false);
        setRenameValue('');
      } catch (err) {
        if (isStaleUpdateError(err)) {
          // Someone (a conversation, another tab) wrote the doc since it was
          // loaded: show the current row instead of overwriting blindly.
          const current = staleUpdateCurrent(err);
          if (current) onRowApplied(current);
          showNotice(STALE_RENAME_NOTICE);
          setRenaming(false);
          setRenameValue('');
        } else if (err instanceof ApiClientError && KEEP_EDITING_ERRORS.has(err.errorCode ?? '')) {
          showNotice(errorMessage(err, 'That title cannot be used.'));
          rejectedTitleRef.current = title;
          renameSubmittedRef.current = false;
          if (trigger === 'enter') renameInputRef.current?.focus();
        } else {
          showNotice(errorMessage(err, 'Failed to rename the doc.'));
          setRenaming(false);
          setRenameValue('');
        }
      } finally {
        setRenameSaving(false);
      }
    },
    [cancelRename, doc.id, doc.title, doc.updated_at, onRowApplied, renameValue, showNotice],
  );

  const openDelete = useCallback(() => {
    if (!canDelete) return;
    setMenuOpen(false);
    setNotice(null);
    setDeleteError(null);
    setDeleteOpen(true);
  }, [canDelete]);

  const confirmDelete = useCallback(async () => {
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteDoc(doc.id);
      setDeleting(false);
      setDeleteOpen(false);
      onDeleted();
    } catch (err) {
      setDeleting(false);
      setDeleteError(errorMessage(err, 'Failed to delete the doc.'));
    }
  }, [doc.id, onDeleted]);

  // Single-key shortcuts while the menu is open (mirror the hint letters).
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key === 'r' && canRename) { e.preventDefault(); startRename(); }
      else if (key === 'd' && canDelete) { e.preventDefault(); openDelete(); }
      else if (key === 'escape') { setMenuOpen(false); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [menuOpen, canRename, canDelete, startRename, openDelete]);

  const closeMenu = () => setMenuOpen(false);

  return (
    <div className="doc-header">
      <div className="doc-header-bar">
        <div className="doc-header-left">
          <div className="doc-header-unit">
            {renaming ? (
              <input
                ref={renameInputRef}
                className="doc-header-rename-input"
                value={renameValue}
                placeholder="Doc title"
                maxLength={TITLE_MAX_LENGTH}
                readOnly={renameSaving}
                aria-busy={renameSaving}
                onChange={(e) => setRenameValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); void submitRename('enter'); }
                  else if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
                }}
                onBlur={() => void submitRename('blur')}
                aria-label="Rename doc"
              />
            ) : (
              <button
                type="button"
                className={`doc-header-title-button${menuOpen ? ' open' : ''}`}
                onClick={(e) => {
                  e.stopPropagation();
                  toggleMenu();
                }}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                title={doc.title}
              >
                <span className="doc-header-title">{doc.title}</span>
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
                {/* Phase 3 (not built): Share, History, Edit menu items go here. */}
                {canRename && <div className="doc-header-menu-separator" role="separator" />}
                <a
                  className="doc-header-menu-item"
                  role="menuitem"
                  href={docDownloadUrl(doc.id, 'md')}
                  download
                  onClick={closeMenu}
                >
                  <FileDown size={16} className="doc-header-menu-icon" />
                  <span>Download Markdown</span>
                </a>
                <a
                  className="doc-header-menu-item"
                  role="menuitem"
                  href={docDownloadUrl(doc.id, 'zip')}
                  download
                  onClick={closeMenu}
                >
                  <FileArchive size={16} className="doc-header-menu-icon" />
                  <span>Download with images (.zip)</span>
                </a>
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

          {(showModeBadge || doc.project_id) && (
            <div className="doc-header-meta">
              {showModeBadge && <DocModeBadge mode={doc.mode} />}
              {doc.project_id && (
                <Link
                  to={docsListPath(doc.project_id)}
                  className="doc-header-project-chip"
                  title={`All docs in ${projectName}`}
                >
                  <Folder size={12} strokeWidth={2.25} aria-hidden="true" />
                  <span className="doc-header-project-name">{projectName}</span>
                </Link>
              )}
            </div>
          )}
        </div>

        <div className="doc-header-actions">
          <button
            type="button"
            className="doc-header-source-toggle"
            onClick={onToggleSource}
            title={showSource ? 'Show the rendered doc' : 'Show the raw markdown'}
          >
            {showSource ? <Eye size={16} aria-hidden="true" /> : <Code size={16} aria-hidden="true" />}
            <span>{showSource ? 'Show rendered' : 'Show source'}</span>
          </button>
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
          title={`Delete '${doc.title}'?`}
          confirmLabel="Delete"
          busyLabel="Deleting..."
          tone="danger"
          busy={deleting}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onClose={() => setDeleteOpen(false)}
        >
          <p>This cannot be undone.</p>
        </DocConfirmDialog>
      )}
    </div>
  );
}
