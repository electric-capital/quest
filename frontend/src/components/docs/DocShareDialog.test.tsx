import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../../api/request';
import type {
  Doc,
  DocDetail,
  DocShare,
  DocUserRef,
  ShareDocRequest,
  UserSearchResponse,
} from '../../api/types';
import { DocShareDialog, SHARE_SEARCH_DEBOUNCE_MS } from './DocShareDialog';

const mocks = vi.hoisted(() => ({
  shareDoc: vi.fn<(id: string, body: ShareDocRequest) => Promise<Doc>>(),
  removeDocShare: vi.fn<(id: string, shareId: number) => Promise<Doc>>(),
  searchUsers: vi.fn<(query: string) => Promise<UserSearchResponse>>(),
}));

vi.mock('../../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/docsApi')>()),
  shareDoc: mocks.shareDoc,
  removeDocShare: mocks.removeDocShare,
}));

vi.mock('../../api/client', () => ({
  searchUsers: mocks.searchUsers,
}));

const ANA = { id: 2, name: 'Ana Lopez', email: 'ana@x.test' };
const BO = { id: 3, name: 'Bo Chen', email: 'bo@x.test' };
const CY = { id: 4, name: 'Cy Diaz', email: 'cy@x.test' };

const NOTE_APPROVAL = 'Once shared, Quest asks for approval before any conversation changes this doc.';
const NOTE_USER_DOC = 'People you add can open it in Quest and use it in their own conversations.';
const NOTE_PROJECT_DOC =
  "People you add can open it in Quest; their conversations can't use project docs.";
const NOTE_DIRECT_EDIT =
  'People with edit access can change it directly in Quest; only changes proposed by conversations need approval.';
const NOTE_HISTORY =
  'People with edit access can also see and restore earlier versions, including ones from before you shared it (kept for 30 days).';

function share(
  id: number,
  user: DocUserRef | null,
  permission: DocShare['permission'] = 'read',
): DocShare {
  return { id, user_id: user?.id ?? null, permission, created_at: '2026-10-01T00:00:00', user };
}

function detail(overrides: Partial<DocDetail> = {}): DocDetail {
  return {
    id: 'd1',
    owner_id: 1,
    project_id: null,
    title: 'Launch plan',
    description: '',
    mode: 'private',
    content_size: 10,
    asset_count: 0,
    require_approval: false,
    last_write_source: null,
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    scope: overrides.project_id ? 'project' : 'user',
    shared: (overrides.shares ?? []).length > 0,
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    access: {
      can_rename: true,
      can_switch_mode: false,
      can_delete: true,
      write: 'free',
      can_edit: true,
      can_share: true,
      can_delete_assets: true, can_require_approval: true,
    },
    shares: [],
    content: '# Launch',
    last_write_conversation: null,
    assets: [],
    ...overrides,
  };
}

/** The owner's row a share write returns: no body, the new roster. */
function row(shares: DocShare[]): Doc {
  const full: Partial<DocDetail> = detail({ shares });
  delete full.content;
  delete full.last_write_conversation;
  delete full.assets;
  return full as Doc;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderDialog(
  doc: DocDetail,
  props: { onClose?: () => void; onRowApplied?: (row: Doc) => void } = {},
) {
  const onClose = props.onClose ?? vi.fn();
  const onRowApplied = props.onRowApplied ?? vi.fn();
  const utils = render(
    <DocShareDialog doc={doc} isOpen onClose={onClose} onRowApplied={onRowApplied} />,
  );
  return { ...utils, onClose, onRowApplied };
}

function shareDialog(): HTMLElement {
  return screen.getByRole('dialog', { name: "Share 'Launch plan'" });
}

function emailInput(): HTMLInputElement {
  return screen.getByRole('combobox', { name: 'Name or email' }) as HTMLInputElement;
}

function addPermissionSelect(): HTMLSelectElement {
  return screen.getByRole('combobox', { name: 'Permission for the person you add' }) as HTMLSelectElement;
}

function everyoneSelect(): HTMLSelectElement {
  return screen.getByRole('combobox', { name: 'Everyone on this install' }) as HTMLSelectElement;
}

function personSelect(who: string): HTMLSelectElement {
  return screen.getByRole('combobox', { name: `Permission for ${who}` }) as HTMLSelectElement;
}

function button(name: string): HTMLButtonElement {
  return screen.getByRole('button', { name }) as HTMLButtonElement;
}

function roster(): HTMLElement | null {
  return screen.queryByRole('list', { name: 'People with access' });
}

function rosterNames(): string[] {
  const list = roster();
  if (!list) return [];
  return [...list.querySelectorAll('.doc-share-person-name')].map((el) => el.textContent ?? '');
}

function notes(): (string | null)[] {
  return [...document.querySelectorAll('.doc-share-notes p')].map((p) => p.textContent);
}

function confirmTitle(): HTMLElement | null {
  return screen.queryByText('Share with everyone on this install?');
}

async function click(el: HTMLElement) {
  await act(async () => {
    fireEvent.click(el);
  });
}

/** Type, then wait for the suggestions answering exactly this text. */
async function typeAndSearch(value: string) {
  const calls = mocks.searchUsers.mock.calls.length;
  fireEvent.change(emailInput(), { target: { value } });
  await waitFor(() => expect(mocks.searchUsers.mock.calls.length).toBe(calls + 1));
  await act(async () => {
    await mocks.searchUsers.mock.results.at(-1)?.value;
  });
  await screen.findByRole('listbox');
}

/** Let the open-time focus of the field land before moving it elsewhere. */
async function openFocusSettled() {
  await waitFor(() => expect(document.activeElement).toBe(emailInput()));
}

beforeEach(() => {
  mocks.shareDoc.mockReset();
  mocks.removeDocShare.mockReset();
  mocks.searchUsers.mockReset();
  mocks.searchUsers.mockResolvedValue({ users: [] });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe('DocShareDialog', () => {
  it('renders nothing while closed', () => {
    render(<DocShareDialog doc={detail()} isOpen={false} onClose={vi.fn()} onRowApplied={vi.fn()} />);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('is named by its title and opens with an empty roster, no everyone grant and the notes', async () => {
    renderDialog(detail());
    const dialog = shareDialog();
    expect(within(dialog).getByRole('heading', { name: "Share 'Launch plan'" })).toBeTruthy();
    expect(screen.getByText('Not shared with anyone yet.')).toBeTruthy();
    expect(roster()).toBeNull();
    expect(everyoneSelect().value).toBe('none');
    expect(addPermissionSelect().value).toBe('read');
    expect(notes()).toEqual([NOTE_APPROVAL, NOTE_USER_DOC, NOTE_DIRECT_EDIT, NOTE_HISTORY]);
    await waitFor(() => expect(document.activeElement).toBe(emailInput()));
  });

  describe('adding people', () => {
    it('adds a person picked from the debounced typeahead with the chosen permission', async () => {
      mocks.searchUsers.mockResolvedValue({ users: [ANA, BO] });
      const applied = row([share(11, ANA, 'write')]);
      mocks.shareDoc.mockResolvedValue(applied);
      const { onRowApplied } = renderDialog(detail());

      // One character: below the search minimum.
      fireEvent.change(emailInput(), { target: { value: 'a' } });
      // Two quick keystrokes: one search, for the latest value.
      fireEvent.change(emailInput(), { target: { value: 'an' } });
      fireEvent.change(emailInput(), { target: { value: 'ana' } });
      const option = await screen.findByRole('option', { name: /Ana Lopez/ });
      expect(mocks.searchUsers).toHaveBeenCalledTimes(1);
      expect(mocks.searchUsers).toHaveBeenCalledWith('ana');
      expect(within(option).getByText('ana@x.test')).toBeTruthy();
      expect(within(screen.getByRole('listbox')).getAllByRole('option')).toHaveLength(2);

      // Picking fills the email (it does not add yet) and closes the list.
      fireEvent.mouseDown(option);
      expect(emailInput().value).toBe('ana@x.test');
      expect(screen.queryByRole('listbox')).toBeNull();
      expect(mocks.shareDoc).not.toHaveBeenCalled();

      fireEvent.change(addPermissionSelect(), { target: { value: 'write' } });
      await click(button('Add'));

      expect(mocks.shareDoc).toHaveBeenCalledWith('d1', { user_email: 'ana@x.test', permission: 'write' });
      expect(onRowApplied).toHaveBeenCalledWith(applied);
      // Shown at once from the returned row, though the prop never changed.
      expect(rosterNames()).toEqual(['Ana Lopez']);
      expect(within(roster()!).getByText('ana@x.test')).toBeTruthy();
      expect(personSelect('ana@x.test').value).toBe('write');
      expect(screen.queryByText('Not shared with anyone yet.')).toBeNull();
      expect(emailInput().value).toBe('');
      // The next person starts at "Can view" again: edit access is never
      // carried over to someone else by accident.
      expect(addPermissionSelect().value).toBe('read');
      // The Add button disabled itself (empty field): the focus is back in it.
      expect(document.activeElement).toBe(emailInput());
    });

    it('submits on Enter, and Enter on a highlighted suggestion picks it instead', async () => {
      mocks.searchUsers.mockResolvedValue({ users: [ANA, BO] });
      mocks.shareDoc.mockResolvedValue(row([share(12, BO)]));
      renderDialog(detail());

      await typeAndSearch('bo');
      fireEvent.keyDown(emailInput(), { key: 'ArrowDown' });
      fireEvent.keyDown(emailInput(), { key: 'ArrowDown' });
      expect(screen.getByRole('option', { name: /Bo Chen/ }).getAttribute('aria-selected')).toBe('true');
      expect(emailInput().getAttribute('aria-activedescendant')).toBe(
        screen.getByRole('option', { name: /Bo Chen/ }).id,
      );
      fireEvent.keyDown(emailInput(), { key: 'Enter' });
      expect(emailInput().value).toBe('bo@x.test');
      expect(mocks.shareDoc).not.toHaveBeenCalled();

      await act(async () => {
        fireEvent.keyDown(emailInput(), { key: 'Enter' });
      });
      expect(mocks.shareDoc).toHaveBeenCalledWith('d1', { user_email: 'bo@x.test', permission: 'read' });
      expect(rosterNames()).toEqual(['Bo Chen']);
    });

    it('ignores Enter while an IME composition is in progress', async () => {
      mocks.shareDoc.mockResolvedValue(row([share(12, BO)]));
      renderDialog(detail());
      fireEvent.change(emailInput(), { target: { value: 'bo@x.test' } });

      await act(async () => {
        fireEvent.keyDown(emailInput(), { key: 'Enter', isComposing: true });
      });
      expect(mocks.shareDoc).not.toHaveBeenCalled();

      await act(async () => {
        fireEvent.keyDown(emailInput(), { key: 'Enter' });
      });
      expect(mocks.shareDoc).toHaveBeenCalledTimes(1);
    });

    it('never sends a name: one suggestion is picked, otherwise the user is asked to pick', async () => {
      renderDialog(detail());

      // No suggestions for the text: asked to pick or type the email.
      fireEvent.change(emailInput(), { target: { value: 'z' } });
      await act(async () => {
        fireEvent.keyDown(emailInput(), { key: 'Enter' });
      });
      expect(screen.getByRole('alert').textContent).toBe(
        'Pick someone from the list or type their full email.',
      );

      // Several suggestions: same.
      mocks.searchUsers.mockResolvedValueOnce({ users: [ANA, BO] });
      await typeAndSearch('x.test');
      await act(async () => {
        fireEvent.keyDown(emailInput(), { key: 'Enter' });
      });
      expect(screen.getByRole('alert').textContent).toBe(
        'Pick someone from the list or type their full email.',
      );

      // Exactly one: picked (not sent yet).
      mocks.searchUsers.mockResolvedValueOnce({ users: [CY] });
      await typeAndSearch('cy');
      await act(async () => {
        fireEvent.keyDown(emailInput(), { key: 'Enter' });
      });
      expect(emailInput().value).toBe('cy@x.test');
      expect(screen.queryByRole('alert')).toBeNull();
      expect(mocks.shareDoc).not.toHaveBeenCalled();
    });

    it('drops a search answer that a newer keystroke superseded', async () => {
      const first = deferred<UserSearchResponse>();
      const second = deferred<UserSearchResponse>();
      mocks.searchUsers.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
      renderDialog(detail());

      fireEvent.change(emailInput(), { target: { value: 'an' } });
      await waitFor(() => expect(mocks.searchUsers).toHaveBeenCalledTimes(1));
      fireEvent.change(emailInput(), { target: { value: 'bo' } });
      await waitFor(() => expect(mocks.searchUsers).toHaveBeenCalledTimes(2));

      await act(async () => {
        second.resolve({ users: [BO] });
        await second.promise;
      });
      await act(async () => {
        first.resolve({ users: [ANA] });
        await first.promise;
      });
      const options = within(screen.getByRole('listbox')).getAllByRole('option');
      expect(options.map((o) => o.textContent)).toEqual(['Bo Chenbo@x.test']);
    });

    it('cancels a pending search when the dialog closes or unmounts', async () => {
      vi.useFakeTimers();
      const doc = detail();
      const { rerender, unmount } = render(
        <DocShareDialog doc={doc} isOpen onClose={vi.fn()} onRowApplied={vi.fn()} />,
      );
      fireEvent.change(emailInput(), { target: { value: 'ana' } });
      rerender(<DocShareDialog doc={doc} isOpen={false} onClose={vi.fn()} onRowApplied={vi.fn()} />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SHARE_SEARCH_DEBOUNCE_MS * 3);
      });
      expect(mocks.searchUsers).not.toHaveBeenCalled();

      rerender(<DocShareDialog doc={doc} isOpen onClose={vi.fn()} onRowApplied={vi.fn()} />);
      fireEvent.change(emailInput(), { target: { value: 'bo' } });
      unmount();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SHARE_SEARCH_DEBOUNCE_MS * 3);
      });
      expect(mocks.searchUsers).not.toHaveBeenCalled();
    });

    it('closes the suggestions on Escape without closing the dialog', async () => {
      mocks.searchUsers.mockResolvedValue({ users: [ANA] });
      const { onClose } = renderDialog(detail());
      await typeAndSearch('an');

      fireEvent.keyDown(emailInput(), { key: 'Escape' });
      expect(screen.queryByRole('listbox')).toBeNull();
      expect(onClose).not.toHaveBeenCalled();

      // With no list open, Escape closes the dialog.
      fireEvent.keyDown(emailInput(), { key: 'Escape' });
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('keeps the focus in the field on list mousedown and scrolls the highlight into view', async () => {
      const scrollIntoView = vi.fn();
      const original = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = scrollIntoView;
      try {
        mocks.searchUsers.mockResolvedValue({ users: [ANA, BO, CY] });
        renderDialog(detail());
        await typeAndSearch('x.test');
        // mousedown on the list itself (e.g. its scrollbar) is cancelled.
        expect(fireEvent.mouseDown(screen.getByRole('listbox'))).toBe(false);
        fireEvent.keyDown(emailInput(), { key: 'ArrowUp' });
        const last = screen.getByRole('option', { name: /Cy Diaz/ });
        expect(last.getAttribute('aria-selected')).toBe('true');
        expect(scrollIntoView).toHaveBeenLastCalledWith({ block: 'nearest' });
        expect(scrollIntoView.mock.contexts.at(-1)).toBe(last);
      } finally {
        Element.prototype.scrollIntoView = original;
      }
    });

    it('marks a suggestion who already has access', async () => {
      mocks.searchUsers.mockResolvedValue({ users: [ANA, BO] });
      renderDialog(detail({ shares: [share(11, ANA)] }));
      await typeAndSearch('x.test');
      const ana = screen.getByRole('option', { name: /Ana Lopez/ });
      expect(within(ana).getByText('Has access')).toBeTruthy();
      expect(within(screen.getByRole('option', { name: /Bo Chen/ })).queryByText('Has access')).toBeNull();
    });
  });

  describe('errors', () => {
    it('shows the server message inline, one line at a time, and keeps the input', async () => {
      mocks.shareDoc.mockRejectedValueOnce(
        new ApiClientError('No Quest user has that email.', 404, 'user_not_found'),
      );
      renderDialog(detail());
      fireEvent.change(emailInput(), { target: { value: 'nobody@x.test' } });
      await click(button('Add'));
      expect(screen.getByRole('alert').textContent).toBe('No Quest user has that email.');
      expect(emailInput().value).toBe('nobody@x.test');
      expect(document.activeElement).toBe(emailInput());

      mocks.shareDoc.mockRejectedValueOnce(
        new ApiClientError('You own this doc; share it with someone else.', 400, 'cannot_share_with_owner'),
      );
      await click(button('Add'));
      expect(screen.getAllByRole('alert')).toHaveLength(1);
      expect(screen.getByRole('alert').textContent).toBe('You own this doc; share it with someone else.');

      mocks.shareDoc.mockRejectedValueOnce(
        new ApiClientError('Several Quest accounts match that email; type it exactly.', 409, 'ambiguous_user'),
      );
      await click(button('Add'));
      expect(screen.getAllByRole('alert')).toHaveLength(1);
      expect(screen.getByRole('alert').textContent).toBe(
        'Several Quest accounts match that email; type it exactly.',
      );
      expect(emailInput().value).toBe('nobody@x.test');

      // A non-API failure gets a generic line; a success clears it.
      mocks.shareDoc.mockRejectedValueOnce(new TypeError('network'));
      await click(button('Add'));
      expect(screen.getByRole('alert').textContent).toBe("Couldn't update sharing. Try again.");
      mocks.shareDoc.mockResolvedValueOnce(row([share(13, BO)]));
      fireEvent.change(emailInput(), { target: { value: 'bo@x.test' } });
      await click(button('Add'));
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('snaps a failed permission change back to the stored one', async () => {
      mocks.shareDoc.mockRejectedValue(new ApiClientError('Bad permission', 400, 'invalid_permission'));
      renderDialog(detail({ shares: [share(11, ANA)] }));
      await act(async () => {
        fireEvent.change(personSelect('ana@x.test'), { target: { value: 'write' } });
      });
      expect(screen.getByRole('alert').textContent).toBe('Bad permission');
      expect(personSelect('ana@x.test').value).toBe('read');
    });
  });

  describe('the roster', () => {
    it("changes a person's permission by upserting their email", async () => {
      const changed = row([share(11, ANA, 'write'), share(12, BO)]);
      mocks.shareDoc.mockResolvedValue(changed);
      const { onRowApplied } = renderDialog(detail({ shares: [share(11, ANA), share(12, BO)] }));
      expect(rosterNames()).toEqual(['Ana Lopez', 'Bo Chen']);

      await openFocusSettled();
      personSelect('ana@x.test').focus();
      await act(async () => {
        fireEvent.change(personSelect('ana@x.test'), { target: { value: 'write' } });
      });
      expect(mocks.shareDoc).toHaveBeenCalledWith('d1', { user_email: 'ana@x.test', permission: 'write' });
      expect(onRowApplied).toHaveBeenCalledWith(changed);
      expect(personSelect('ana@x.test').value).toBe('write');
      expect(document.activeElement).toBe(personSelect('ana@x.test'));
    });

    it("removes by share id and moves the focus to the next row's remove button, else the field", async () => {
      mocks.removeDocShare
        .mockResolvedValueOnce(row([share(12, BO), share(13, CY)]))
        .mockResolvedValueOnce(row([share(12, BO)]))
        .mockResolvedValueOnce(row([]));
      renderDialog(detail({ shares: [share(11, ANA), share(12, BO), share(13, CY)] }));
      await openFocusSettled();

      await click(button('Remove ana@x.test'));
      expect(mocks.removeDocShare).toHaveBeenLastCalledWith('d1', 11);
      expect(rosterNames()).toEqual(['Bo Chen', 'Cy Diaz']);
      expect(document.activeElement).toBe(button('Remove bo@x.test'));

      // The last row has no next one: back to the field.
      await click(button('Remove cy@x.test'));
      expect(mocks.removeDocShare).toHaveBeenLastCalledWith('d1', 13);
      expect(document.activeElement).toBe(emailInput());

      await click(button('Remove bo@x.test'));
      expect(roster()).toBeNull();
      expect(screen.getByText('Not shared with anyone yet.')).toBeTruthy();
      expect(document.activeElement).toBe(emailInput());
    });

    it('keeps a grant whose account is gone removable but not editable', async () => {
      const gone = share(15, { id: 9, name: null, email: null });
      mocks.removeDocShare.mockResolvedValue(row([]));
      renderDialog(detail({ shares: [gone] }));
      expect(rosterNames()).toEqual(['Deleted user']);
      expect(personSelect('Deleted user').disabled).toBe(true);
      expect(button('Remove Deleted user').disabled).toBe(false);
      await click(button('Remove Deleted user'));
      expect(mocks.removeDocShare).toHaveBeenCalledWith('d1', 15);
    });

    it('resyncs the roster when the doc prop brings new shares', async () => {
      mocks.shareDoc.mockResolvedValue(row([share(11, ANA)]));
      const onRowApplied = vi.fn();
      const { rerender } = render(
        <DocShareDialog doc={detail()} isOpen onClose={vi.fn()} onRowApplied={onRowApplied} />,
      );
      fireEvent.change(emailInput(), { target: { value: 'ana@x.test' } });
      await click(button('Add'));
      expect(rosterNames()).toEqual(['Ana Lopez']);

      // A later refresh (another tab removed Ana and added Bo) wins.
      rerender(
        <DocShareDialog
          doc={detail({ shares: [share(12, BO, 'write')] })}
          isOpen
          onClose={vi.fn()}
          onRowApplied={onRowApplied}
        />,
      );
      expect(rosterNames()).toEqual(['Bo Chen']);
    });

    it('shows the response roster when the shares prop changed while the request was in flight', async () => {
      const pending = deferred<Doc>();
      mocks.shareDoc.mockReturnValue(pending.promise);
      const props = { isOpen: true, onClose: vi.fn(), onRowApplied: vi.fn() };
      const { rerender } = render(<DocShareDialog doc={detail()} {...props} />);
      fireEvent.change(emailInput(), { target: { value: 'ana@x.test' } });
      fireEvent.click(button('Add'));

      // A refresh lands mid-request (Bo was added from another tab).
      const refreshed = detail({ shares: [share(12, BO)] });
      rerender(<DocShareDialog doc={refreshed} {...props} />);
      expect(rosterNames()).toEqual(['Bo Chen']);

      // The parent ignores the response row (it never reaches the prop):
      // the dialog still shows it, being the newest roster.
      await act(async () => {
        pending.resolve(row([share(12, BO), share(11, ANA)]));
        await pending.promise;
      });
      expect(rosterNames()).toEqual(['Bo Chen', 'Ana Lopez']);
      expect(props.onRowApplied).toHaveBeenCalledTimes(1);
    });
  });

  describe('everyone on this install', () => {
    it('asks before granting, and Cancel grants nothing', async () => {
      renderDialog(detail());
      await openFocusSettled();
      everyoneSelect().focus();
      // What arrowing through the native select does.
      fireEvent.change(everyoneSelect(), { target: { value: 'read' } });
      expect(confirmTitle()).toBeTruthy();
      expect(screen.getByText('Every Quest user will be able to open this doc.')).toBeTruthy();
      expect(mocks.shareDoc).not.toHaveBeenCalled();
      // The confirm opens on Cancel; Escape closes only the confirm.
      expect(document.activeElement).toBe(button('Cancel'));
      fireEvent.keyDown(document, { key: 'Escape' });
      expect(confirmTitle()).toBeNull();
      expect(shareDialog()).toBeTruthy();
      expect(everyoneSelect().value).toBe('none');
      expect(document.activeElement).toBe(everyoneSelect());

      fireEvent.change(everyoneSelect(), { target: { value: 'read' } });
      await click(button('Cancel'));
      expect(confirmTitle()).toBeNull();
      expect(mocks.shareDoc).not.toHaveBeenCalled();
    });

    it('grants on confirm, narrows and revokes without one, and confirms view -> edit', async () => {
      mocks.shareDoc
        .mockResolvedValueOnce(row([share(20, null, 'write')]))
        .mockResolvedValueOnce(row([share(20, null, 'read')]))
        .mockResolvedValueOnce(row([share(20, null, 'write')]));
      mocks.removeDocShare.mockResolvedValue(row([]));
      const { onRowApplied } = renderDialog(detail());
      await openFocusSettled();

      fireEvent.change(everyoneSelect(), { target: { value: 'write' } });
      expect(screen.getByText('Every Quest user will be able to open this doc and edit it.')).toBeTruthy();
      await click(button('Share with everyone'));
      expect(mocks.shareDoc).toHaveBeenLastCalledWith('d1', { everyone: true, permission: 'write' });
      expect(confirmTitle()).toBeNull();
      expect(everyoneSelect().value).toBe('write');
      expect(document.activeElement).toBe(everyoneSelect());
      // The everyone grant is not a roster entry.
      expect(roster()).toBeNull();
      expect(screen.getByText('No one added individually.')).toBeTruthy();

      // Edit -> view narrows access: no confirm.
      await act(async () => {
        fireEvent.change(everyoneSelect(), { target: { value: 'read' } });
      });
      expect(confirmTitle()).toBeNull();
      expect(mocks.shareDoc).toHaveBeenLastCalledWith('d1', { everyone: true, permission: 'read' });
      expect(everyoneSelect().value).toBe('read');

      // View -> edit widens it again: confirm.
      fireEvent.change(everyoneSelect(), { target: { value: 'write' } });
      expect(confirmTitle()).toBeTruthy();
      await click(button('Share with everyone'));
      expect(mocks.shareDoc).toHaveBeenLastCalledWith('d1', { everyone: true, permission: 'write' });

      // No access revokes at once.
      await act(async () => {
        fireEvent.change(everyoneSelect(), { target: { value: 'none' } });
      });
      expect(mocks.removeDocShare).toHaveBeenCalledWith('d1', 20);
      expect(everyoneSelect().value).toBe('none');
      expect(onRowApplied).toHaveBeenCalledTimes(4);
    });

    it('keeps the confirm open with the error when the grant fails', async () => {
      mocks.shareDoc.mockRejectedValueOnce(new ApiClientError('Docs are disabled.', 403, 'docs_disabled'));
      renderDialog(detail());
      fireEvent.change(everyoneSelect(), { target: { value: 'read' } });
      await click(button('Share with everyone'));
      expect(confirmTitle()).toBeTruthy();
      expect(screen.getAllByRole('alert').map((a) => a.textContent)).toEqual(['Docs are disabled.']);
      await click(button('Cancel'));
      expect(screen.queryByRole('alert')).toBeNull();
      expect(everyoneSelect().value).toBe('none');
    });
  });

  describe('while a request is in flight', () => {
    it('marks the controls aria-disabled, ignores them, keeps the focus, and cannot be closed', async () => {
      const pending = deferred<Doc>();
      mocks.shareDoc.mockReturnValue(pending.promise);
      const { onClose } = renderDialog(detail({ shares: [share(11, ANA), share(20, null, 'write')] }));

      await openFocusSettled();
      personSelect('ana@x.test').focus();
      fireEvent.change(personSelect('ana@x.test'), { target: { value: 'write' } });
      // The select shows the chosen value while it is being applied, and
      // keeps the focus (it is not `disabled`).
      expect(personSelect('ana@x.test').value).toBe('write');
      expect(document.activeElement).toBe(personSelect('ana@x.test'));
      for (const el of [
        personSelect('ana@x.test'),
        everyoneSelect(),
        addPermissionSelect(),
        emailInput(),
        button('Add'),
        button('Remove ana@x.test'),
        button('Close'),
        button('Done'),
      ]) {
        expect(el.getAttribute('aria-disabled')).toBe('true');
        expect((el as HTMLButtonElement).disabled ?? false).toBe(false);
      }
      expect(emailInput().readOnly).toBe(true);

      // Input is ignored...
      fireEvent.change(everyoneSelect(), { target: { value: 'none' } });
      fireEvent.click(button('Remove ana@x.test'));
      fireEvent.change(addPermissionSelect(), { target: { value: 'write' } });
      expect(mocks.removeDocShare).not.toHaveBeenCalled();
      expect(everyoneSelect().value).toBe('write');
      expect(addPermissionSelect().value).toBe('read');
      // ...and nothing closes the dialog.
      fireEvent.keyDown(document, { key: 'Escape' });
      fireEvent.click(shareDialog());
      fireEvent.click(button('Close'));
      fireEvent.click(button('Done'));
      expect(onClose).not.toHaveBeenCalled();

      await act(async () => {
        pending.resolve(row([share(11, ANA, 'write'), share(20, null, 'write')]));
        await pending.promise;
      });
      expect(personSelect('ana@x.test').getAttribute('aria-disabled')).toBeNull();
      expect(personSelect('ana@x.test').value).toBe('write');
      expect(emailInput().readOnly).toBe(false);
      expect(document.activeElement).toBe(personSelect('ana@x.test'));

      fireEvent.click(button('Done'));
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  it('explains sharing per doc kind', () => {
    renderDialog(detail({ project_id: 'p1' }));
    expect(notes()).toEqual([NOTE_APPROVAL, NOTE_PROJECT_DOC, NOTE_DIRECT_EDIT, NOTE_HISTORY]);
    cleanup();

    renderDialog(detail({ project_id: 'p1', mode: 'public' }));
    expect(notes()).toEqual([NOTE_PROJECT_DOC, NOTE_HISTORY]);
  });

  it('closes from the close button, Done, Escape and the backdrop', () => {
    const { onClose } = renderDialog(detail());
    fireEvent.click(button('Close'));
    fireEvent.click(button('Done'));
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(shareDialog());
    expect(onClose).toHaveBeenCalledTimes(4);
  });

  it('gives the focus back to the opener on close', async () => {
    const opener = document.createElement('button');
    opener.textContent = 'Share';
    document.body.appendChild(opener);
    try {
      opener.focus();
      const doc = detail();
      const { rerender } = render(
        <DocShareDialog doc={doc} isOpen onClose={vi.fn()} onRowApplied={vi.fn()} />,
      );
      await waitFor(() => expect(document.activeElement).toBe(emailInput()));
      rerender(<DocShareDialog doc={doc} isOpen={false} onClose={vi.fn()} onRowApplied={vi.fn()} />);
      expect(document.activeElement).toBe(opener);
    } finally {
      opener.remove();
    }
  });

  it('starts clean each time it opens', async () => {
    mocks.shareDoc.mockRejectedValue(new ApiClientError('No Quest user has that email.', 404, 'user_not_found'));
    const doc = detail();
    const { rerender } = render(
      <DocShareDialog doc={doc} isOpen onClose={vi.fn()} onRowApplied={vi.fn()} />,
    );
    fireEvent.change(emailInput(), { target: { value: 'nobody@x.test' } });
    await click(button('Add'));
    expect(screen.getByRole('alert')).toBeTruthy();

    rerender(<DocShareDialog doc={doc} isOpen={false} onClose={vi.fn()} onRowApplied={vi.fn()} />);
    rerender(<DocShareDialog doc={doc} isOpen onClose={vi.fn()} onRowApplied={vi.fn()} />);
    expect(emailInput().value).toBe('');
    expect(screen.queryByRole('alert')).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(emailInput()));
  });
});
