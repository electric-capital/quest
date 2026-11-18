/**
 * AutoProjectHeader -- the title unit at the top of the AutoProject page (/autoprojects/<id>).
 *
 * Same interaction model as DocHeader / ConversationHeader: the title plus a
 * chevron open a small dropdown (`role="menu"`, closed by an outside click or
 * Escape), single-key hints act while it is open, and Rename swaps the title
 * for an inline input. Items (spec 8.3: title, archive and delete live here,
 * not in the panel):
 *
 * - Rename (R): Enter or blur saves through PATCH /autoprojects/{id}, Escape
 *   cancels; an empty or unchanged title cancels. A 400 `invalid_title`
 *   keeps the input open with the server's message under it; blurring away
 *   from the refused title gives up instead of resubmitting it.
 * - Archive (A) on an unarchived AutoProject, disabled while a run is active (the
 *   server refuses with 409 `autoproject_run_active`, still handled since the shown
 *   state can lag); Unarchive (U) on an archived one (409
 *   `autoprojects_cap_reached`).
 * - Delete (D), behind a danger confirm with the AutoProjects list's wording; on
 *   success the page navigates to the AutoProjects list (replace: the AutoProject route
 *   would 404), failures show in the dialog.
 *
 * The "Archived" chip sits beside the title. Writes hand the returned AutoProject
 * to `onAutoProjectDetail` (useAutoProject.applyDetail) rather than waiting for the
 * realtime re-fetch. Failures show as a dismissible AutoProjectNotice under the
 * title until dismissed or the next menu action; the archive confirmation is
 * a transient status line (NOTICE_MS).
 *
 * Keyboard: opening the menu with the keyboard (Enter / Space, or
 * ArrowDown / ArrowUp on the title) focuses its first (last) enabled item;
 * ArrowDown / ArrowUp move between the items (wrapping), Home / End jump to
 * the ends, Tab closes the menu (focus continues from the title), Escape
 * closes it and returns focus to the title. Focus also comes back to the
 * title after a rename ended with Enter or Escape and after a menu action
 * that leaves nothing to focus (archive; Delete's dialog returns there).
 *
 * `jump` (AutoProjectMainColumn) is a button at the end of the title row that
 * moves between the brief and the newest update (AutoProjectHeaderJump), with a
 * dot on "Latest" while a run works (pulsing) or the AutoProject is unread. The
 * header sits above the page's scroller, so it stays on screen.
 *
 * Styles in AutoProjectHeader.css.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Archive, ArchiveRestore, ArrowDown, ArrowUp, ChevronDown, Pencil, Trash2 } from 'lucide-react'
import {
  archiveAutoProject,
  deleteAutoProject,
  AUTOPROJECT_TITLE_MAX_CHARS,
  unarchiveAutoProject,
  updateAutoProject,
} from '../../api/autoprojectsApi'
import { apiErrorCode } from '../../api/request'
import type { AutoProject } from '../../api/types'
import { autoprojectsListPath } from '../../utils/autoprojectsRoute'
import { errorMessage } from '../../utils/errorMessage'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { AutoProjectNotice } from './AutoProjectNotice'
import './AutoProjectHeader.css'

const NOTICE_MS = 5000
const ARCHIVED_NOTICE = 'AutoProject archived. It is hidden from the sidebar and takes no new prompts.'
const CAP_REACHED_FALLBACK =
  'You have reached the limit of active AutoProjects; archive or delete another AutoProject first.'

const RUN_ACTIVE_MESSAGE =
  'An update is still in progress. Stop it or wait for it to finish before archiving.'

function jumpLabel(jump: AutoProjectHeaderJump): string {
  if (jump.target === 'brief') return 'Back to the brief'
  if (jump.mark === 'working') return 'Jump to the newest update (in progress)'
  if (jump.mark === 'unread') return 'Jump to the newest update (not seen yet)'
  return 'Jump to the newest update'
}

interface HeaderNotice {
  text: string
  /** `status`: transient confirmation; `error`: stays until dismissed. */
  tone: 'status' | 'error'
  /** Restarts the timer when the same text is shown again. */
  seq: number
}

/** Where the jump button goes: up to the brief, or down to the newest update. */
export type AutoProjectHeaderJumpTarget = 'brief' | 'latest'

/** The header's jump button (AutoProjectMainColumn owns the scroller it moves). */
export interface AutoProjectHeaderJump {
  /** `latest` while the newest update is out of view, `brief` once scrolled down with it in view. */
  target: AutoProjectHeaderJumpTarget
  onJump: (target: AutoProjectHeaderJumpTarget) => void
  /** On "Latest": a run is working, or the AutoProject has an update not yet seen. */
  mark: 'working' | 'unread' | null
  /** The target is above the view (the brief; the newest update, recent first). */
  up: boolean
}

export interface AutoProjectHeaderProps {
  autoproject: AutoProject
  /** An AutoProject returned by rename / archive / unarchive (useAutoProject.applyDetail). */
  onAutoProjectDetail?: (autoproject: AutoProject) => void
  /** The jump between the brief and the newest update; none while both are in view. */
  jump?: AutoProjectHeaderJump
}

/** Which menu item to focus once the menu has opened. */
type MenuFocus = 'first' | 'last' | null

const MENU_ITEM_SELECTOR = '[role="menuitem"]:not(:disabled)'

export function AutoProjectHeader({ autoproject, onAutoProjectDetail, jump }: AutoProjectHeaderProps) {
  const navigate = useNavigate()
  const activeRun = autoproject.active_run
  const canArchive = !autoproject.archived && activeRun === null

  const [menuOpen, setMenuOpen] = useState(false)
  const titleButtonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  // Set by a keyboard open; read once the menu is in the DOM.
  const menuFocusRef = useRef<MenuFocus>(null)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [renameSaving, setRenameSaving] = useState(false)
  const [renameError, setRenameError] = useState<string | null>(null)
  const renameInputRef = useRef<HTMLInputElement>(null)
  // Guards the blur-after-Enter double submit and the blur while saving.
  const renameSubmittedRef = useRef(false)
  // The last title the server refused while the input stayed open.
  const rejectedTitleRef = useRef<string | null>(null)
  // A rename ended from the keyboard (Enter / Escape): focus the title again.
  const restoreTitleFocusRef = useRef(false)

  const [archiving, setArchiving] = useState(false)

  const [notice, setNotice] = useState<HeaderNotice | null>(null)
  const noticeSeqRef = useRef(0)
  const showNotice = useCallback((text: string, tone: HeaderNotice['tone']) => {
    noticeSeqRef.current += 1
    setNotice({ text, tone, seq: noticeSeqRef.current })
  }, [])

  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useEffect(() => {
    if (!notice || notice.tone !== 'status') return
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS)
    return () => window.clearTimeout(timer)
  }, [notice])

  // Close the menu on any outside click (the toggle stops propagation).
  useEffect(() => {
    if (!menuOpen) return
    const close = () => setMenuOpen(false)
    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [menuOpen])

  const toggleMenu = useCallback((focus: MenuFocus) => {
    setNotice(null)
    menuFocusRef.current = menuOpen ? null : focus
    setMenuOpen(!menuOpen)
  }, [menuOpen])

  const openMenu = useCallback((focus: MenuFocus) => {
    setNotice(null)
    menuFocusRef.current = focus
    setMenuOpen(true)
  }, [])

  const menuItems = useCallback(
    (): HTMLElement[] => Array.from(menuRef.current?.querySelectorAll<HTMLElement>(MENU_ITEM_SELECTOR) ?? []),
    [],
  )

  // A keyboard open puts focus on the first (or last) enabled item.
  useEffect(() => {
    if (!menuOpen) return
    const focus = menuFocusRef.current
    menuFocusRef.current = null
    if (!focus) return
    const items = menuItems()
    const item = focus === 'first' ? items[0] : items[items.length - 1]
    item?.focus()
  }, [menuOpen, menuItems])

  /**
   * Close the menu from one of its actions. Focus on an item would be lost
   * with the menu, so it moves to the title first (Delete's dialog then
   * returns focus there when it closes).
   */
  const closeMenuFromItem = useCallback(() => {
    if (menuRef.current?.contains(document.activeElement)) titleButtonRef.current?.focus()
    setMenuOpen(false)
  }, [])

  // --- Rename ---------------------------------------------------------------

  const startRename = useCallback(() => {
    setMenuOpen(false)
    setNotice(null)
    restoreTitleFocusRef.current = false
    setRenameError(null)
    renameSubmittedRef.current = false
    rejectedTitleRef.current = null
    setRenameValue(autoproject.title)
    setRenaming(true)
  }, [autoproject.title])

  useEffect(() => {
    if (renaming) {
      renameInputRef.current?.focus()
      renameInputRef.current?.select()
    } else if (restoreTitleFocusRef.current) {
      restoreTitleFocusRef.current = false
      titleButtonRef.current?.focus()
    }
  }, [renaming])

  /** End the rename; `restoreFocus` (Enter / Escape) puts focus back on the title. */
  const cancelRename = useCallback((restoreFocus = false) => {
    restoreTitleFocusRef.current = restoreFocus
    renameSubmittedRef.current = true
    rejectedTitleRef.current = null
    setRenaming(false)
    setRenameValue('')
    setRenameError(null)
  }, [])

  const submitRename = useCallback(
    async (trigger: 'enter' | 'blur') => {
      if (renameSubmittedRef.current) return
      const fromKeyboard = trigger === 'enter'
      const title = renameValue.trim()
      if (!title || title === autoproject.title) {
        cancelRename(fromKeyboard)
        return
      }
      if (trigger === 'blur' && title === rejectedTitleRef.current) {
        cancelRename()
        return
      }
      renameSubmittedRef.current = true
      setNotice(null)
      setRenameError(null)
      setRenameSaving(true)
      try {
        const updated = await updateAutoProject(autoproject.id, { title })
        onAutoProjectDetail?.(updated)
        restoreTitleFocusRef.current = fromKeyboard
        setRenaming(false)
        setRenameValue('')
      } catch (err) {
        if (apiErrorCode(err) === 'invalid_title') {
          // Fixable by editing the typed title: keep the input open.
          setRenameError(errorMessage(err, 'That title cannot be used.'))
          rejectedTitleRef.current = title
          renameSubmittedRef.current = false
          if (trigger === 'enter') renameInputRef.current?.focus()
        } else {
          showNotice(errorMessage(err, 'The AutoProject could not be renamed.'), 'error')
          restoreTitleFocusRef.current = fromKeyboard
          setRenaming(false)
          setRenameValue('')
        }
      } finally {
        setRenameSaving(false)
      }
    },
    [cancelRename, onAutoProjectDetail, renameValue, showNotice, autoproject.id, autoproject.title],
  )

  // --- Archive / unarchive --------------------------------------------------

  const runArchive = useCallback(
    async (action: 'archive' | 'unarchive') => {
      closeMenuFromItem()
      setNotice(null)
      if (archiving) return
      setArchiving(true)
      try {
        const updated = action === 'archive' ? await archiveAutoProject(autoproject.id) : await unarchiveAutoProject(autoproject.id)
        onAutoProjectDetail?.(updated)
        if (action === 'archive') showNotice(ARCHIVED_NOTICE, 'status')
      } catch (err) {
        const code = apiErrorCode(err)
        if (code === 'autoproject_run_active') {
          showNotice(RUN_ACTIVE_MESSAGE, 'error')
        } else if (code === 'autoprojects_cap_reached') {
          showNotice(errorMessage(err, CAP_REACHED_FALLBACK), 'error')
        } else {
          const verb = action === 'archive' ? 'archived' : 'unarchived'
          showNotice(errorMessage(err, `The AutoProject could not be ${verb}.`), 'error')
        }
      } finally {
        setArchiving(false)
      }
    },
    [archiving, closeMenuFromItem, onAutoProjectDetail, showNotice, autoproject.id],
  )

  // --- Delete ---------------------------------------------------------------

  const openDelete = useCallback(() => {
    closeMenuFromItem()
    setNotice(null)
    setDeleteError(null)
    setDeleteOpen(true)
  }, [closeMenuFromItem])

  const leaveDeleted = useCallback(() => {
    setDeleteOpen(false)
    // Back to the list the AutoProject was in; replace, since the AutoProject route is gone.
    navigate(autoprojectsListPath({ archived: autoproject.archived }), { replace: true })
  }, [navigate, autoproject.archived])

  const confirmDelete = useCallback(async () => {
    setDeleting(true)
    setDeleteError(null)
    try {
      await deleteAutoProject(autoproject.id)
      setDeleting(false)
      leaveDeleted()
    } catch (err) {
      setDeleting(false)
      if (apiErrorCode(err) === 'autoproject_not_found') {
        // Already gone (another tab): leave as on success.
        leaveDeleted()
      } else {
        setDeleteError(errorMessage(err, 'The AutoProject could not be deleted.'))
      }
    }
  }, [leaveDeleted, autoproject.id])

  // --- Keys -------------------------------------------------------------------

  // While the menu is open: single-key shortcuts (mirror the hint letters)
  // and the menu's keyboard navigation.
  useEffect(() => {
    if (!menuOpen) return
    const moveFocus = (to: 'next' | 'prev' | 'first' | 'last') => {
      const items = menuItems()
      if (items.length === 0) return
      const at = items.indexOf(document.activeElement as HTMLElement)
      let next: number
      if (to === 'first') next = 0
      else if (to === 'last') next = items.length - 1
      else if (to === 'next') next = at < 0 ? 0 : (at + 1) % items.length
      else next = at < 0 ? items.length - 1 : (at - 1 + items.length) % items.length
      items[next].focus()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const key = e.key.toLowerCase()
      if (key === 'r') { e.preventDefault(); startRename() }
      else if (key === 'a' && canArchive && !archiving) { e.preventDefault(); void runArchive('archive') }
      else if (key === 'u' && autoproject.archived && !archiving) { e.preventDefault(); void runArchive('unarchive') }
      else if (key === 'd') { e.preventDefault(); openDelete() }
      else if (key === 'arrowdown') { e.preventDefault(); moveFocus('next') }
      else if (key === 'arrowup') { e.preventDefault(); moveFocus('prev') }
      else if (key === 'home') { e.preventDefault(); moveFocus('first') }
      else if (key === 'end') { e.preventDefault(); moveFocus('last') }
      else if (key === 'escape') {
        e.preventDefault()
        titleButtonRef.current?.focus()
        setMenuOpen(false)
      } else if (key === 'tab') {
        // No preventDefault: Tab carries on from the title, as if the menu
        // had never been entered.
        if (menuRef.current?.contains(document.activeElement)) titleButtonRef.current?.focus()
        setMenuOpen(false)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [menuOpen, canArchive, archiving, autoproject.archived, startRename, runArchive, openDelete, menuItems])

  const titleLength = renameValue.length

  return (
    <header className="autoproject-header">
      <div className="autoproject-header-bar">
        <div className="autoproject-header-unit">
          {renaming ? (
            <div className="autoproject-header-rename">
              <input
                ref={renameInputRef}
                className="autoproject-header-rename-input"
                value={renameValue}
                placeholder="AutoProject title"
                maxLength={AUTOPROJECT_TITLE_MAX_CHARS}
                readOnly={renameSaving}
                aria-busy={renameSaving}
                aria-invalid={renameError !== null}
                aria-describedby="autoproject-header-rename-count"
                onChange={(e) => {
                  setRenameValue(e.target.value)
                  setRenameError(null)
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); void submitRename('enter') }
                  else if (e.key === 'Escape') { e.preventDefault(); cancelRename(true) }
                }}
                onBlur={() => void submitRename('blur')}
                aria-label="Rename AutoProject"
              />
              <span
                id="autoproject-header-rename-count"
                className={`autoproject-header-rename-count${titleLength >= AUTOPROJECT_TITLE_MAX_CHARS ? ' at-cap' : ''}`}
              >
                {titleLength} / {AUTOPROJECT_TITLE_MAX_CHARS}
              </span>
            </div>
          ) : (
            <h1 className="autoproject-header-heading">
              <button
                ref={titleButtonRef}
                type="button"
                className={`autoproject-header-title-button${menuOpen ? ' open' : ''}`}
                onClick={(e) => {
                  e.stopPropagation()
                  // detail 0: activated from the keyboard (Enter / Space).
                  toggleMenu(e.detail === 0 ? 'first' : null)
                }}
                onKeyDown={(e) => {
                  if (menuOpen || e.metaKey || e.ctrlKey || e.altKey) return
                  if (e.key === 'ArrowDown') { e.preventDefault(); openMenu('first') }
                  else if (e.key === 'ArrowUp') { e.preventDefault(); openMenu('last') }
                }}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                title={autoproject.title}
              >
                <span className="autoproject-header-title">{autoproject.title}</span>
                <span className="autoproject-header-chevron" aria-hidden="true">
                  <ChevronDown size={16} />
                </span>
              </button>
            </h1>
          )}

          {menuOpen && (
            <div
              ref={menuRef}
              className="autoproject-header-menu"
              role="menu"
              aria-label="AutoProject actions"
              onClick={(e) => e.stopPropagation()}
            >
              <button type="button" className="autoproject-header-menu-item" role="menuitem" tabIndex={-1} onClick={startRename}>
                <Pencil size={16} className="autoproject-header-menu-icon" aria-hidden="true" />
                <span>Rename</span>
                <span className="autoproject-header-menu-key">R</span>
              </button>
              {autoproject.archived ? (
                <button
                  type="button"
                  className="autoproject-header-menu-item"
                  role="menuitem"
                  tabIndex={-1}
                  disabled={archiving}
                  onClick={() => void runArchive('unarchive')}
                  title="Move this AutoProject back to the active list"
                >
                  <ArchiveRestore size={16} className="autoproject-header-menu-icon" aria-hidden="true" />
                  <span>Unarchive</span>
                  <span className="autoproject-header-menu-key">U</span>
                </button>
              ) : (
                <button
                  type="button"
                  className="autoproject-header-menu-item"
                  role="menuitem"
                  tabIndex={-1}
                  disabled={!canArchive || archiving}
                  onClick={() => void runArchive('archive')}
                  title={
                    activeRun
                      ? RUN_ACTIVE_MESSAGE
                      : 'Hide this AutoProject from the sidebar; it takes no new prompts until unarchived'
                  }
                >
                  <Archive size={16} className="autoproject-header-menu-icon" aria-hidden="true" />
                  <span>Archive</span>
                  <span className="autoproject-header-menu-key">A</span>
                </button>
              )}
              <div className="autoproject-header-menu-separator" role="separator" />
              <button
                type="button"
                className="autoproject-header-menu-item autoproject-header-menu-item--danger"
                role="menuitem"
                tabIndex={-1}
                onClick={openDelete}
              >
                <Trash2 size={16} className="autoproject-header-menu-icon" aria-hidden="true" />
                <span>Delete</span>
                <span className="autoproject-header-menu-key">D</span>
              </button>
            </div>
          )}
        </div>

        {autoproject.archived && <span className="autoproject-header-chip">Archived</span>}

        {jump && !renaming && (
          <button
            type="button"
            className="autoproject-header-jump"
            aria-label={jumpLabel(jump)}
            title={jumpLabel(jump)}
            onClick={() => jump.onJump(jump.target)}
          >
            {jump.up ? (
              <ArrowUp size={16} aria-hidden="true" />
            ) : (
              <ArrowDown size={16} aria-hidden="true" />
            )}
            <span>{jump.target === 'brief' ? 'Brief' : 'Latest'}</span>
            {jump.mark && <span className={`autoproject-header-jump-mark ${jump.mark}`} aria-hidden="true" />}
          </button>
        )}
      </div>

      {renaming && renameError && (
        <p className="autoproject-header-rename-error" role="alert">
          {renameError}
        </p>
      )}

      {notice?.tone === 'status' && (
        <div className="autoproject-header-notice" role="status" aria-live="polite">
          {notice.text}
        </div>
      )}
      {notice?.tone === 'error' && (
        <div className="autoproject-header-error">
          <AutoProjectNotice message={notice.text} onDismiss={() => setNotice(null)} />
        </div>
      )}

      <DocConfirmDialog
        isOpen={deleteOpen}
        title="Delete AutoProject?"
        confirmLabel="Delete"
        busyLabel="Deleting..."
        tone="danger"
        busy={deleting}
        error={deleteError}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteOpen(false)}
      >
        <p>
          <strong>{autoproject.title}</strong> is deleted for good, with all its updates and the work
          behind them, its brief and its files.
        </p>
        {activeRun && <p>The update in progress will be stopped.</p>}
        <p>This cannot be undone.</p>
      </DocConfirmDialog>
    </header>
  )
}
