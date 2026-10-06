/**
 * DocShareDialog -- the owner's Share dialog (Phase 3), opened from the doc
 * header's Share item / share chip (DocViewer renders it for `can_share`
 * only). Four parts, top to bottom:
 *   - add a person: "Name or email" field with a debounced user typeahead
 *     (min 2 chars, the Settings > Skills share search; the suggestions
 *     render in flow under the row so the scrolling body never clips them),
 *     a Can view / Can edit select and Add (Enter submits; text without an
 *     "@" is never sent -- a single suggestion is picked, otherwise the user
 *     is asked to pick or type the full email);
 *   - "Everyone on this install": No access / Can view / Can edit. Widening
 *     it (a new everyone grant, or view -> edit) goes through a confirm,
 *     since a native select fires `change` on arrow keys and keyboard
 *     browsing must never grant install-wide access by itself;
 *   - the roster of per-person grants, each with a permission select and a
 *     remove button;
 *   - notes on what sharing means for this doc (utils/docSharing
 *     shareDialogNotes, the shipped access matrix).
 * Every write (POST /docs/{id}/shares upsert, DELETE .../shares/{share_id})
 * returns the owner's row: it is reported through `onRowApplied` AND shown
 * at once from a local roster seeded from `doc.shares`, which yields to the
 * prop as soon as `doc.shares` changes (the applied row, or a refresh).
 *
 * One request at a time. While it is in flight the controls are
 * aria-disabled and ignore input -- not `disabled`, which would drop the
 * focus of the control the user just changed -- and the dialog cannot be
 * closed (like DocConfirmDialog), so no result goes unreported. A failure
 * shows the server's message in the single error line. Focus: the field on
 * open, back to it after Add, to the next row's remove button (else the
 * field) after a removal, to the everyone select after its confirm, and to
 * whatever had it before the dialog opened on close.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Trash2, Users, X } from 'lucide-react';
import { searchUsers } from '../../api/client';
import { removeDocShare, shareDoc } from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type {
  Doc,
  DocDetail,
  DocShare,
  DocSharePermission,
  UserSearchResult,
} from '../../api/types';
import {
  directShares,
  everyoneShare,
  permissionLabel,
  shareDialogNotes,
  shareDisplayName,
} from '../../utils/docSharing';
import { ModalShell } from '../ModalShell';
import { DocConfirmDialog } from './DocConfirmDialog';
import './DocShareDialog.css';

/** GET /users/search needs at least this many characters. */
export const SHARE_SEARCH_MIN_CHARS = 2;
/** Quiet period after the last keystroke before the user search runs. */
export const SHARE_SEARCH_DEBOUNCE_MS = 300;

const PERMISSIONS: DocSharePermission[] = ['read', 'write'];
/** The everyone select's "no grant" value. */
const NO_ACCESS = 'none';
const EVERYONE_KEY = 'everyone';
const GENERIC_ERROR = "Couldn't update sharing. Try again.";
const PICK_OR_EMAIL_ERROR = 'Pick someone from the list or type their full email.';

export interface DocShareDialogProps {
  doc: DocDetail;
  isOpen: boolean;
  onClose: () => void;
  /** The owner's row returned by a share add / change / remove. */
  onRowApplied: (row: Doc) => void;
}

/**
 * The roster from the latest response, tagged with the doc and the
 * `doc.shares` it was received over: it is shown only while the prop still
 * holds that same array, so any newer prop (the applied row, a refresh)
 * takes over again.
 */
interface RosterOverride {
  docId: string;
  base: DocShare[] | undefined;
  shares: DocShare[];
}

/** The select value a request in flight is changing (one at a time). */
interface PendingValue {
  key: string;
  value: string;
}

/** Where the focus goes once the next render has committed. */
type FocusTarget =
  | { kind: 'input' }
  | { kind: 'everyone' }
  | { kind: 'remove'; shareId: number };

function shareKey(share: DocShare): string {
  return `share:${share.id}`;
}

/** True when moving the everyone grant from `from` to `to` widens access. */
function widensEveryone(from: DocSharePermission | null, to: DocSharePermission): boolean {
  return from === null || (from === 'read' && to === 'write');
}

export function DocShareDialog({ doc, isOpen, onClose, onRowApplied }: DocShareDialogProps) {
  const [override, setOverride] = useState<RosterOverride | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<PendingValue | null>(null);
  const [error, setError] = useState<string | null>(null);
  // An everyone grant awaiting the user's confirmation.
  const [confirmEveryone, setConfirmEveryone] = useState<DocSharePermission | null>(null);

  const [email, setEmail] = useState('');
  const [addPermission, setAddPermission] = useState<DocSharePermission>('read');
  const [results, setResults] = useState<UserSearchResult[]>([]);
  // The query `results` answer (a newer keystroke may still be debouncing).
  const [resultsQuery, setResultsQuery] = useState('');
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);

  const inputRef = useRef<HTMLInputElement>(null);
  const everyoneSelectRef = useRef<HTMLSelectElement>(null);
  const removeButtonsRef = useRef(new Map<number, HTMLButtonElement>());
  const confirmBodyRef = useRef<HTMLParagraphElement>(null);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Bumped by every keystroke / pick / close; a search answer for an older
  // value is dropped so it can never reopen the list over a newer one.
  const searchSeqRef = useRef(0);
  // The latest props, for responses that land after a re-render.
  const docIdRef = useRef(doc.id);
  docIdRef.current = doc.id;
  const docSharesRef = useRef(doc.shares);
  docSharesRef.current = doc.shares;

  // Focus requests are carried out after the render they wait for (a new
  // roster row, a closed confirm): set the target, then bump the tick.
  const focusTargetRef = useRef<FocusTarget | null>(null);
  const [focusTick, setFocusTick] = useState(0);
  const focusAfterRender = useCallback((target: FocusTarget) => {
    focusTargetRef.current = target;
    setFocusTick((n) => n + 1);
  }, []);

  const titleId = useId();
  const listboxId = useId();

  const shares =
    override && override.docId === doc.id && override.base === doc.shares
      ? override.shares
      : (doc.shares ?? []);
  const everyone = everyoneShare(shares);
  const roster = directShares(shares);
  const rosterUserIds = new Set(roster.map((share) => share.user_id));
  const notes = shareDialogNotes(doc);
  const listOpen = dropdownOpen && results.length > 0;

  const cancelSearch = useCallback(() => {
    searchSeqRef.current += 1;
    if (searchTimerRef.current) {
      clearTimeout(searchTimerRef.current);
      searchTimerRef.current = null;
    }
  }, []);

  // Each open starts clean (no leftover input, results, error or confirm)
  // with the field focused; closing cancels a pending search and gives the
  // focus back to whatever had it before.
  useEffect(() => {
    if (!isOpen) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelSearch();
    setEmail('');
    setAddPermission('read');
    setResults([]);
    setDropdownOpen(false);
    setActiveIndex(-1);
    setError(null);
    setConfirmEveryone(null);
    const timer = setTimeout(() => inputRef.current?.focus(), 50);
    return () => {
      clearTimeout(timer);
      cancelSearch();
      if (opener && opener !== document.body && opener.isConnected) opener.focus();
    };
  }, [isOpen, cancelSearch]);

  // No search may fire after the dialog is gone.
  useEffect(() => cancelSearch, [cancelSearch]);

  useEffect(() => {
    const target = focusTargetRef.current;
    if (!target) return;
    focusTargetRef.current = null;
    let el: HTMLElement | null | undefined;
    if (target.kind === 'everyone') el = everyoneSelectRef.current;
    else if (target.kind === 'remove') el = removeButtonsRef.current.get(target.shareId);
    (el ?? inputRef.current)?.focus();
  }, [focusTick]);

  // The everyone confirm opens on its Cancel button: a keyboard user who
  // reached it by arrowing through the select must not grant by Enter.
  useEffect(() => {
    if (confirmEveryone === null) return;
    const dialog = confirmBodyRef.current?.closest('.doc-dialog');
    dialog?.querySelector<HTMLButtonElement>('.doc-dialog-cancel')?.focus();
  }, [confirmEveryone]);

  // Keep the highlighted suggestion visible while arrowing through a long list.
  useEffect(() => {
    if (!listOpen || activeIndex < 0) return;
    document.getElementById(`${listboxId}-option-${activeIndex}`)?.scrollIntoView?.({ block: 'nearest' });
  }, [listOpen, activeIndex, listboxId]);

  /** Run one share write; the returned row, or null when it failed. */
  const apply = useCallback(
    async (request: () => Promise<Doc>, pendingValue: PendingValue | null = null) => {
      setBusy(true);
      setPending(pendingValue);
      setError(null);
      try {
        const row = await request();
        if (row.id === docIdRef.current) {
          setOverride({ docId: row.id, base: docSharesRef.current, shares: row.shares ?? [] });
        }
        onRowApplied(row);
        return row;
      } catch (err) {
        setError(err instanceof ApiClientError && err.message ? err.message : GENERIC_ERROR);
        return null;
      } finally {
        setBusy(false);
        setPending(null);
      }
    },
    [onRowApplied],
  );

  const closeDropdown = useCallback(() => {
    setDropdownOpen(false);
    setActiveIndex(-1);
  }, []);

  const handleEmailChange = (value: string) => {
    if (busy) return;
    setEmail(value);
    setActiveIndex(-1);
    cancelSearch();
    const query = value.trim();
    if (query.length < SHARE_SEARCH_MIN_CHARS) {
      setResults([]);
      setDropdownOpen(false);
      return;
    }
    const seq = searchSeqRef.current;
    searchTimerRef.current = setTimeout(async () => {
      searchTimerRef.current = null;
      try {
        const response = await searchUsers(query);
        if (seq !== searchSeqRef.current) return;
        setResults(response.users);
        setResultsQuery(query);
        setDropdownOpen(response.users.length > 0);
      } catch {
        if (seq !== searchSeqRef.current) return;
        setResults([]);
        setDropdownOpen(false);
      }
    }, SHARE_SEARCH_DEBOUNCE_MS);
  };

  const pickUser = (user: UserSearchResult) => {
    cancelSearch();
    setEmail(user.email);
    setResults([]);
    setError(null);
    closeDropdown();
    inputRef.current?.focus();
  };

  const submitAdd = async () => {
    const target = email.trim();
    if (!target || busy) return;
    if (!target.includes('@')) {
      // A name, not an address: only a suggestion for this very text can
      // resolve it.
      if (results.length === 1 && resultsQuery === target) {
        pickUser(results[0]);
      } else {
        setError(PICK_OR_EMAIL_ERROR);
        inputRef.current?.focus();
      }
      return;
    }
    cancelSearch();
    closeDropdown();
    const row = await apply(() => shareDoc(doc.id, { user_email: target, permission: addPermission }));
    if (row) {
      setEmail('');
      setResults([]);
      // Back to the least access for the next person: edit access is
      // granted per person on purpose, never carried over by accident.
      setAddPermission('read');
    }
    focusAfterRender({ kind: 'input' });
  };

  const handleEmailKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    // Keys confirming an IME composition belong to the IME.
    if (e.nativeEvent.isComposing) return;
    if (e.key === 'ArrowDown' && results.length > 0) {
      e.preventDefault();
      setDropdownOpen(true);
      setActiveIndex((i) => (listOpen ? (i + 1) % results.length : 0));
    } else if (e.key === 'ArrowUp' && listOpen) {
      e.preventDefault();
      setActiveIndex((i) => (i <= 0 ? results.length - 1 : i - 1));
    } else if (e.key === 'Escape' && listOpen) {
      // Close the suggestions, not the dialog (ModalShell listens on the
      // document, past this handler).
      e.stopPropagation();
      closeDropdown();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (listOpen && activeIndex >= 0 && activeIndex < results.length) {
        pickUser(results[activeIndex]);
      } else {
        void submitAdd();
      }
    }
  };

  const grantEveryone = async (permission: DocSharePermission) => {
    const row = await apply(
      () => shareDoc(doc.id, { everyone: true, permission }),
      { key: EVERYONE_KEY, value: permission },
    );
    return row !== null;
  };

  const handleEveryoneChange = (value: string) => {
    if (busy) return;
    if (value === NO_ACCESS) {
      if (!everyone) return;
      void apply(() => removeDocShare(doc.id, everyone.id), { key: EVERYONE_KEY, value });
      return;
    }
    const permission = value as DocSharePermission;
    const current = everyone?.permission ?? null;
    if (current === permission) return;
    if (widensEveryone(current, permission)) {
      setError(null);
      setConfirmEveryone(permission);
      return;
    }
    void grantEveryone(permission);
  };

  const cancelEveryoneConfirm = () => {
    if (busy) return;
    setConfirmEveryone(null);
    setError(null);
    focusAfterRender({ kind: 'everyone' });
  };

  const confirmEveryoneGrant = async () => {
    if (confirmEveryone === null || busy) return;
    if (await grantEveryone(confirmEveryone)) {
      setConfirmEveryone(null);
      focusAfterRender({ kind: 'everyone' });
    }
  };

  const handlePersonPermission = (share: DocShare, permission: DocSharePermission) => {
    const userEmail = share.user?.email;
    if (busy || !userEmail || share.permission === permission) return;
    void apply(
      () => shareDoc(doc.id, { user_email: userEmail, permission }),
      { key: shareKey(share), value: permission },
    );
  };

  const handleRemove = async (share: DocShare) => {
    if (busy) return;
    const index = roster.findIndex((s) => s.id === share.id);
    const row = await apply(() => removeDocShare(doc.id, share.id));
    if (!row) return;
    // The row that moved into the removed one's place, else the field.
    const next = directShares(row.shares)[index];
    focusAfterRender(next ? { kind: 'remove', shareId: next.id } : { kind: 'input' });
  };

  const guardedClose = useCallback(() => {
    if (!busy) onClose();
  }, [busy, onClose]);

  const valueFor = (key: string, actual: string) =>
    pending && pending.key === key ? pending.value : actual;
  const everyoneValue =
    confirmEveryone ?? valueFor(EVERYONE_KEY, everyone?.permission ?? NO_ACCESS);

  const activeOptionId = listOpen && activeIndex >= 0 ? `${listboxId}-option-${activeIndex}` : undefined;
  // In flight: announced as disabled, input ignored, focus kept.
  const busyProps = busy ? { 'aria-disabled': true as const } : {};
  const confirmOpen = confirmEveryone !== null;

  return (
    <>
      <ModalShell
        isOpen={isOpen}
        onClose={guardedClose}
        // While the confirm is up, Escape belongs to it alone.
        onEscape={confirmOpen ? null : guardedClose}
        overlayClassName="doc-share-overlay"
        modalClassName="doc-share-dialog"
        ariaLabelledBy={titleId}
      >
        <div className="doc-share-header">
          <h2 id={titleId} className="doc-share-title">
            Share '{doc.title}'
          </h2>
          <button
            type="button"
            className="doc-share-icon-button doc-share-close"
            onClick={guardedClose}
            aria-label="Close"
            {...busyProps}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        <div className="doc-share-body" aria-busy={busy}>
          <form
            className="doc-share-add"
            onSubmit={(e) => {
              e.preventDefault();
              void submitAdd();
            }}
          >
            <input
              ref={inputRef}
              type="text"
              inputMode="email"
              autoComplete="off"
              spellCheck={false}
              className="doc-share-input"
              placeholder="Add people by name or email"
              aria-label="Name or email"
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={listOpen}
              aria-controls={listboxId}
              aria-activedescendant={activeOptionId}
              value={email}
              readOnly={busy}
              {...busyProps}
              onChange={(e) => handleEmailChange(e.target.value)}
              onKeyDown={handleEmailKeyDown}
              onFocus={() => {
                if (results.length > 0) setDropdownOpen(true);
              }}
              onBlur={closeDropdown}
            />
            <select
              className="doc-share-select"
              aria-label="Permission for the person you add"
              value={addPermission}
              onChange={(e) => {
                if (!busy) setAddPermission(e.target.value as DocSharePermission);
              }}
              {...busyProps}
            >
              {PERMISSIONS.map((permission) => (
                <option key={permission} value={permission}>
                  {permissionLabel(permission)}
                </option>
              ))}
            </select>
            <button
              type="submit"
              className="doc-share-add-button"
              disabled={!busy && !email.trim()}
              {...busyProps}
            >
              Add
            </button>
          </form>

          {listOpen && (
            <ul
              id={listboxId}
              className="doc-share-suggestions"
              role="listbox"
              aria-label="Matching people"
              // Keep the focus in the field (a click on the list's scrollbar
              // would otherwise blur it and close the list).
              onMouseDown={(e) => e.preventDefault()}
            >
              {results.map((user, i) => (
                <li
                  key={user.id}
                  id={`${listboxId}-option-${i}`}
                  role="option"
                  aria-selected={i === activeIndex}
                  className={`doc-share-suggestion${i === activeIndex ? ' doc-share-suggestion--active' : ''}`}
                  // mousedown, not click: the field's blur would close the
                  // list before a click lands.
                  onMouseDown={(e) => {
                    e.preventDefault();
                    pickUser(user);
                  }}
                  onMouseEnter={() => setActiveIndex(i)}
                >
                  <span className="doc-share-suggestion-name">{user.name || user.email}</span>
                  <span className="doc-share-suggestion-email">{user.email}</span>
                  {rosterUserIds.has(user.id) && (
                    <span className="doc-share-suggestion-note">Has access</span>
                  )}
                </li>
              ))}
            </ul>
          )}

          {error && !confirmOpen && (
            <div className="doc-share-error" role="alert">
              {error}
            </div>
          )}

          <div className="doc-share-row doc-share-everyone">
            <span className="doc-share-avatar" aria-hidden="true">
              <Users size={16} />
            </span>
            <span className="doc-share-person-text">
              <span className="doc-share-person-name">Everyone on this install</span>
              <span className="doc-share-person-email">Every Quest user</span>
            </span>
            <select
              ref={everyoneSelectRef}
              className="doc-share-select"
              aria-label="Everyone on this install"
              value={everyoneValue}
              onChange={(e) => handleEveryoneChange(e.target.value)}
              {...busyProps}
            >
              <option value={NO_ACCESS}>No access</option>
              {PERMISSIONS.map((permission) => (
                <option key={permission} value={permission}>
                  {permissionLabel(permission)}
                </option>
              ))}
            </select>
            {/* Keeps the select in line with the roster's above its remove buttons. */}
            <span className="doc-share-remove-slot" aria-hidden="true" />
          </div>

          <h3 className="doc-share-section-title">People with access</h3>
          {roster.length === 0 ? (
            <p className="doc-share-empty">
              {everyone ? 'No one added individually.' : 'Not shared with anyone yet.'}
            </p>
          ) : (
            <ul className="doc-share-roster" aria-label="People with access">
              {roster.map((share) => {
                const shareEmail = share.user?.email ?? '';
                const name = shareDisplayName(share);
                const who = shareEmail || name;
                return (
                  <li key={share.id} className="doc-share-row">
                    <span className="doc-share-avatar doc-share-avatar--initial" aria-hidden="true">
                      {name.charAt(0).toUpperCase()}
                    </span>
                    <span className="doc-share-person-text">
                      <span className="doc-share-person-name">{name}</span>
                      {shareEmail && shareEmail !== name && (
                        <span className="doc-share-person-email">{shareEmail}</span>
                      )}
                    </span>
                    <select
                      className="doc-share-select"
                      aria-label={`Permission for ${who}`}
                      value={valueFor(shareKey(share), share.permission)}
                      onChange={(e) =>
                        handlePersonPermission(share, e.target.value as DocSharePermission)
                      }
                      // Grants are upserted by email: a deleted account's
                      // leftover grant can only be removed.
                      disabled={!shareEmail}
                      {...busyProps}
                    >
                      {PERMISSIONS.map((permission) => (
                        <option key={permission} value={permission}>
                          {permissionLabel(permission)}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      ref={(el) => {
                        if (el) removeButtonsRef.current.set(share.id, el);
                        else removeButtonsRef.current.delete(share.id);
                      }}
                      className="doc-share-icon-button doc-share-remove"
                      aria-label={`Remove ${who}`}
                      title={`Remove ${who}`}
                      onClick={() => void handleRemove(share)}
                      {...busyProps}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="doc-share-notes">
            {notes.map((note) => (
              <p key={note}>{note}</p>
            ))}
          </div>
        </div>

        <div className="doc-share-footer">
          <button type="button" className="doc-share-done" onClick={guardedClose} {...busyProps}>
            Done
          </button>
        </div>
      </ModalShell>

      <DocConfirmDialog
        isOpen={isOpen && confirmOpen}
        title="Share with everyone on this install?"
        confirmLabel="Share with everyone"
        busyLabel="Sharing..."
        busy={busy}
        error={confirmOpen ? error : null}
        onConfirm={() => void confirmEveryoneGrant()}
        onClose={cancelEveryoneConfirm}
      >
        <p ref={confirmBodyRef}>
          {confirmEveryone === 'write'
            ? 'Every Quest user will be able to open this doc and edit it.'
            : 'Every Quest user will be able to open this doc.'}
        </p>
      </DocConfirmDialog>
    </>
  );
}
