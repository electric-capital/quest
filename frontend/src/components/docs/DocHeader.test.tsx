// DocHeader: menu items follow the viewer's access flags (no mode switch for
// any doc), rename sends the optimistic-concurrency token and recovers from a
// stale_update 409, and only a public doc shows a mode badge.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { Doc, DocDetail } from '../../api/types';
import { DocHeader } from './DocHeader';

const mocks = vi.hoisted(() => ({
  updateDoc: vi.fn(),
  deleteDoc: vi.fn(),
}));

vi.mock('../../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/docsApi')>()),
  updateDoc: mocks.updateDoc,
  deleteDoc: mocks.deleteDoc,
}));

vi.mock('../../contexts/ProjectsContext', () => ({
  useProjects: () => ({
    projects: [{ id: 'p1', name: 'Launch Plan' }],
  }),
}));

function row(overrides: Partial<Doc> = {}): Doc {
  return {
    id: 'd1',
    owner_id: 1,
    project_id: null,
    title: 'Roadmap',
    description: '',
    mode: 'private',
    content_size: 10,
    asset_count: 0,
    last_write_source: 'ui',
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-02T00:00:00',
    scope: 'user',
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    // can_switch_mode is always false from the server in v1.
    access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free', can_edit: true, can_share: true, can_delete_assets: true },
    shares: [],
    ...overrides,
  };
}

function detail(overrides: Partial<DocDetail> = {}): DocDetail {
  return { ...row(), content: '# Roadmap', last_write_conversation: null, assets: [], ...overrides };
}

const OWNER_USER_DOC = detail();
const OWNER_PROJECT_DOC = detail({
  project_id: 'p1',
  scope: 'project',
  access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free', can_edit: true, can_share: true, can_delete_assets: true },
});
const NON_OWNER_DOC = detail({
  owner_id: 2,
  shared: true,
  shared_with_me: true,
  permission: 'read',
  owner: { id: 2, name: 'Bea', email: 'bea@example.com' },
  last_write_source: null,
  shares: undefined,
  access: { can_rename: false, can_switch_mode: false, can_delete: false, write: 'approval', can_edit: false, can_share: false, can_delete_assets: false },
});

function renderHeader(doc: DocDetail, props: Partial<React.ComponentProps<typeof DocHeader>> = {}) {
  const handlers = {
    onToggleSource: vi.fn(),
    onRowApplied: vi.fn(),
    onDeleted: vi.fn(),
  };
  render(
    <MemoryRouter>
      <DocHeader doc={doc} showSource={false} {...handlers} {...props} />
    </MemoryRouter>,
  );
  return handlers;
}

function openMenu(title = 'Roadmap') {
  fireEvent.click(screen.getByRole('button', { name: title }));
  return screen.getByRole('menu');
}

function menuItemLabels(menu: HTMLElement): string[] {
  return within(menu)
    .getAllByRole('menuitem')
    .map((item) => item.textContent ?? '');
}

describe('DocHeader', () => {
  beforeEach(() => {
    mocks.updateDoc.mockReset();
    mocks.deleteDoc.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  describe('menu items by access flags', () => {
    it('owner of a user doc gets Rename, both downloads and Delete', () => {
      renderHeader(OWNER_USER_DOC);
      const menu = openMenu();
      expect(menuItemLabels(menu)).toEqual([
        'RenameR',
        'Download Markdown',
        'Download with images (.zip)',
        'DeleteD',
      ]);
      expect(within(menu).queryByText(/share|history|edit/i)).toBeNull();
    });

    it('owner of a project doc gets the same items, and a folder chip to the project docs', () => {
      renderHeader(OWNER_PROJECT_DOC);
      const chip = screen.getByRole('link', { name: 'Launch Plan' });
      expect(chip.getAttribute('href')).toBe('/docs?project=p1');
      expect(menuItemLabels(openMenu())).toEqual([
        'RenameR',
        'Download Markdown',
        'Download with images (.zip)',
        'DeleteD',
      ]);
    });

    it('offers no mode switch for any doc, even if a server flag says it may', () => {
      const docs = [
        OWNER_USER_DOC,
        OWNER_PROJECT_DOC,
        detail({ project_id: 'p1', scope: 'project', mode: 'public' }),
        // A stale can_switch_mode: true (the server always sends false in v1).
        detail({ access: { can_rename: true, can_switch_mode: true, can_delete: true, write: 'free', can_edit: true, can_share: true, can_delete_assets: true } }),
      ];
      for (const doc of docs) {
        renderHeader(doc);
        const menu = openMenu();
        expect(within(menu).queryByText(/Switch to/)).toBeNull();
        expect(within(menu).queryByText('P')).toBeNull();
        fireEvent.keyDown(document, { key: 'p' });
        expect(screen.queryByRole('dialog')).toBeNull();
        cleanup();
      }
    });

    it('non-owner gets only the two downloads, as cookie-authed links', () => {
      renderHeader(NON_OWNER_DOC);
      const menu = openMenu();
      expect(menuItemLabels(menu)).toEqual(['Download Markdown', 'Download with images (.zip)']);
      const [md, zip] = within(menu).getAllByRole('menuitem');
      expect(md.getAttribute('href')).toBe('/app/api/docs/d1/download?format=md');
      expect(md.hasAttribute('download')).toBe(true);
      expect(zip.getAttribute('href')).toBe('/app/api/docs/d1/download?format=zip');
    });

    it('ignores the hint keys for actions the viewer may not take', () => {
      renderHeader(NON_OWNER_DOC);
      openMenu();
      fireEvent.keyDown(document, { key: 'r' });
      fireEvent.keyDown(document, { key: 'd' });
      expect(screen.queryByRole('textbox')).toBeNull();
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  describe('rename', () => {
    function startRename() {
      openMenu();
      fireEvent.keyDown(document, { key: 'r' });
      return screen.getByRole('textbox', { name: 'Rename doc' }) as HTMLInputElement;
    }

    it('submits the trimmed title with expected_updated_at and applies the row', async () => {
      const renamed = row({ title: 'Roadmap 2027', updated_at: '2026-10-03T00:00:00' });
      mocks.updateDoc.mockResolvedValue(renamed);
      const { onRowApplied } = renderHeader(OWNER_USER_DOC);

      const input = startRename();
      expect(input.value).toBe('Roadmap');
      expect(input.maxLength).toBe(200);
      fireEvent.change(input, { target: { value: '  Roadmap 2027 ' } });
      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter' });
      });

      expect(mocks.updateDoc).toHaveBeenCalledTimes(1);
      expect(mocks.updateDoc).toHaveBeenCalledWith('d1', {
        title: 'Roadmap 2027',
        expected_updated_at: '2026-10-02T00:00:00',
      });
      expect(onRowApplied).toHaveBeenCalledWith(renamed);
      expect(screen.queryByRole('textbox')).toBeNull();
    });

    it('makes no call for an unchanged title, and Escape cancels', () => {
      renderHeader(OWNER_USER_DOC);
      let input = startRename();
      fireEvent.keyDown(input, { key: 'Enter' });
      expect(screen.queryByRole('textbox')).toBeNull();

      input = startRename();
      fireEvent.change(input, { target: { value: 'Something else' } });
      fireEvent.keyDown(input, { key: 'Escape' });
      fireEvent.blur(input);
      expect(screen.queryByRole('textbox')).toBeNull();
      expect(mocks.updateDoc).not.toHaveBeenCalled();
    });

    it('on a stale_update 409 applies the current row and shows a notice', async () => {
      const current = row({ title: 'Roadmap (edited elsewhere)', updated_at: '2026-10-04T00:00:00' });
      mocks.updateDoc.mockRejectedValue(
        new ApiClientError('Doc changed', 409, 'stale_update', undefined, {
          error: 'stale_update',
          message: 'Doc changed',
          current,
        }),
      );
      const { onRowApplied } = renderHeader(OWNER_USER_DOC);

      const input = startRename();
      fireEvent.change(input, { target: { value: 'Roadmap 2027' } });
      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter' });
      });

      expect(onRowApplied).toHaveBeenCalledWith(current);
      expect(screen.getByRole('status').textContent).toBe(
        'This doc changed elsewhere; the title was reloaded.',
      );
      expect(screen.queryByRole('textbox')).toBeNull();
    });

    it('on duplicate_title shows the message and keeps the typed title open', async () => {
      mocks.updateDoc.mockRejectedValue(
        new ApiClientError('You already have a doc named "Plan".', 409, 'duplicate_title'),
      );
      const { onRowApplied } = renderHeader(OWNER_USER_DOC);

      const input = startRename();
      fireEvent.change(input, { target: { value: 'Plan' } });
      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter' });
      });

      expect(onRowApplied).not.toHaveBeenCalled();
      expect(screen.getByRole('status').textContent).toBe('You already have a doc named "Plan".');
      const still = screen.getByRole('textbox', { name: 'Rename doc' }) as HTMLInputElement;
      expect(still.value).toBe('Plan');
    });
  });

  describe('delete', () => {
    it('confirms, deletes and reports onDeleted', async () => {
      mocks.deleteDoc.mockResolvedValue({ deleted: true });
      const { onDeleted } = renderHeader(OWNER_USER_DOC);

      openMenu();
      fireEvent.click(screen.getByRole('menuitem', { name: /Delete/ }));
      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByRole('heading').textContent).toBe("Delete 'Roadmap'?");
      expect(within(dialog).getByText('This cannot be undone.')).toBeTruthy();

      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
      });
      expect(mocks.deleteDoc).toHaveBeenCalledWith('d1');
      expect(onDeleted).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  describe('mode badge', () => {
    const badge = () => document.querySelector('.doc-mode-badge');

    it('shows no badge on a private user doc (and no meta row at all)', () => {
      renderHeader(OWNER_USER_DOC);
      expect(badge()).toBeNull();
      expect(document.querySelector('.doc-header-meta')).toBeNull();
    });

    it('shows no badge on a private project doc, only the folder chip', () => {
      renderHeader(OWNER_PROJECT_DOC);
      expect(badge()).toBeNull();
      expect(screen.getByRole('link', { name: 'Launch Plan' })).toBeTruthy();
    });

    it('shows the Public badge on a public doc', () => {
      renderHeader(detail({ project_id: 'p1', scope: 'project', mode: 'public' }));
      expect(badge()?.textContent).toBe('Public');
    });
  });

  it('the source toggle names the other view and calls onToggleSource', () => {
    const { onToggleSource } = renderHeader(OWNER_USER_DOC);
    fireEvent.click(screen.getByRole('button', { name: 'Show source' }));
    expect(onToggleSource).toHaveBeenCalledTimes(1);
    cleanup();
    renderHeader(OWNER_USER_DOC, { showSource: true });
    expect(screen.getByRole('button', { name: 'Show rendered' })).toBeTruthy();
  });

  describe('edit, history and share', () => {
    const viewerHandlers = () => ({
      onEdit: vi.fn(),
      onShowHistory: vi.fn(),
      onShare: vi.fn(),
    });

    it('owner gets Edit and Share beside the source toggle, History in the menu', () => {
      const extra = viewerHandlers();
      renderHeader(OWNER_USER_DOC, extra);
      fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
      expect(extra.onEdit).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole('button', { name: 'Share' }));
      expect(extra.onShare).toHaveBeenCalledTimes(1);
      const menu = openMenu();
      expect(menuItemLabels(menu)).toEqual([
        'RenameR',
        'HistoryH',
        'Download Markdown',
        'Download with images (.zip)',
        'DeleteD',
      ]);
      fireEvent.click(within(menu).getByText('History'));
      expect(extra.onShowHistory).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('menu')).toBeNull();
    });

    it('a read-share recipient gets the downloads only (no Edit, Share or History)', () => {
      renderHeader(NON_OWNER_DOC, viewerHandlers());
      expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Share' })).toBeNull();
      expect(menuItemLabels(openMenu())).toEqual([
        'Download Markdown',
        'Download with images (.zip)',
      ]);
    });

    it('a write-share recipient may edit but not share', () => {
      const writer = detail({
        ...NON_OWNER_DOC,
        permission: 'write',
        access: { ...NON_OWNER_DOC.access, write: 'free', can_edit: true },
      });
      renderHeader(writer, viewerHandlers());
      expect(screen.getByRole('button', { name: 'Edit' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Share' })).toBeNull();
      expect(menuItemLabels(openMenu())).toEqual([
        'HistoryH',
        'Download Markdown',
        'Download with images (.zip)',
      ]);
      expect(screen.getByText('Shared by Bea · Can edit')).toBeTruthy();
    });

    it('single-key hints run the menu actions while the menu is open', () => {
      const extra = viewerHandlers();
      renderHeader(OWNER_USER_DOC, extra);
      openMenu();
      fireEvent.keyDown(document, { key: 'h' });
      expect(extra.onShowHistory).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('menu')).toBeNull();
      // Edit and Share left the menu, so their old hint letters do nothing.
      openMenu();
      fireEvent.keyDown(document, { key: 'e' });
      fireEvent.keyDown(document, { key: 's' });
      expect(extra.onEdit).not.toHaveBeenCalled();
      expect(extra.onShare).not.toHaveBeenCalled();
      expect(screen.getByRole('menu')).toBeTruthy();
    });

    it('edit and history modes drop Edit, History and the source toggle but keep Share', () => {
      renderHeader(OWNER_USER_DOC, { ...viewerHandlers(), mode: 'edit' });
      expect(screen.queryByRole('button', { name: 'Show source' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
      expect(screen.getByRole('button', { name: 'Share' })).toBeTruthy();
      expect(menuItemLabels(openMenu())).toEqual([
        'RenameR',
        'Download Markdown',
        'Download with images (.zip)',
        'DeleteD',
      ]);
    });

    it('the owner of a shared doc gets a share chip that opens the dialog', () => {
      const extra = viewerHandlers();
      renderHeader(
        detail({
          shared: true,
          shares: [
            { id: 1, user_id: 5, permission: 'read', created_at: '', user: { id: 5, name: 'Cy', email: 'cy@x.io' } },
            { id: 2, user_id: 6, permission: 'write', created_at: '', user: { id: 6, name: 'Di', email: 'di@x.io' } },
          ],
        }),
        extra,
      );
      fireEvent.click(screen.getByRole('button', { name: 'Shared with 2 people' }));
      expect(extra.onShare).toHaveBeenCalledTimes(1);
    });

    it('an everyone grant reads "Shared with everyone"', () => {
      renderHeader(
        detail({
          shared: true,
          shares: [{ id: 1, user_id: null, permission: 'read', created_at: '', user: null }],
        }),
        viewerHandlers(),
      );
      expect(screen.getByRole('button', { name: 'Shared with everyone' })).toBeTruthy();
    });

    it("a doc from someone else's project gets a plain folder chip, not a link", () => {
      renderHeader(
        detail({ ...NON_OWNER_DOC, project_id: 'p-other', scope: 'project' }),
        viewerHandlers(),
      );
      expect(screen.getByText('Project doc')).toBeTruthy();
      expect(screen.queryByRole('link')).toBeNull();
      expect(screen.getByText('Shared by Bea · Can view')).toBeTruthy();
    });
  });
});
