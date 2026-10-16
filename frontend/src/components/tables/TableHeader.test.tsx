// TableHeader: menu items follow the row's access flags (owner-only items
// hidden for a reader; Columns and History shown once the viewer wires
// them, Share a disabled slot with its reason until phase C2), the
// single-key shortcuts (enabled items only, never from a field or under a
// dialog), rename and the approval switch send the concurrency token and
// recover from stale_update, delete confirms with the row count and
// navigates to the right list; `menuButtonRef` hands the host the title
// (menu) button.
import { createRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { TableAccess, TableDetail, TableListRow } from '../../api/types';
import { SHARE_UNAVAILABLE_NOTE, STALE_TABLE_NOTICE, TableHeader } from './TableHeader';

const mocks = vi.hoisted(() => ({
  updateTable: vi.fn(),
  deleteTable: vi.fn(),
}));

vi.mock('../../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/tablesApi')>()),
  updateTable: mocks.updateTable,
  deleteTable: mocks.deleteTable,
}));

vi.mock('../../contexts/ProjectsContext', () => ({
  useProjects: () => ({ projects: [{ id: 'p1', name: 'Launch Plan' }] }),
}));

const OWNER_ACCESS: TableAccess = {
  can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
  can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
  can_require_approval: true, can_download_history: true, deny_reason: null,
  schema_deny_reason: null,
};

const READER_ACCESS: TableAccess = {
  ...OWNER_ACCESS, rows: 'approval', can_edit: false, can_restore: false, can_rename: false,
  can_delete: false, can_share: false, can_edit_schema: false, can_require_approval: false,
  can_download_history: false,
};

function detail(overrides: Partial<TableDetail> = {}): TableDetail {
  return {
    id: 't1',
    owner_id: 1,
    project_id: null,
    title: 'Leads',
    description: '',
    mode: 'private',
    schema_version: 3,
    row_count: 350,
    last_write_source: 'ui:1',
    require_approval: false,
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-02T00:00:00',
    scope: 'user',
    column_count: 2,
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    shares: [],
    last_write_user: null,
    access: OWNER_ACCESS,
    schema: [],
    last_write_conversation: null,
    ...overrides,
  };
}

const READER = detail({
  owner_id: 2,
  shared: true,
  shared_with_me: true,
  permission: 'read',
  owner: { id: 2, name: 'Bea', email: 'bea@example.com' },
  shares: null,
  last_write_source: null,
  access: READER_ACCESS,
});

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function renderHeader(table: TableDetail, props: Partial<React.ComponentProps<typeof TableHeader>> = {}) {
  const handlers = { onDetailApplied: vi.fn(), onRowApplied: vi.fn(), onStale: vi.fn() };
  render(
    <MemoryRouter initialEntries={['/tables/t1']}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <TableHeader table={table} {...handlers} {...props} />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
  return handlers;
}

function openMenu(title = 'Leads') {
  fireEvent.click(screen.getByRole('button', { name: title }));
  return screen.getByRole('menu');
}

function itemLabels(menu: HTMLElement): string[] {
  return within(menu).getAllByRole('menuitem').map((item) => item.textContent ?? '');
}

function staleError(current: TableListRow | null) {
  return new ApiClientError('Changed elsewhere', 409, 'stale_update', undefined, {
    error: 'stale_update',
    message: 'Changed elsewhere',
    ...(current ? { current } : {}),
  });
}

describe('TableHeader', () => {
  beforeEach(() => {
    mocks.updateTable.mockReset();
    mocks.deleteTable.mockReset();
  });
  afterEach(() => cleanup());

  describe('menu items', () => {
    it('owner gets every item; with the viewer wiring, only Share is a disabled slot', () => {
      renderHeader(detail(), { onOpenColumns: vi.fn(), onOpenHistory: vi.fn() });
      const menu = openMenu();
      expect(itemLabels(menu)).toEqual([
        'RenameR',
        'ColumnsC',
        'HistoryH',
        // Disabled: no key hint, the reason as visible text.
        `Share${SHARE_UNAVAILABLE_NOTE}`,
        'Download CSV',
        'Download SQLite',
        'Download SQLite with history',
        'DeleteD',
      ]);
      for (const name of [/Columns/, /History/]) {
        expect((within(menu).getByRole('menuitem', { name }) as HTMLButtonElement).disabled).toBe(false);
      }
      const share = within(menu).getByRole('menuitem', { name: 'Share' }) as HTMLButtonElement;
      expect(share.disabled).toBe(true);
      expect(share.title).toBe(SHARE_UNAVAILABLE_NOTE);
      // The reason is the item's description (a disabled button's title is not read).
      const noteId = share.getAttribute('aria-describedby');
      expect(noteId).toBeTruthy();
      expect(document.getElementById(noteId as string)?.textContent).toBe(SHARE_UNAVAILABLE_NOTE);
      expect(document.body.textContent).not.toContain('Coming soon');
      const checkbox = within(menu).getByRole('menuitemcheckbox', { name: /Require approval for agent writes/ });
      expect(checkbox.getAttribute('aria-checked')).toBe('false');
    });

    it('hands the host the title (menu) button through menuButtonRef', () => {
      const menuButtonRef = createRef<HTMLButtonElement>();
      renderHeader(detail(), { menuButtonRef });
      expect(menuButtonRef.current).toBe(screen.getByRole('button', { name: 'Leads' }));
    });

    it('downloads are cookie-authed links with download', () => {
      renderHeader(detail());
      const menu = openMenu();
      const csv = within(menu).getByRole('menuitem', { name: 'Download CSV' });
      expect(csv.getAttribute('href')).toBe('/app/api/tables/t1/download?format=csv');
      expect(csv.hasAttribute('download')).toBe(true);
      expect(within(menu).getByRole('menuitem', { name: 'Download SQLite' }).getAttribute('href')).toBe(
        '/app/api/tables/t1/download?format=sqlite',
      );
      expect(
        within(menu).getByRole('menuitem', { name: 'Download SQLite with history' }).getAttribute('href'),
      ).toBe('/app/api/tables/t1/download?format=sqlite&include_history=true');
    });

    it('a host that wires nothing gets no Columns or History item, and their keys do nothing', () => {
      renderHeader(detail());
      const menu = openMenu();
      expect(within(menu).queryByRole('menuitem', { name: /Columns/ })).toBeNull();
      expect(within(menu).queryByRole('menuitem', { name: /History/ })).toBeNull();
      expect((within(menu).getByRole('menuitem', { name: 'Share' }) as HTMLButtonElement).disabled).toBe(true);
      fireEvent.keyDown(document, { key: 'c' });
      fireEvent.keyDown(document, { key: 'h' });
      fireEvent.keyDown(document, { key: 's' });
      // Nothing acted: the menu is still open.
      expect(screen.getByRole('menu')).toBeTruthy();
    });

    it('a reader gets Columns (the read-only panel) and the two plain downloads only', () => {
      const onOpenColumns = vi.fn();
      const onOpenHistory = vi.fn();
      renderHeader(READER, { onOpenColumns, onOpenHistory });
      const menu = openMenu();
      expect(itemLabels(menu)).toEqual(['ColumnsC', 'Download CSV', 'Download SQLite']);
      expect((within(menu).getByRole('menuitem', { name: /Columns/ }) as HTMLButtonElement).disabled).toBe(false);
      fireEvent.keyDown(document, { key: 'h' });
      expect(onOpenHistory).not.toHaveBeenCalled();
      expect(within(menu).queryByRole('menuitemcheckbox')).toBeNull();
      fireEvent.keyDown(document, { key: 'r' });
      fireEvent.keyDown(document, { key: 'd' });
      expect(screen.queryByRole('textbox')).toBeNull();
      expect(screen.queryByRole('dialog')).toBeNull();
      // Who shared it, and the view-only permission.
      expect(screen.getByText('Shared by Bea · Can view')).toBeTruthy();
      // The menu is still open (those keys did nothing): C opens Columns.
      fireEvent.keyDown(document, { key: 'c' });
      expect(onOpenColumns).toHaveBeenCalledTimes(1);
    });

    it('a write-share recipient also gets History and the history download', () => {
      renderHeader(
        detail({ ...READER, permission: 'write', access: { ...READER_ACCESS, rows: 'free', can_edit: true, can_restore: true, can_download_history: true } }),
        { onOpenColumns: vi.fn(), onOpenHistory: vi.fn() },
      );
      expect(itemLabels(openMenu())).toEqual([
        'ColumnsC', 'HistoryH', 'Download CSV', 'Download SQLite', 'Download SQLite with history',
      ]);
    });

    it('slots are enabled and call back once wired', () => {
      const onOpenColumns = vi.fn();
      const onOpenHistory = vi.fn();
      const onOpenShare = vi.fn();
      renderHeader(detail(), { onOpenColumns, onOpenHistory, onOpenShare });
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Columns/ }));
      expect(onOpenColumns).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('menu')).toBeNull();
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /History/ }));
      expect(onOpenHistory).toHaveBeenCalledTimes(1);
      openMenu();
      fireEvent.keyDown(document, { key: 's' });
      expect(onOpenShare).toHaveBeenCalledTimes(1);
    });
  });

  describe('shortcuts', () => {
    it('each key runs its item and closes the menu (the approval switch keeps it open)', async () => {
      const onOpenColumns = vi.fn();
      const onOpenHistory = vi.fn();
      const onOpenShare = vi.fn();
      mocks.updateTable.mockResolvedValue(detail({ require_approval: true }));
      renderHeader(detail(), { onOpenColumns, onOpenHistory, onOpenShare });

      openMenu();
      fireEvent.keyDown(document, { key: 'C' });
      expect(onOpenColumns).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('menu')).toBeNull();

      openMenu();
      fireEvent.keyDown(document, { key: 'h' });
      expect(onOpenHistory).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('menu')).toBeNull();

      openMenu();
      fireEvent.keyDown(document, { key: 's' });
      expect(onOpenShare).toHaveBeenCalledTimes(1);

      openMenu();
      await act(async () => {
        fireEvent.keyDown(document, { key: 'a' });
      });
      expect(mocks.updateTable).toHaveBeenCalledWith('t1', { require_approval: true });
      expect(screen.getByRole('menu')).toBeTruthy();

      fireEvent.keyDown(document, { key: 'Escape' });
      expect(screen.queryByRole('menu')).toBeNull();

      openMenu();
      fireEvent.keyDown(document, { key: 'r' });
      expect(screen.getByRole('textbox', { name: 'Rename table' })).toBeTruthy();
      fireEvent.keyDown(screen.getByRole('textbox', { name: 'Rename table' }), { key: 'Escape' });

      openMenu();
      fireEvent.keyDown(document, { key: 'd' });
      expect(screen.getByRole('dialog').textContent).toContain('Delete "Leads"');
    });

    it('ignores modified keys', () => {
      const onOpenColumns = vi.fn();
      renderHeader(detail(), { onOpenColumns });
      openMenu();
      fireEvent.keyDown(document, { key: 'c', ctrlKey: true });
      fireEvent.keyDown(document, { key: 'c', metaKey: true });
      expect(onOpenColumns).not.toHaveBeenCalled();
    });

    it('does not act while focus is in a field or contenteditable, but Escape still closes', () => {
      const onOpenColumns = vi.fn();
      const onOpenHistory = vi.fn();
      renderHeader(detail(), { onOpenColumns, onOpenHistory });
      const input = document.createElement('input');
      const textarea = document.createElement('textarea');
      const select = document.createElement('select');
      const editable = document.createElement('div');
      editable.setAttribute('contenteditable', 'true');
      document.body.append(input, textarea, select, editable);
      try {
        openMenu();
        for (const el of [input, textarea, select, editable]) {
          fireEvent.keyDown(el, { key: 'c' });
          fireEvent.keyDown(el, { key: 'h' });
          fireEvent.keyDown(el, { key: 'd' });
        }
        expect(onOpenColumns).not.toHaveBeenCalled();
        expect(onOpenHistory).not.toHaveBeenCalled();
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(screen.getByRole('menu')).toBeTruthy();
        fireEvent.keyDown(input, { key: 'Escape' });
        expect(screen.queryByRole('menu')).toBeNull();
      } finally {
        input.remove();
        textarea.remove();
        select.remove();
        editable.remove();
      }
    });

    it('does not act while a dialog is open', () => {
      const onOpenColumns = vi.fn();
      renderHeader(detail(), { onOpenColumns });
      const dialog = document.createElement('div');
      dialog.setAttribute('role', 'dialog');
      document.body.append(dialog);
      try {
        openMenu();
        fireEvent.keyDown(document, { key: 'c' });
        expect(onOpenColumns).not.toHaveBeenCalled();
      } finally {
        dialog.remove();
      }
      fireEvent.keyDown(document, { key: 'c' });
      expect(onOpenColumns).toHaveBeenCalledTimes(1);
    });
  });

  describe('chips', () => {
    it('shows the project chip as a link to that project tables, and share chips', () => {
      renderHeader(
        detail({
          project_id: 'p1',
          scope: 'project',
          mode: 'public',
          shared: true,
          shares: [
            { id: 1, user_id: null, permission: 'read', created_at: '2026-10-01T00:00:00', user: null },
            { id: 2, user_id: 5, permission: 'write', created_at: '2026-10-01T00:00:00', user: { id: 5, name: 'Ana', email: 'ana@example.com' } },
          ],
        }),
      );
      expect(screen.getByRole('link', { name: 'Launch Plan' }).getAttribute('href')).toBe('/tables?project=p1');
      expect(screen.getByText('Everyone')).toBeTruthy();
      expect(screen.getByText('Ana · can edit')).toBeTruthy();
      expect(screen.getByText('Public')).toBeTruthy();
    });
  });

  describe('rename', () => {
    it('sends the title with expected_updated_at and applies the returned detail', async () => {
      const renamed = detail({ title: 'Prospects', updated_at: '2026-10-03T00:00:00' });
      mocks.updateTable.mockResolvedValueOnce(renamed);
      const { onDetailApplied } = renderHeader(detail());
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Rename/ }));
      const input = screen.getByRole('textbox', { name: 'Rename table' });
      fireEvent.change(input, { target: { value: 'Prospects' } });
      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter' });
      });
      expect(mocks.updateTable).toHaveBeenCalledWith('t1', {
        title: 'Prospects',
        expected_updated_at: '2026-10-02T00:00:00',
      });
      expect(onDetailApplied).toHaveBeenCalledWith(renamed);
      expect(screen.queryByRole('textbox')).toBeNull();
    });

    it('on stale_update applies the current row and says so', async () => {
      const current = detail({ title: 'Renamed elsewhere', updated_at: '2026-10-04T00:00:00' });
      mocks.updateTable.mockRejectedValueOnce(staleError(current));
      const { onRowApplied, onDetailApplied } = renderHeader(detail());
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Rename/ }));
      const input = screen.getByRole('textbox', { name: 'Rename table' });
      fireEvent.change(input, { target: { value: 'Prospects' } });
      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter' });
      });
      expect(onRowApplied).toHaveBeenCalledWith(current);
      expect(onDetailApplied).not.toHaveBeenCalled();
      expect(screen.getByRole('status').textContent).toBe(STALE_TABLE_NOTICE);
      // The typed title survives so the user can decide again.
      expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('Prospects');
      expect(mocks.updateTable).toHaveBeenCalledTimes(1);
    });

    it('retries once when only rows moved (title unchanged on the server)', async () => {
      const current = detail({ updated_at: '2026-10-04T00:00:00', row_count: 999 });
      const renamed = detail({ title: 'Prospects', updated_at: '2026-10-04T00:00:01' });
      mocks.updateTable.mockRejectedValueOnce(staleError(current)).mockResolvedValueOnce(renamed);
      const { onRowApplied, onDetailApplied } = renderHeader(detail());
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Rename/ }));
      const input = screen.getByRole('textbox', { name: 'Rename table' });
      fireEvent.change(input, { target: { value: 'Prospects' } });
      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter' });
      });
      expect(mocks.updateTable).toHaveBeenLastCalledWith('t1', {
        title: 'Prospects',
        expected_updated_at: '2026-10-04T00:00:00',
      });
      expect(onRowApplied).toHaveBeenCalledWith(current);
      expect(onDetailApplied).toHaveBeenCalledWith(renamed);
      expect(screen.queryByRole('textbox')).toBeNull();
    });

    it('on stale_update without a current row asks for a re-fetch', async () => {
      mocks.updateTable.mockRejectedValueOnce(staleError(null));
      const { onStale, onRowApplied } = renderHeader(detail());
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Rename/ }));
      const input = screen.getByRole('textbox', { name: 'Rename table' });
      fireEvent.change(input, { target: { value: 'Prospects' } });
      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter' });
      });
      expect(onStale).toHaveBeenCalledTimes(1);
      expect(onRowApplied).not.toHaveBeenCalled();
    });

    it('keeps the input open on a duplicate title and shows the error', async () => {
      mocks.updateTable.mockRejectedValueOnce(
        new ApiClientError('A table with that title exists.', 409, 'duplicate_title'),
      );
      renderHeader(detail());
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Rename/ }));
      const input = screen.getByRole('textbox', { name: 'Rename table' });
      fireEvent.change(input, { target: { value: 'Other' } });
      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter' });
      });
      expect(screen.getByRole('textbox')).toBeTruthy();
      expect(screen.getByRole('status').textContent).toBe('A table with that title exists.');
    });
  });

  describe('require approval', () => {
    it('flips the switch without a concurrency token and keeps the menu open', async () => {
      const on = detail({ require_approval: true, updated_at: '2026-10-03T00:00:00' });
      mocks.updateTable.mockResolvedValueOnce(on);
      const { onDetailApplied } = renderHeader(detail());
      const menu = openMenu();
      await act(async () => {
        fireEvent.click(within(menu).getByRole('menuitemcheckbox', { name: /Require approval/ }));
      });
      // Row writes move updated_at; the switch must not fail as stale.
      expect(mocks.updateTable).toHaveBeenCalledWith('t1', { require_approval: true });
      expect(onDetailApplied).toHaveBeenCalledWith(on);
      expect(screen.getByRole('menu')).toBeTruthy();
    });

    it('shows a failure notice', async () => {
      mocks.updateTable.mockRejectedValueOnce(new ApiClientError('Nope.', 500, 'internal'));
      renderHeader(detail());
      const menu = openMenu();
      await act(async () => {
        fireEvent.click(within(menu).getByRole('menuitemcheckbox', { name: /Require approval/ }));
      });
      expect(screen.getByRole('status').textContent).toBe('Nope.');
    });
  });

  describe('delete', () => {
    it('confirms with the row count and navigates to All Tables', async () => {
      mocks.deleteTable.mockResolvedValueOnce(undefined);
      renderHeader(detail());
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Delete/ }));
      const dialog = screen.getByRole('dialog');
      expect(dialog.textContent).toContain('Delete "Leads" and its 350 rows? This cannot be undone.');
      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
      });
      expect(mocks.deleteTable).toHaveBeenCalledWith('t1');
      expect(screen.getByTestId('location').textContent).toBe('/tables');
    });

    it('a project table goes back to its project list; one row reads singular', async () => {
      mocks.deleteTable.mockResolvedValueOnce(undefined);
      renderHeader(detail({ project_id: 'p1', scope: 'project', row_count: 1 }));
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Delete/ }));
      const dialog = screen.getByRole('dialog');
      expect(dialog.textContent).toContain('and its 1 row?');
      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
      });
      expect(screen.getByTestId('location').textContent).toBe('/tables?project=p1');
    });

    it('shows a failed delete in the dialog and stays put', async () => {
      mocks.deleteTable.mockRejectedValueOnce(new ApiClientError('Nope', 500));
      renderHeader(detail());
      fireEvent.click(within(openMenu()).getByRole('menuitem', { name: /Delete/ }));
      const dialog = screen.getByRole('dialog');
      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
      });
      expect(within(dialog).getByRole('alert').textContent).toBe('Nope');
      expect(screen.getByTestId('location').textContent).toBe('/tables/t1');
    });
  });
});
