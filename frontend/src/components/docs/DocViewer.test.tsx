// DocViewer: the loading / not-found / error states, the rendered body with
// doc asset resolution and the Show source toggle, the Assets panel (card on
// desktop, collapsed section on phones; Delete for the owner in view mode
// only, refreshing the doc), the empty-doc line per reach / edit access, the
// footer's "Last written by" subject per `last_write_source`, the editor
// wiring (save -> applyContent + view, cancel -> view, 409 -> refresh, focus
// back on the title), and unsaved drafts (the view-mode notice, the
// read-only recovery panel for a vanished doc or lost edit access, the
// owner's delete dropping the draft).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { DocDetail } from '../../api/types';
import type { DocView } from '../../hooks/useDoc';
import { DocViewer } from './DocViewer';

const mocks = vi.hoisted(() => ({
  view: null as unknown as DocView,
  isMobile: false,
  deleteDocAsset: vi.fn(),
  deleteDoc: vi.fn(),
  updateDocContent: vi.fn(),
  fetchDoc: vi.fn(),
}));

vi.mock('../../api/docsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/docsApi')>();
  return {
    ...actual,
    deleteDocAsset: mocks.deleteDocAsset,
    deleteDoc: mocks.deleteDoc,
    updateDocContent: mocks.updateDocContent,
    fetchDoc: mocks.fetchDoc,
    // History loads its versions on open; never answering keeps it loading.
    fetchDocRevisions: () => new Promise(() => {}),
  };
});

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ userEmail: 'me@example.com' }),
}));

vi.mock('../../hooks/useDoc', () => ({
  useDoc: () => mocks.view,
}));

vi.mock('../../hooks/useIsMobile', () => ({
  useIsMobile: () => mocks.isMobile,
}));

vi.mock('../../contexts/ProjectsContext', () => ({
  useProjects: () => ({ projects: [] }),
}));

// Message.tsx (markdownComponents) reaches pdfjs-dist through its card
// imports; pdf.js needs browser canvas APIs jsdom lacks.
vi.mock('../PdfViewer', () => ({ PdfViewer: () => null }));

function doc(overrides: Partial<DocDetail> = {}): DocDetail {
  return {
    id: 'd1',
    owner_id: 1,
    project_id: null,
    title: 'Roadmap',
    description: '',
    mode: 'private',
    content_size: 30,
    asset_count: 1,
    require_approval: false,
    last_write_source: 'ui',
    created_at: '2026-10-01T00:00:00',
    updated_at: new Date(Date.now() - 5 * 60 * 1000).toISOString().replace('Z', ''),
    scope: 'user',
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free', can_edit: true, can_share: true, can_delete_assets: true, can_require_approval: true },
    shares: [],
    content: '# Plan\n\n![chart](assets/chart.png)',
    last_write_conversation: null,
    assets: [{ name: 'chart.png', size: 2048, mime: 'image/png' }],
    ...overrides,
  };
}

function setView(view: Partial<DocView>) {
  mocks.view = {
    doc: null,
    loading: false,
    error: null,
    notFound: false,
    refresh: vi.fn().mockResolvedValue(undefined),
    applyRow: vi.fn(),
    applyContent: vi.fn(),
    ...view,
  };
}

const DRAFT_KEY = 'quest_doc_draft:me%40example.com:d1';

const viewerUi = () => (
  <MemoryRouter initialEntries={['/docs/d1']}>
    <DocViewer docId="d1" />
  </MemoryRouter>
);

function renderViewer() {
  return render(viewerUi());
}

function storeDraft(content: string) {
  localStorage.setItem(
    DRAFT_KEY,
    JSON.stringify({ content, base: '2026-10-06T09:00:00', saved_at: new Date().toISOString(), title: 'Roadmap' }),
  );
}

function openMenuItem(name: RegExp) {
  fireEvent.click(screen.getByRole('button', { name: /Roadmap/ }));
  fireEvent.click(screen.getByRole('menuitem', { name }));
}

/** The header's Edit button (beside the Show source toggle). */
function clickEdit() {
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
}

function titleButton(): HTMLElement {
  return screen.getByRole('button', { name: /Roadmap/ });
}

function editorTextarea(): HTMLTextAreaElement | null {
  return screen.queryByRole('textbox', { name: 'Doc markdown' }) as HTMLTextAreaElement | null;
}

function footerText(container: HTMLElement): string {
  return container.querySelector('.doc-viewer-footer')?.textContent ?? '';
}

describe('DocViewer', () => {
  beforeEach(() => {
    mocks.isMobile = false;
    mocks.updateDocContent.mockReset();
    mocks.fetchDoc.mockReset();
    mocks.deleteDoc.mockReset();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
  });

  it('shows Loading... until the first response', () => {
    setView({ loading: true });
    renderViewer();
    expect(screen.getByText('Loading...')).toBeTruthy();
  });

  it('shows the not-found state with a link back to All docs', () => {
    setView({ notFound: true });
    renderViewer();
    expect(screen.getByText('This doc no longer exists.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'All docs' }).getAttribute('href')).toBe('/docs');
  });

  it('shows a load error with a Retry that refreshes', () => {
    setView({ error: new ApiClientError('Server exploded', 500) });
    renderViewer();
    expect(screen.getByText('Server exploded')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(mocks.view.refresh).toHaveBeenCalledTimes(1);
  });

  it('renders the markdown with doc assets and toggles to the raw source', () => {
    setView({ doc: doc() });
    const { container } = renderViewer();

    expect(screen.getByRole('heading', { name: 'Plan' })).toBeTruthy();
    expect(screen.getByRole('img', { name: 'chart' }).getAttribute('src')).toBe(
      '/app/api/docs/d1/assets/chart.png',
    );
    expect(container.querySelector('.doc-viewer-markdown.message-content')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Show source' }));
    expect(container.querySelector('pre.doc-viewer-source')?.textContent).toBe(
      '# Plan\n\n![chart](assets/chart.png)',
    );
    expect(screen.queryByRole('img')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show rendered' }));
    expect(container.querySelector('pre.doc-viewer-source')).toBeNull();
  });

  it("lists the doc's assets in the aside card", () => {
    setView({
      doc: doc({
        asset_count: 2,
        require_approval: false,
        assets: [
          { name: 'chart.png', size: 2048, mime: 'image/png' },
          { name: 'photo.jpg', size: 300, mime: 'image/jpeg' },
        ],
      }),
    });
    const { container } = renderViewer();

    const aside = container.querySelector('aside.doc-viewer-aside') as HTMLElement;
    const card = within(aside).getByRole('region', { name: 'Assets' });
    expect(card.classList.contains('right-panel-card')).toBe(true);
    expect(card.querySelector('.doc-assets-count')?.textContent).toBe('2');
    expect(
      [...card.querySelectorAll('.doc-asset-name')].map((el) => el.textContent),
    ).toEqual(['chart.png', 'photo.jpg']);
    expect(card.querySelector('img')?.getAttribute('src')).toBe('/app/api/docs/d1/assets/chart.png');
    expect(aside.querySelector('details')).toBeNull();
  });

  it('collapses the assets into an "Assets (N)" section on phones', () => {
    mocks.isMobile = true;
    setView({ doc: doc() });
    const { container } = renderViewer();

    const details = container.querySelector('aside.doc-viewer-aside details') as HTMLDetailsElement;
    expect(details).not.toBeNull();
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toBe('Assets (1)');
    expect(container.querySelector('.right-panel-card')).toBeNull();
  });

  describe('empty doc', () => {
    const base = doc();
    const readOnly = { ...base.access, can_edit: false, can_rename: false, can_delete: false };

    function emptyText(overrides: Partial<DocDetail>): string {
      setView({ doc: doc({ content: '  \n', ...overrides }) });
      const { container } = renderViewer();
      const text = container.querySelector('.doc-viewer-empty')?.textContent ?? '';
      cleanup();
      return text;
    }

    it("offers Quest and Edit to the owner", () => {
      expect(emptyText({})).toBe(
        'This doc is empty. Ask Quest to add to it, or choose Edit from the title menu.',
      );
    });

    it('offers Quest for a USER doc shared with write permission (the recipient\'s conversations can write it)', () => {
      const writeShare = { ...base.access, can_rename: false, can_delete: false, can_share: false };
      expect(emptyText({ shared_with_me: true, permission: 'write', access: writeShare })).toBe(
        'This doc is empty. Ask Quest to add to it, or choose Edit from the title menu.',
      );
    });

    it('never offers Quest on a read share (its conversations can only read the doc)', () => {
      expect(emptyText({ shared_with_me: true, permission: 'read', access: readOnly })).toBe(
        'This doc is empty.',
      );
    });

    it('never offers Quest for a shared project doc (hidden from the recipient\'s conversations)', () => {
      const project = { shared_with_me: true, scope: 'project' as const, project_id: 'p9' };
      expect(emptyText({ ...project, permission: 'write', access: { ...base.access, can_rename: false } })).toBe(
        'This doc is empty. Choose Edit from the title menu to write it.',
      );
      expect(emptyText({ ...project, permission: 'read', access: readOnly })).toBe('This doc is empty.');
    });
  });

  describe('asset delete', () => {
    beforeEach(() => {
      mocks.deleteDocAsset.mockReset();
    });

    it('is offered to the owner in view mode and refreshes the doc after a delete', async () => {
      mocks.deleteDocAsset.mockResolvedValue({
        deleted: true, asset_count: 0, updated_at: '2026-10-06T10:00:30', previous_updated_at: '2026-10-06T10:00:00',
      });
      setView({ doc: doc() });
      renderViewer();

      fireEvent.click(screen.getByRole('button', { name: 'Delete chart.png' }));
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
      expect(mocks.deleteDocAsset).toHaveBeenCalledWith('d1', 'chart.png');
      await waitFor(() => expect(mocks.view.refresh).toHaveBeenCalledTimes(1));
    });

    it('is not offered to someone who cannot delete assets (a write share)', () => {
      const base = doc();
      setView({
        doc: doc({
          access: { ...base.access, can_rename: false, can_delete: false, can_share: false, can_delete_assets: false, can_require_approval: false },
          shared_with_me: true,
          permission: 'write',
        }),
      });
      renderViewer();
      expect(screen.getByRole('region', { name: 'Assets' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Delete chart.png' })).toBeNull();
    });

    it('is not offered in edit mode', () => {
      setView({ doc: doc() });
      const { container } = renderViewer();
      expect(screen.getByRole('button', { name: 'Delete chart.png' })).toBeTruthy();

      clickEdit();
      expect(container.querySelector('.doc-viewer--edit')).not.toBeNull();
      expect(screen.getByRole('textbox', { name: 'Doc markdown' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Delete chart.png' })).toBeNull();
    });
  });

  describe('footer', () => {
    it('names "you" for a UI write, then the relative update time', () => {
      setView({ doc: doc({ last_write_source: 'ui' }) });
      const { container } = renderViewer();
      expect(footerText(container)).toBe('Last written by you · Updated 5m ago');
    });

    it('names the viewer "you" for a ui:<id> write of their own, else the person', () => {
      setView({ doc: doc({
        last_write_source: 'ui:1',
        last_write_user: { id: 1, name: 'Me', email: 'ME@example.com' },
      }) });
      const { container } = renderViewer();
      expect(footerText(container)).toBe('Last written by you · Updated 5m ago');
      cleanup();

      setView({ doc: doc({
        last_write_source: 'ui:2',
        last_write_user: { id: 2, name: 'Bob Builder', email: 'bob@example.com' },
      }) });
      const second = renderViewer();
      expect(footerText(second.container)).toBe('Last written by Bob Builder · Updated 5m ago');
      cleanup();

      setView({ doc: doc({ last_write_source: 'ui:3', last_write_user: null }) });
      const third = renderViewer();
      expect(footerText(third.container)).toBe('Last written by a deleted user · Updated 5m ago');
    });

    it("names a share recipient's approved change", () => {
      setView({ doc: doc({
        last_write_source: 'action_request:42',
        last_write_user: { id: 2, name: '', email: 'bob@example.com' },
      }) });
      const { container } = renderViewer();
      expect(footerText(container)).toBe(
        'Last written by bob@example.com (approved change) · Updated 5m ago',
      );
    });

    it('names a deleted proposer of an approved change', () => {
      setView({ doc: doc({
        last_write_source: 'action_request:42',
        last_write_user: { id: 7, name: null, email: null },
      }) });
      const { container } = renderViewer();
      expect(footerText(container)).toBe(
        'Last written by a deleted user (approved change) · Updated 5m ago',
      );
    });

    it('names the action request', () => {
      setView({ doc: doc({ last_write_source: 'action_request:42' }) });
      const { container } = renderViewer();
      expect(footerText(container)).toBe('Last written by action request #42 · Updated 5m ago');
    });

    it('omits the writer when the source is null', () => {
      setView({ doc: doc({ last_write_source: null }) });
      const { container } = renderViewer();
      expect(footerText(container)).toBe('Updated 5m ago');
    });

    it('reads "a deleted conversation" when the server could not resolve the writer', () => {
      setView({ doc: doc({ last_write_source: 'conversation:c9', last_write_conversation: null }) });
      const { container } = renderViewer();
      expect(footerText(container)).toBe('Last written by a deleted conversation · Updated 5m ago');
      expect(screen.queryByRole('link', { name: /conversation/ })).toBeNull();
    });

    it('links the writing conversation by its title, into its project when it has one', () => {
      setView({ doc: doc({
        last_write_source: 'conversation:c9',
        last_write_conversation: { id: 'c9', title: 'Quarterly planning', project_id: 'p1' },
      }) });
      renderViewer();
      const link = screen.getByRole('link', { name: 'Quarterly planning' });
      expect(link.getAttribute('href')).toBe('/projects/p1/c9');
    });

    it('links a standalone writing conversation under /chats', () => {
      setView({ doc: doc({
        last_write_source: 'conversation:c9',
        last_write_conversation: { id: 'c9', title: 'Notes chat', project_id: null },
      }) });
      renderViewer();
      expect(screen.getByRole('link', { name: 'Notes chat' }).getAttribute('href')).toBe('/chats/c9');
    });
  });
  describe('editor wiring', () => {
    it('Save applies the returned content, returns to view and focuses the title', async () => {
      const current = doc();
      const row = { ...current, content: 'New body', changed: true };
      mocks.updateDocContent.mockResolvedValue(row);
      setView({ doc: current });
      const { container } = renderViewer();

      clickEdit();
      expect(container.querySelector('.doc-viewer--edit')).not.toBeNull();
      fireEvent.change(editorTextarea()!, { target: { value: 'New body' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));

      await waitFor(() => expect(mocks.view.applyContent).toHaveBeenCalledWith(row));
      await waitFor(() => expect(document.activeElement).toBe(titleButton()));
      expect(editorTextarea()).toBeNull();
      expect(container.querySelector('.doc-viewer--view')).not.toBeNull();
    });

    it('Cancel returns to view and focuses the title', () => {
      setView({ doc: doc() });
      renderViewer();
      clickEdit();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(editorTextarea()).toBeNull();
      expect(document.activeElement).toBe(titleButton());
    });

    it('leaving History focuses the title too', () => {
      setView({ doc: doc() });
      renderViewer();
      openMenuItem(/History/);
      fireEvent.click(screen.getByRole('button', { name: /Back to doc/ }));
      expect(document.activeElement).toBe(titleButton());
    });

    it('a 409 on save re-fetches the doc', async () => {
      const current = doc();
      mocks.updateDocContent.mockRejectedValue(
        new ApiClientError('stale', 409, 'stale_update', undefined, {
          error: 'stale_update',
          current: { ...current, updated_at: '2026-10-06T23:00:00' },
        }),
      );
      mocks.fetchDoc.mockResolvedValue({ ...current, content: 'Theirs', updated_at: '2026-10-06T23:00:00' });
      setView({ doc: current });
      renderViewer();
      clickEdit();
      fireEvent.change(editorTextarea()!, { target: { value: 'Mine' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
      await waitFor(() => expect(mocks.view.refresh).toHaveBeenCalledTimes(1));
      expect(await screen.findByText(/Someone changed this doc/)).toBeTruthy();
    });
  });

  describe('unsaved drafts', () => {
    afterEach(() => {
      localStorage.clear();
    });

    it('a doc that vanishes mid-edit shows the draft read-only: copy, download, discard', async () => {
      setView({ doc: doc() });
      const { rerender } = renderViewer();
      clickEdit();
      fireEvent.change(editorTextarea()!, { target: { value: 'Unsaved words' } });

      // Deleted elsewhere / share revoked: the re-fetch 404s.
      setView({ notFound: true });
      rerender(viewerUi());
      expect(screen.getByText('This doc no longer exists.')).toBeTruthy();
      const panel = screen.getByRole('region', { name: 'Unsaved draft' });
      expect(within(panel).getByText(/^Your unsaved draft from .* is still in this browser\.$/)).toBeTruthy();
      expect(within(panel).getByLabelText('Draft text').textContent).toBe('Unsaved words');

      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
      fireEvent.click(within(panel).getByRole('button', { name: 'Copy text' }));
      expect(writeText).toHaveBeenCalledWith('Unsaved words');
      expect(await within(panel).findByText('Copied to the clipboard.')).toBeTruthy();

      const createObjectURL = vi.fn(() => 'blob:draft');
      const revokeObjectURL = vi.fn();
      Object.assign(URL, { createObjectURL, revokeObjectURL });
      const clicked: { download: string; href: string }[] = [];
      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
        clicked.push({ download: this.download, href: this.getAttribute('href') ?? '' });
      });
      fireEvent.click(within(panel).getByRole('button', { name: 'Download .md' }));
      expect(clicked).toEqual([{ download: 'Roadmap.md', href: 'blob:draft' }]);
      const blob = (createObjectURL.mock.calls[0] as unknown as [Blob])[0];
      const text = await new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsText(blob);
      });
      expect(text).toBe('Unsaved words');
      expect(blob.type).toBe('text/markdown;charset=utf-8');
      click.mockRestore();

      fireEvent.click(within(panel).getByRole('button', { name: 'Discard' }));
      expect(within(screen.getByRole('dialog')).getByText('Discard your unsaved draft?')).toBeTruthy();
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Discard draft' }));
      expect(screen.queryByRole('region', { name: 'Unsaved draft' })).toBeNull();
      expect(localStorage.getItem(DRAFT_KEY)).toBeNull();
      expect(screen.getByText('This doc no longer exists.')).toBeTruthy();
      Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    });

    it('a plain 404 without a draft shows no panel', () => {
      setView({ notFound: true });
      renderViewer();
      expect(screen.queryByRole('region', { name: 'Unsaved draft' })).toBeNull();
    });

    it('view mode offers to resume a stored draft; the editor then offers the restore', () => {
      storeDraft('Older draft');
      setView({ doc: doc() });
      renderViewer();
      const notice = screen.getByText(/^You have unsaved changes from /);
      expect(notice).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: 'Resume editing' }));
      expect(editorTextarea()).not.toBeNull();
      expect(screen.getByText(/^Restore unsaved draft from /, { selector: 'p' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Resume editing' })).toBeNull();
    });

    it('a Cancel that keeps the undecided draft brings the notice back', () => {
      storeDraft('Older draft');
      setView({ doc: doc() });
      renderViewer();
      fireEvent.click(screen.getByRole('button', { name: 'Resume editing' }));
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(screen.getByRole('button', { name: 'Resume editing' })).toBeTruthy();
    });

    it('no notice for a draft equal to the doc', () => {
      storeDraft('# Plan\n\n![chart](assets/chart.png)');
      setView({ doc: doc() });
      renderViewer();
      expect(screen.queryByText(/^You have unsaved changes from /)).toBeNull();
    });

    it('without edit access any more, the draft shows read-only instead', () => {
      storeDraft('Older draft');
      const base = doc();
      setView({ doc: doc({ access: { ...base.access, can_edit: false, write: 'approval' } }) });
      renderViewer();
      expect(screen.queryByRole('button', { name: 'Resume editing' })).toBeNull();
      const panel = screen.getByRole('region', { name: 'Unsaved draft' });
      expect(within(panel).getByText(/^You no longer have edit access to this doc\./)).toBeTruthy();
      expect(within(panel).getByLabelText('Draft text').textContent).toBe('Older draft');
    });

    it("the owner's own delete drops the draft", async () => {
      storeDraft('Older draft');
      mocks.deleteDoc.mockResolvedValue({ deleted: true });
      setView({ doc: doc() });
      renderViewer();
      openMenuItem(/Delete/);
      await act(async () => {
        fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
      });
      expect(mocks.deleteDoc).toHaveBeenCalledWith('d1');
      expect(localStorage.getItem(DRAFT_KEY)).toBeNull();
    });
  });
});
