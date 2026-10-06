// DocHeader: menu items follow the viewer's access flags, rename sends the
// optimistic-concurrency token and recovers from a stale_update 409, and the
// mode-switch dialog shows the spec 8.4 text.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { Doc, DocDetail, DocMode } from '../../api/types';
import { DocHeader } from './DocHeader';

const mocks = vi.hoisted(() => ({
  updateDoc: vi.fn(),
  setDocMode: vi.fn(),
  deleteDoc: vi.fn(),
}));

vi.mock('../../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/docsApi')>()),
  updateDoc: mocks.updateDoc,
  setDocMode: mocks.setDocMode,
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
    access: { can_rename: true, can_switch_mode: true, can_delete: true, write: 'free' },
    shares: [],
    ...overrides,
  };
}

function detail(overrides: Partial<DocDetail> = {}): DocDetail {
  return { ...row(), content: '# Roadmap', ...overrides };
}

const OWNER_USER_DOC = detail();
const OWNER_PROJECT_DOC = detail({
  project_id: 'p1',
  scope: 'project',
  access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free' },
});
const NON_OWNER_DOC = detail({
  owner_id: 2,
  shared: true,
  last_write_source: null,
  shares: undefined,
  access: { can_rename: false, can_switch_mode: false, can_delete: false, write: 'approval' },
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
    mocks.setDocMode.mockReset();
    mocks.deleteDoc.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  describe('menu items by access flags', () => {
    it('owner of a user doc gets Rename, Switch, both downloads and Delete', () => {
      renderHeader(OWNER_USER_DOC);
      const menu = openMenu();
      expect(menuItemLabels(menu)).toEqual([
        'RenameR',
        'Switch to publicP',
        'Download Markdown',
        'Download with images (.zip)',
        'DeleteD',
      ]);
      expect(within(menu).queryByText(/share|history|edit/i)).toBeNull();
    });

    it('offers Switch to private on a public doc', () => {
      renderHeader(detail({ mode: 'public' }));
      expect(within(openMenu()).getByRole('menuitem', { name: /Switch to private/ })).toBeTruthy();
    });

    it('owner of a project doc gets no mode switch, and a folder chip to the project docs', () => {
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

  describe('mode switch', () => {
    it('private -> public shows the verbatim warning (+ write-share line) and applies the row', async () => {
      const switched = row({ mode: 'public' as DocMode });
      mocks.setDocMode.mockResolvedValue(switched);
      const { onRowApplied } = renderHeader(
        detail({
          shares: [{ id: 1, user_id: 7, permission: 'write', created_at: '2026-10-01T00:00:00' }],
        }),
      );

      openMenu();
      fireEvent.keyDown(document, { key: 'p' });
      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByRole('heading').textContent).toBe("Make 'Roadmap' public?");
      const paragraphs = Array.from(dialog.querySelectorAll('p')).map((p) => p.textContent);
      expect(paragraphs).toEqual([
        "Public conversations run in an internet-enabled sandbox and may send this doc's content to third-party sites. Public conversations will be able to read and change it, and private conversations will no longer be able to change it.",
        'Write access for people you shared it with will no longer require your approval.',
      ]);

      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to public' }));
      });
      expect(mocks.setDocMode).toHaveBeenCalledWith('d1', 'public');
      expect(onRowApplied).toHaveBeenCalledWith(switched);
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('public -> private is a plain confirm; a failure stays in the dialog', async () => {
      mocks.setDocMode.mockRejectedValue(
        new ApiClientError('You already have a private doc named "Roadmap".', 409, 'duplicate_title'),
      );
      renderHeader(detail({ mode: 'public' }));

      openMenu();
      fireEvent.click(screen.getByRole('menuitem', { name: /Switch to private/ }));
      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByRole('heading').textContent).toBe("Make 'Roadmap' private?");
      expect(Array.from(dialog.querySelectorAll('p')).map((p) => p.textContent)).toEqual([
        'Public conversations will no longer see this doc, and private conversations will be able to change it again.',
      ]);

      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to private' }));
      });
      expect(within(screen.getByRole('dialog')).getByRole('alert').textContent).toBe(
        'You already have a private doc named "Roadmap".',
      );
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

  it('the source toggle names the other view and calls onToggleSource', () => {
    const { onToggleSource } = renderHeader(OWNER_USER_DOC);
    fireEvent.click(screen.getByRole('button', { name: 'Show source' }));
    expect(onToggleSource).toHaveBeenCalledTimes(1);
    cleanup();
    renderHeader(OWNER_USER_DOC, { showSource: true });
    expect(screen.getByRole('button', { name: 'Show rendered' })).toBeTruthy();
  });
});
