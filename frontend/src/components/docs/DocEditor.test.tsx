// DocEditor: save (token, changed=false, Ctrl/Cmd+S and where it applies),
// the 409 flow (metadata-only bumps re-save silently, real body changes show
// the conflict banner, onStale), 403 / other errors, image uploads
// (placeholder at the cursor, swap / removal / append, token adoption incl.
// a pending conflict's, 5 MB pre-check, paste / drop), the concurrent-change
// banner, Tab / Shift+Tab / Escape, the discard confirms (Cancel, links,
// both banners, uploads in flight), the per-user localStorage draft backup
// (debounce, unmount / visibility flush, failure notice, pruning, the
// restore offer), and the preview (debounce, large docs, phone panes).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { DocAssetUploadResponse, DocContentResponse, DocDetail } from '../../api/types';
import { DocEditor } from './DocEditor';

const mocks = vi.hoisted(() => ({
  isMobile: false,
  updateDocContent: vi.fn(),
  uploadDocAsset: vi.fn(),
  fetchDoc: vi.fn(),
}));

vi.mock('../../hooks/useIsMobile', () => ({
  useIsMobile: () => mocks.isMobile,
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ userEmail: 'Me@Example.com' }),
}));

vi.mock('../../api/docsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/docsApi')>();
  return {
    ...actual,
    updateDocContent: mocks.updateDocContent,
    uploadDocAsset: mocks.uploadDocAsset,
    fetchDoc: mocks.fetchDoc,
  };
});

// Message.tsx (markdownComponents) reaches pdfjs-dist through its card
// imports; pdf.js needs browser canvas APIs jsdom lacks.
vi.mock('../PdfViewer', () => ({ PdfViewer: () => null }));

const T0 = '2026-10-06T09:00:00';
const T1 = '2026-10-06T10:00:00';
const T1_UPLOAD = '2026-10-06T10:00:30';
const T2 = '2026-10-06T10:05:00';
const T2_UPLOAD = '2026-10-06T10:05:30';
const T3 = '2026-10-06T10:10:00';
// Per user: the lower-cased, URI-encoded sign-in email.
const DRAFT_KEY = 'quest_doc_draft:me%40example.com:d1';
const PLACEHOLDER = '![Uploading chart.png…]()';
const CHART = '![chart](assets/chart.png)';
const CHANGED = 'This doc changed since you started editing.';

function doc(overrides: Partial<DocDetail> = {}): DocDetail {
  return {
    id: 'd1',
    owner_id: 1,
    project_id: null,
    title: 'Roadmap',
    description: '',
    mode: 'private',
    content_size: 13,
    asset_count: 0,
    last_write_source: 'ui',
    created_at: '2026-10-01T00:00:00',
    updated_at: T1,
    scope: 'user',
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    access: {
      can_rename: true, can_switch_mode: false, can_delete: true, write: 'free',
      can_edit: true, can_share: true, can_delete_assets: true,
    },
    shares: [],
    content: 'Original body',
    last_write_conversation: null,
    assets: [],
    ...overrides,
  };
}

function savedRow(overrides: Partial<DocContentResponse> = {}): DocContentResponse {
  return { ...doc(), updated_at: T2, content: 'saved', changed: true, ...overrides };
}

function uploadResult(overrides: Partial<DocAssetUploadResponse> = {}): DocAssetUploadResponse {
  return {
    asset: { name: 'chart.png', size: 10, mime: 'image/png' },
    markdown: CHART,
    asset_count: 1,
    updated_at: T1_UPLOAD,
    previous_updated_at: T1,
    ...overrides,
  };
}

function staleError(currentUpdatedAt: string): ApiClientError {
  return new ApiClientError('stale', 409, 'stale_update', undefined, {
    error: 'stale_update',
    message: 'stale',
    current: { ...doc(), updated_at: currentUpdatedAt },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderEditor(initial: DocDetail = doc()) {
  const onSaved = vi.fn();
  const onCancel = vi.fn();
  const onStale = vi.fn();
  const ui = (current: DocDetail) => (
    <MemoryRouter initialEntries={['/docs/d1']}>
      <Routes>
        <Route
          path="/docs/:id"
          element={
            <>
              <input aria-label="Elsewhere" />
              <DocEditor doc={current} onSaved={onSaved} onCancel={onCancel} onStale={onStale} />
              <Link to="/chats/c1">Some chat</Link>
              <LocationProbe />
            </>
          }
        />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  );
  const result = render(ui(initial));
  return {
    ...result,
    onSaved,
    onCancel,
    onStale,
    rerenderDoc: (next: DocDetail) => result.rerender(ui(next)),
  };
}

function textarea(): HTMLTextAreaElement {
  return screen.getByRole('textbox', { name: 'Doc markdown' }) as HTMLTextAreaElement;
}

function type(value: string) {
  fireEvent.change(textarea(), { target: { value } });
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
}

function toolbarStatus(container: HTMLElement): string {
  return container.querySelector('.doc-editor-status')?.textContent ?? '';
}

function fileInput(container: HTMLElement): HTMLInputElement {
  return container.querySelector('input[type="file"]') as HTMLInputElement;
}

function pickImage(container: HTMLElement, name = 'chart.png', size?: number) {
  const file = new File(['png'], name, { type: 'image/png' });
  if (size !== undefined) Object.defineProperty(file, 'size', { value: size });
  fireEvent.change(fileInput(container), { target: { files: [file] } });
  return file;
}

function storedDraft(key = DRAFT_KEY): { content: string; base: string; saved_at: string; title?: string } | null {
  const raw = localStorage.getItem(key);
  return raw ? JSON.parse(raw) : null;
}

function storeDraft(content: string, base = T0, key = DRAFT_KEY, savedAt = new Date().toISOString()) {
  localStorage.setItem(key, JSON.stringify({ content, base, saved_at: savedAt, title: 'Roadmap' }));
}

function confirmDialog(): HTMLElement {
  return screen.getByRole('dialog');
}

function confirmIn(label: string) {
  fireEvent.click(within(confirmDialog()).getByRole('button', { name: label }));
}

describe('DocEditor', () => {
  beforeEach(() => {
    mocks.isMobile = false;
    mocks.updateDocContent.mockReset();
    mocks.uploadDocAsset.mockReset();
    mocks.fetchDoc.mockReset();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  describe('saving', () => {
    it('starts clean (Save disabled), focused on the doc body', () => {
      const { container } = renderEditor();
      expect(textarea().value).toBe('Original body');
      expect(document.activeElement).toBe(textarea());
      expect(textarea().getAttribute('spellcheck')).toBe('true');
      expect(saveButton().disabled).toBe(true);
      expect(saveButton().getAttribute('aria-keyshortcuts')).toBe('Control+S Meta+S');
      expect(toolbarStatus(container)).toBe('');
    });

    it('saves the draft against the base token and reports the row', async () => {
      const row = savedRow({ content: 'New body' });
      mocks.updateDocContent.mockResolvedValue(row);
      const { onSaved, container } = renderEditor();

      type('New body');
      expect(toolbarStatus(container)).toBe('Unsaved changes');
      expect(saveButton().disabled).toBe(false);
      fireEvent.click(saveButton());

      expect(mocks.updateDocContent).toHaveBeenCalledWith('d1', {
        content: 'New body',
        expected_updated_at: T1,
      });
      await waitFor(() => expect(onSaved).toHaveBeenCalledWith(row));
    });

    it('reports an unchanged save (changed=false) too', async () => {
      const row = savedRow({ updated_at: T1, content: 'Same', changed: false });
      mocks.updateDocContent.mockResolvedValue(row);
      const { onSaved } = renderEditor();
      type('Same');
      fireEvent.click(saveButton());
      await waitFor(() => expect(onSaved).toHaveBeenCalledWith(row));
    });

    it('saves on Ctrl+S / Cmd+S in the editor or on the bare page', async () => {
      mocks.updateDocContent.mockResolvedValue(savedRow());
      const { onSaved } = renderEditor();

      // Clean: nothing to save, but the browser's Save page is still suppressed.
      expect(fireEvent.keyDown(textarea(), { key: 's', ctrlKey: true })).toBe(false);
      expect(mocks.updateDocContent).not.toHaveBeenCalled();

      type('Edited');
      expect(fireEvent.keyDown(document.body, { key: 'S', metaKey: true })).toBe(false);
      expect(mocks.updateDocContent).toHaveBeenCalledWith('d1', {
        content: 'Edited',
        expected_updated_at: T1,
      });
      await waitFor(() => expect(onSaved).toHaveBeenCalled());
    });

    it('leaves Ctrl+S alone in another field and under a dialog', () => {
      renderEditor();
      type('Edited');
      expect(
        fireEvent.keyDown(screen.getByRole('textbox', { name: 'Elsewhere' }), { key: 's', ctrlKey: true }),
      ).toBe(true);

      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(confirmDialog()).toBeTruthy();
      expect(fireEvent.keyDown(textarea(), { key: 's', ctrlKey: true })).toBe(true);
      expect(mocks.updateDocContent).not.toHaveBeenCalled();
    });

    it('matches the S key by code only when the layout key is not a Latin letter', () => {
      mocks.updateDocContent.mockReturnValue(new Promise(() => {}));
      renderEditor();
      type('Edited');
      // Dvorak: the physical S key types "o" -- that is Ctrl+O, not save.
      expect(fireEvent.keyDown(textarea(), { key: 'o', code: 'KeyS', ctrlKey: true })).toBe(true);
      expect(mocks.updateDocContent).not.toHaveBeenCalled();
      // Russian: the S key types "ы".
      expect(fireEvent.keyDown(textarea(), { key: 'ы', code: 'KeyS', ctrlKey: true })).toBe(false);
      expect(mocks.updateDocContent).toHaveBeenCalledTimes(1);
    });

    it('on a 403 says edit access is gone and disables Save', async () => {
      mocks.updateDocContent.mockRejectedValue(new ApiClientError('Forbidden', 403, 'forbidden'));
      renderEditor();
      type('Mine');
      fireEvent.click(saveButton());
      expect((await screen.findByRole('alert')).textContent).toBe(
        'You no longer have edit access to this doc.',
      );
      expect(saveButton().disabled).toBe(true);
      expect(textarea().value).toBe('Mine');
    });

    it('shows the same notice when a re-fetched row no longer grants editing; Tab is not trapped', () => {
      const { rerenderDoc } = renderEditor();
      type('Mine');
      const base = doc();
      rerenderDoc(doc({ access: { ...base.access, can_edit: false, write: 'approval' } }));
      expect(screen.getByRole('alert').textContent).toBe('You no longer have edit access to this doc.');
      expect(saveButton().disabled).toBe(true);
      expect((screen.getByRole('button', { name: 'Add image' }) as HTMLButtonElement).disabled).toBe(true);
      expect(textarea().value).toBe('Mine');
      // Read-only: Tab moves focus instead of indenting.
      expect(fireEvent.keyDown(textarea(), { key: 'Tab' })).toBe(true);
      expect(textarea().value).toBe('Mine');
    });

    it('shows the server message for content_too_large and keeps the draft', async () => {
      mocks.updateDocContent.mockRejectedValue(
        new ApiClientError('The doc is larger than 1 MB.', 400, 'content_too_large'),
      );
      renderEditor();
      type('Huge');
      fireEvent.click(saveButton());
      expect((await screen.findByRole('alert')).textContent).toBe('The doc is larger than 1 MB.');
      expect(textarea().value).toBe('Huge');
      expect(saveButton().disabled).toBe(false);
    });
  });

  describe('409 stale_update', () => {
    it('re-saves silently when only metadata moved (a rename during the edit)', async () => {
      const row = savedRow({ content: 'Mine' });
      mocks.updateDocContent.mockRejectedValueOnce(staleError(T2)).mockResolvedValueOnce(row);
      // The body is still the one the editor started from.
      mocks.fetchDoc.mockResolvedValue(doc({ title: 'Renamed', updated_at: T2 }));
      const { onSaved, onStale } = renderEditor();

      type('Mine');
      fireEvent.click(saveButton());
      await waitFor(() => expect(onSaved).toHaveBeenCalledWith(row));
      expect(mocks.fetchDoc).toHaveBeenCalledWith('d1');
      expect(mocks.updateDocContent).toHaveBeenNthCalledWith(2, 'd1', {
        content: 'Mine',
        expected_updated_at: T2,
      });
      expect(onStale).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('shows the conflict banner for a real body change and overwrites with the current token', async () => {
      const row = savedRow({ content: 'Mine' });
      mocks.updateDocContent.mockRejectedValueOnce(staleError(T2)).mockResolvedValueOnce(row);
      mocks.fetchDoc.mockResolvedValue(doc({ content: 'Theirs', updated_at: T2 }));
      const { onSaved, onCancel, onStale } = renderEditor();

      type('Mine');
      fireEvent.click(saveButton());
      const banner = await screen.findByRole('alert');
      expect(banner.textContent).toContain(
        'Someone changed this doc while you were editing. Your changes are still here.',
      );
      expect(banner.textContent).toContain('History');
      expect(mocks.updateDocContent).toHaveBeenCalledTimes(1);
      expect(onStale).toHaveBeenCalledTimes(1);
      expect(textarea().value).toBe('Mine');
      expect(onSaved).not.toHaveBeenCalled();

      fireEvent.click(within(banner).getByRole('button', { name: 'Overwrite with my version' }));
      expect(mocks.updateDocContent).toHaveBeenLastCalledWith('d1', {
        content: 'Mine',
        expected_updated_at: T2,
      });
      await waitFor(() => expect(onSaved).toHaveBeenCalledWith(row));
      expect(onCancel).not.toHaveBeenCalled();
    });

    it('shows the banner when the one silent retry loses a race too', async () => {
      mocks.updateDocContent
        .mockRejectedValueOnce(staleError(T2))
        .mockRejectedValueOnce(staleError(T3));
      mocks.fetchDoc.mockResolvedValue(doc({ updated_at: T2 }));
      const { onStale } = renderEditor();
      type('Mine');
      fireEvent.click(saveButton());
      expect((await screen.findByRole('alert')).textContent).toContain('Someone changed this doc');
      expect(mocks.updateDocContent).toHaveBeenCalledTimes(2);
      expect(onStale).toHaveBeenCalledTimes(2);

      mocks.updateDocContent.mockResolvedValueOnce(savedRow());
      fireEvent.click(screen.getByRole('button', { name: 'Overwrite with my version' }));
      expect(mocks.updateDocContent).toHaveBeenLastCalledWith('d1', expect.objectContaining({
        expected_updated_at: T3,
      }));
    });

    it('"Discard my changes" on the conflict asks first, then leaves and drops the backup', async () => {
      mocks.updateDocContent.mockRejectedValueOnce(staleError(T2));
      mocks.fetchDoc.mockResolvedValue(doc({ content: 'Theirs', updated_at: T2 }));
      const { onCancel, unmount } = renderEditor();
      type('Mine');
      fireEvent.click(saveButton());
      const banner = await screen.findByRole('alert');
      fireEvent.click(within(banner).getByRole('button', { name: 'Discard my changes' }));
      expect(within(confirmDialog()).getByText('Discard your unsaved changes?')).toBeTruthy();
      expect(onCancel).not.toHaveBeenCalled();

      confirmIn('Discard changes');
      expect(onCancel).toHaveBeenCalledTimes(1);
      unmount();
      expect(storedDraft()).toBeNull();
    });

    it("advances a pending conflict's token with the editor's own upload", async () => {
      mocks.updateDocContent.mockRejectedValueOnce(staleError(T2));
      mocks.fetchDoc.mockResolvedValue(doc({ content: 'Theirs', updated_at: T2 }));
      mocks.uploadDocAsset.mockResolvedValue(
        uploadResult({ previous_updated_at: T2, updated_at: T2_UPLOAD }),
      );
      const { container } = renderEditor();
      type('Mine');
      fireEvent.click(saveButton());
      await screen.findByRole('alert');

      pickImage(container);
      await waitFor(() => expect(textarea().value).toContain(CHART));
      mocks.updateDocContent.mockResolvedValueOnce(savedRow());
      fireEvent.click(screen.getByRole('button', { name: 'Overwrite with my version' }));
      expect(mocks.updateDocContent).toHaveBeenLastCalledWith('d1', expect.objectContaining({
        expected_updated_at: T2_UPLOAD,
      }));
    });
  });

  describe('images', () => {
    it('puts a placeholder at the cursor, swaps in the markdown and adopts the token', async () => {
      const upload = deferred<DocAssetUploadResponse>();
      mocks.uploadDocAsset.mockReturnValue(upload.promise);
      mocks.updateDocContent.mockResolvedValue(savedRow());
      const { container } = renderEditor(doc({ content: 'Line one\nLine two' }));

      textarea().setSelectionRange(8, 8); // end of "Line one"
      const file = pickImage(container);
      expect(textarea().value).toBe(`Line one\n${PLACEHOLDER}\nLine two`);
      expect(toolbarStatus(container)).toBe('Uploading image...');
      expect(saveButton().disabled).toBe(true);
      await waitFor(() => expect(mocks.uploadDocAsset).toHaveBeenCalledWith('d1', file));

      await act(async () => {
        upload.resolve(uploadResult());
      });
      expect(textarea().value).toBe(`Line one\n${CHART}\nLine two`);
      expect(textarea().selectionStart).toBe(8 + 1 + CHART.length);

      fireEvent.click(saveButton());
      expect(mocks.updateDocContent).toHaveBeenCalledWith('d1', {
        content: `Line one\n${CHART}\nLine two`,
        expected_updated_at: T1_UPLOAD,
      });
    });

    it('replaces the selection with the image', async () => {
      mocks.uploadDocAsset.mockResolvedValue(uploadResult());
      const { container } = renderEditor(doc({ content: 'a PLACEHOLDER b' }));
      textarea().setSelectionRange(2, 13);
      pickImage(container);
      await waitFor(() => expect(textarea().value).toBe(`a \n${CHART}\n b`));
    });

    it('keeps its token when the upload was taken over a different one', async () => {
      mocks.uploadDocAsset.mockResolvedValue(uploadResult({ previous_updated_at: T0 }));
      mocks.updateDocContent.mockResolvedValue(savedRow());
      const { container } = renderEditor();
      textarea().setSelectionRange(0, 0);
      pickImage(container);
      await waitFor(() => expect(textarea().value).toBe(`${CHART}\nOriginal body`));
      fireEvent.click(saveButton());
      expect(mocks.updateDocContent).toHaveBeenCalledWith('d1', expect.objectContaining({
        expected_updated_at: T1,
      }));
    });

    it('removes the placeholder when the upload fails and shows the error', async () => {
      mocks.uploadDocAsset.mockRejectedValue(
        new ApiClientError('That is not a supported image.', 400, 'invalid_image'),
      );
      const { container } = renderEditor();
      textarea().setSelectionRange(13, 13);
      pickImage(container, 'notes.png');
      expect(textarea().value).toBe('Original body\n![Uploading notes.png…]()');
      const alert = await screen.findByRole('alert');
      expect(alert.textContent).toBe('Could not add notes.png: That is not a supported image.');
      expect(textarea().value).toBe('Original body');
      expect(saveButton().disabled).toBe(true);
      fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss' }));
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('appends the image with a notice when its placeholder was deleted meanwhile', async () => {
      const upload = deferred<DocAssetUploadResponse>();
      mocks.uploadDocAsset.mockReturnValue(upload.promise);
      const { container } = renderEditor();
      textarea().setSelectionRange(0, 0);
      pickImage(container);
      type('Rewritten');
      await act(async () => {
        upload.resolve(uploadResult());
      });
      expect(textarea().value).toBe(`Rewritten\n${CHART}`);
      expect(
        screen.getByText('chart.png was added at the end of the doc because its placeholder was removed.', {
          selector: 'p',
        }),
      ).toBeTruthy();
    });

    it('refuses images over 5 MB before uploading', () => {
      const { container } = renderEditor();
      pickImage(container, 'huge.png', 5 * 1024 * 1024 + 1);
      expect(screen.getByRole('alert').textContent).toBe(
        'Could not add huge.png: Images can be at most 5 MB.',
      );
      expect(mocks.uploadDocAsset).not.toHaveBeenCalled();
      expect(textarea().value).toBe('Original body');
    });

    it('does not flag its own adopted upload as a concurrent change', async () => {
      mocks.uploadDocAsset.mockResolvedValue(uploadResult());
      const { container, rerenderDoc } = renderEditor();
      pickImage(container);
      await waitFor(() => expect(textarea().value).toContain(CHART));
      // The realtime re-fetch lands with the upload's bump.
      rerenderDoc(doc({ updated_at: T1_UPLOAD, asset_count: 1 }));
      expect(screen.queryByText(CHANGED, { selector: 'p' })).toBeNull();
    });

    it('uploads a pasted image file, but lets a paste carrying text through', async () => {
      mocks.uploadDocAsset.mockResolvedValue(uploadResult());
      const { container } = renderEditor();
      const image = new File(['png'], 'image.png', { type: 'image/png' });

      const withText = fireEvent.paste(textarea(), {
        clipboardData: { files: [image], items: [], types: ['Files', 'text/plain'], getData: () => 'cell text' },
      });
      expect(withText).toBe(true);
      await Promise.resolve();
      expect(mocks.uploadDocAsset).not.toHaveBeenCalled();
      expect(toolbarStatus(container)).toBe('');

      const imageOnly = fireEvent.paste(textarea(), {
        clipboardData: { files: [image], items: [], types: ['Files'], getData: () => '' },
      });
      expect(imageOnly).toBe(false);
      await waitFor(() => expect(mocks.uploadDocAsset).toHaveBeenCalledWith('d1', image));
      await waitFor(() => expect(textarea().value).toContain(CHART));
    });

    it('uploads an image file dropped on the textarea', async () => {
      mocks.uploadDocAsset.mockResolvedValue(uploadResult());
      renderEditor();
      const image = new File(['png'], 'drop.png', { type: 'image/png' });
      const dataTransfer = { files: [image], items: [], types: ['Files'], dropEffect: 'none' };
      expect(fireEvent.dragOver(textarea(), { dataTransfer })).toBe(false);
      expect(fireEvent.drop(textarea(), { dataTransfer })).toBe(false);
      await waitFor(() => expect(mocks.uploadDocAsset).toHaveBeenCalledWith('d1', image));
    });
  });

  describe('concurrent changes', () => {
    it('shows a non-blocking banner for a new body; Keep editing hides it until the next change', () => {
      const { rerenderDoc, container } = renderEditor();
      type('Mine');
      rerenderDoc(doc({ updated_at: T2, content: 'Theirs' }));

      expect(screen.getByText(CHANGED, { selector: 'p' })).toBeTruthy();
      // Announced through the always-present polite region.
      expect(container.querySelector('.doc-editor-sr-only[role="status"]')?.textContent).toBe(CHANGED);
      expect(textarea().value).toBe('Mine');
      fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
      expect(screen.queryByText(CHANGED, { selector: 'p' })).toBeNull();

      rerenderDoc(doc({ updated_at: T3, content: 'Theirs again' }));
      expect(screen.getByText(CHANGED, { selector: 'p' })).toBeTruthy();
    });

    it('ignores metadata-only bumps (a rename keeps the body)', () => {
      const { rerenderDoc } = renderEditor();
      type('Mine');
      rerenderDoc(doc({ updated_at: T2, title: 'Renamed' }));
      expect(screen.queryByText(CHANGED, { selector: 'p' })).toBeNull();
    });

    it('waits while an upload is in flight', async () => {
      const upload = deferred<DocAssetUploadResponse>();
      mocks.uploadDocAsset.mockReturnValue(upload.promise);
      const { container, rerenderDoc } = renderEditor();
      pickImage(container);
      rerenderDoc(doc({ updated_at: T2, content: 'Theirs' }));
      expect(screen.queryByText(CHANGED, { selector: 'p' })).toBeNull();

      await act(async () => {
        upload.resolve(uploadResult());
      });
      expect(screen.getByText(CHANGED, { selector: 'p' })).toBeTruthy();
    });

    it('"Discard my changes" asks first, then reloads the latest version into the editor', () => {
      mocks.updateDocContent.mockResolvedValue(savedRow());
      const { rerenderDoc } = renderEditor();
      type('Mine');
      rerenderDoc(doc({ updated_at: T2, content: 'Theirs' }));
      fireEvent.click(screen.getByRole('button', { name: 'Discard my changes' }));
      expect(within(confirmDialog()).getByText('Discard your unsaved changes?')).toBeTruthy();
      expect(textarea().value).toBe('Mine');
      confirmIn('Discard changes');

      expect(textarea().value).toBe('Theirs');
      expect(screen.queryByText(CHANGED, { selector: 'p' })).toBeNull();
      expect(saveButton().disabled).toBe(true);
      type('Theirs, edited');
      fireEvent.click(saveButton());
      expect(mocks.updateDocContent).toHaveBeenCalledWith('d1', {
        content: 'Theirs, edited',
        expected_updated_at: T2,
      });
    });

    it('an untouched editor follows the new version silently', () => {
      const { rerenderDoc } = renderEditor();
      rerenderDoc(doc({ updated_at: T2, content: 'Theirs' }));
      expect(textarea().value).toBe('Theirs');
      expect(screen.queryByText(CHANGED, { selector: 'p' })).toBeNull();
    });
  });

  describe('keyboard', () => {
    it('Tab inserts two spaces at a bare cursor', () => {
      renderEditor(doc({ content: 'abc' }));
      textarea().setSelectionRange(1, 1);
      expect(fireEvent.keyDown(textarea(), { key: 'Tab' })).toBe(false);
      expect(textarea().value).toBe('a  bc');
      expect([textarea().selectionStart, textarea().selectionEnd]).toEqual([3, 3]);
    });

    it('Tab indents the line of a one-line selection instead of replacing the text', () => {
      renderEditor(doc({ content: 'abc def' }));
      textarea().setSelectionRange(4, 7); // "def"
      fireEvent.keyDown(textarea(), { key: 'Tab' });
      expect(textarea().value).toBe('  abc def');
      expect([textarea().selectionStart, textarea().selectionEnd]).toEqual([6, 9]);
    });

    it('Tab indents and Shift+Tab outdents every selected line', () => {
      renderEditor(doc({ content: 'one\ntwo\nthree' }));
      textarea().setSelectionRange(0, 7); // "one\ntwo"
      fireEvent.keyDown(textarea(), { key: 'Tab' });
      expect(textarea().value).toBe('  one\n  two\nthree');
      expect([textarea().selectionStart, textarea().selectionEnd]).toEqual([0, 11]);

      fireEvent.keyDown(textarea(), { key: 'Tab', shiftKey: true });
      expect(textarea().value).toBe('one\ntwo\nthree');
      expect([textarea().selectionStart, textarea().selectionEnd]).toEqual([0, 7]);
    });

    it('Shift+Tab outdents the cursor line', () => {
      renderEditor(doc({ content: 'a\n    b' }));
      textarea().setSelectionRange(6, 6);
      fireEvent.keyDown(textarea(), { key: 'Tab', shiftKey: true });
      expect(textarea().value).toBe('a\n  b');
      expect(textarea().selectionStart).toBe(4);
    });

    it('Escape releases the Tab trap for the next Tab', () => {
      renderEditor(doc({ content: 'abc' }));
      textarea().setSelectionRange(0, 0);
      fireEvent.keyDown(textarea(), { key: 'Escape' });
      // Not prevented: the browser moves focus on.
      expect(fireEvent.keyDown(textarea(), { key: 'Tab' })).toBe(true);
      expect(textarea().value).toBe('abc');
      // The trap is back for the Tab after that.
      expect(fireEvent.keyDown(textarea(), { key: 'Tab' })).toBe(false);
      expect(textarea().value).toBe('  abc');
    });

    it('edits through execCommand("insertText") when the browser has it (undo keeps working)', () => {
      const execCommand = vi.fn((command: string, _ui: boolean, text?: string) => {
        const element = document.activeElement as HTMLTextAreaElement;
        if (command !== 'insertText' || text === undefined) return false;
        element.setRangeText(text, element.selectionStart, element.selectionEnd, 'end');
        element.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      });
      Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true });
      try {
        renderEditor(doc({ content: 'abc' }));
        textarea().setSelectionRange(3, 3);
        fireEvent.keyDown(textarea(), { key: 'Tab' });
        expect(execCommand).toHaveBeenCalledWith('insertText', false, '  ');
        expect(textarea().value).toBe('abc  ');
        expect(saveButton().disabled).toBe(false);
      } finally {
        delete (document as unknown as { execCommand?: unknown }).execCommand;
      }
    });
  });

  describe('unsaved-changes guard', () => {
    it('Cancel leaves at once while clean', () => {
      const { onCancel } = renderEditor();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(onCancel).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('Cancel while dirty asks to discard first', () => {
      const { onCancel, unmount } = renderEditor();
      type('Mine');
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(within(confirmDialog()).getByText('Discard your unsaved changes?')).toBeTruthy();

      confirmIn('Cancel');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(onCancel).not.toHaveBeenCalled();
      expect(textarea().value).toBe('Mine');

      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      confirmIn('Discard changes');
      expect(onCancel).toHaveBeenCalledTimes(1);
      unmount();
      expect(storedDraft()).toBeNull();
    });

    it('Cancel while an image uploads asks first, even with the placeholder gone', () => {
      mocks.uploadDocAsset.mockReturnValue(new Promise(() => {}));
      const { container, onCancel } = renderEditor();
      pickImage(container);
      type('Original body'); // the placeholder deleted: the text is clean again
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(within(confirmDialog()).getByText(/an image is still uploading/)).toBeTruthy();
      expect(onCancel).not.toHaveBeenCalled();
    });

    it('intercepts an in-app link while dirty; the confirm leaves edit mode and navigates', () => {
      const { onCancel } = renderEditor();
      type('Mine');
      fireEvent.click(screen.getByRole('link', { name: 'Some chat' }));
      expect(screen.getByTestId('location').textContent).toBe('/docs/d1');

      confirmIn('Discard changes');
      expect(onCancel).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('location').textContent).toBe('/chats/c1');
      expect(storedDraft()).toBeNull();
    });

    it('lets links through while clean', () => {
      renderEditor();
      fireEvent.click(screen.getByRole('link', { name: 'Some chat' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByTestId('location').textContent).toBe('/chats/c1');
    });

    it('asks the browser to confirm unloading only while dirty', () => {
      renderEditor();
      const clean = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(clean);
      expect(clean.defaultPrevented).toBe(false);

      type('Mine');
      const dirty = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(dirty);
      expect(dirty.defaultPrevented).toBe(true);
    });
  });

  describe('draft backup', () => {
    it('backs the draft up per user ~500 ms after typing stops', () => {
      vi.useFakeTimers();
      renderEditor();
      type('Mine');
      act(() => {
        vi.advanceTimersByTime(400);
      });
      expect(storedDraft()).toBeNull();
      act(() => {
        vi.advanceTimersByTime(100);
      });
      expect(storedDraft()).toMatchObject({ content: 'Mine', base: T1, title: 'Roadmap' });
      expect(typeof storedDraft()?.saved_at).toBe('string');

      // Back to the original text: nothing left to back up.
      type('Original body');
      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(storedDraft()).toBeNull();
    });

    it('writes the backup at once when the editor goes away unsaved (back button)', () => {
      const { unmount } = renderEditor();
      type('Mine');
      unmount();
      expect(storedDraft()).toMatchObject({ content: 'Mine', base: T1 });
    });

    it('writes the backup at once when the page is hidden', () => {
      renderEditor();
      type('Mine');
      Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
      try {
        document.dispatchEvent(new Event('visibilitychange'));
        expect(storedDraft()).toMatchObject({ content: 'Mine' });
      } finally {
        delete (document as unknown as { visibilityState?: unknown }).visibilityState;
      }
    });

    it("says so when the browser won't store the backup", () => {
      vi.useFakeTimers();
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
        throw new Error('QuotaExceededError');
      });
      const { container } = renderEditor();
      type('Mine');
      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(toolbarStatus(container)).toBe("Couldn't back up your draft in this browser");
    });

    it('clears the backup on save', async () => {
      mocks.updateDocContent.mockResolvedValue(savedRow());
      const { onSaved, unmount } = renderEditor();
      type('Mine');
      fireEvent.click(saveButton());
      await waitFor(() => expect(onSaved).toHaveBeenCalled());
      unmount();
      expect(storedDraft()).toBeNull();
    });

    it("prunes legacy unscoped and month-old drafts, and ignores another user's", () => {
      const old = new Date(Date.now() - 31 * 24 * 60 * 60 * 1000).toISOString();
      storeDraft('Legacy', T0, 'quest_doc_draft:d1');
      storeDraft('Ancient', T0, 'quest_doc_draft:me%40example.com:d2', old);
      storeDraft('Recent', T0, 'quest_doc_draft:me%40example.com:d3');
      storeDraft('Theirs', T0, 'quest_doc_draft:other%40example.com:d1');
      renderEditor();

      expect(localStorage.getItem('quest_doc_draft:d1')).toBeNull();
      expect(localStorage.getItem('quest_doc_draft:me%40example.com:d2')).toBeNull();
      expect(storedDraft('quest_doc_draft:me%40example.com:d3')?.content).toBe('Recent');
      expect(storedDraft('quest_doc_draft:other%40example.com:d1')?.content).toBe('Theirs');
      expect(screen.queryByText(/^Restore unsaved draft from /, { selector: 'p' })).toBeNull();
    });

    it('offers a differing backup; Restore brings back its text and token (no silent re-save)', async () => {
      storeDraft('My old draft');
      mocks.updateDocContent.mockRejectedValue(staleError(T1));
      // The server body equals the editor's starting body, but the body the
      // backup was edited against is unknown: no silent overwrite.
      mocks.fetchDoc.mockResolvedValue(doc());
      renderEditor();

      expect(textarea().value).toBe('Original body');
      expect(screen.getByText(/^Restore unsaved draft from /, { selector: 'p' })).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: 'Restore' }));

      expect(textarea().value).toBe('My old draft');
      expect(screen.queryByText(/^Restore unsaved draft from /, { selector: 'p' })).toBeNull();
      // The doc moved on since the backup's token.
      expect(screen.getByText(CHANGED, { selector: 'p' })).toBeTruthy();

      fireEvent.click(saveButton());
      expect(mocks.updateDocContent).toHaveBeenCalledWith('d1', {
        content: 'My old draft',
        expected_updated_at: T0,
      });
      expect((await screen.findByRole('alert')).textContent).toContain('Someone changed this doc');
      expect(mocks.updateDocContent).toHaveBeenCalledTimes(1);
    });

    it('confirms Restore when the editor already has changes', () => {
      storeDraft('My old draft');
      renderEditor();
      type('Fresh typing');
      fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
      expect(within(confirmDialog()).getByText('Replace your changes with the saved draft?')).toBeTruthy();
      expect(textarea().value).toBe('Fresh typing');
      confirmIn('Restore draft');
      expect(textarea().value).toBe('My old draft');
    });

    it('while undecided, backs up the live draft and puts the offer back on discard', () => {
      vi.useFakeTimers();
      storeDraft('My old draft');
      const { unmount } = renderEditor();
      type('Fresh typing');
      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(storedDraft()?.content).toBe('Fresh typing');
      expect(screen.getByText(/^Restore unsaved draft from /, { selector: 'p' })).toBeTruthy();

      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      confirmIn('Discard changes');
      unmount();
      expect(storedDraft()?.content).toBe('My old draft');
    });

    it('Discard on the offer removes the backup', () => {
      storeDraft('My old draft');
      renderEditor();
      fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
      expect(screen.queryByText(/^Restore unsaved draft from /, { selector: 'p' })).toBeNull();
      expect(storedDraft()).toBeNull();
      expect(textarea().value).toBe('Original body');
    });

    it('keeps an undecided backup when leaving untouched, and ignores one equal to the doc', () => {
      storeDraft('My old draft');
      const first = renderEditor();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      first.unmount();
      expect(storedDraft()?.content).toBe('My old draft');

      storeDraft('Original body');
      renderEditor();
      expect(screen.queryByText(/^Restore unsaved draft from /, { selector: 'p' })).toBeNull();
    });
  });

  describe('preview', () => {
    it('renders the draft like the viewer, 250 ms after typing stops', async () => {
      renderEditor(doc({ content: '# Plan' }));
      const preview = screen.getByRole('region', { name: 'Preview' });
      expect(within(preview).getByRole('heading', { name: 'Plan' })).toBeTruthy();

      type('# Plan\n\n![chart](assets/chart.png)');
      // Not yet: no request for a half-typed asset name.
      expect(within(preview).queryByRole('img')).toBeNull();
      await waitFor(() =>
        expect(within(preview).getByRole('img', { name: 'chart' }).getAttribute('src')).toBe(
          '/app/api/docs/d1/assets/chart.png',
        ),
      );
    });

    it('updates a large doc only on "Refresh preview"', async () => {
      const big = `# Big\n\n${'x'.repeat(210 * 1024)}`;
      renderEditor(doc({ content: big }));
      const preview = screen.getByRole('region', { name: 'Preview' });
      const refresh = within(preview).getByRole('button', { name: 'Refresh preview' }) as HTMLButtonElement;
      expect(refresh.disabled).toBe(true);

      type(`# Bigger\n\n${'x'.repeat(210 * 1024)}`);
      await new Promise((resolve) => setTimeout(resolve, 300));
      expect(within(preview).getByRole('heading', { name: 'Big' })).toBeTruthy();
      expect(refresh.disabled).toBe(false);

      fireEvent.click(refresh);
      await waitFor(() => expect(within(preview).getByRole('heading', { name: 'Bigger' })).toBeTruthy());
    });

    it('can be hidden on desktop, remembered for next time', () => {
      renderEditor();
      const toggle = screen.getByRole('button', { name: 'Preview' });
      expect(toggle.getAttribute('aria-pressed')).toBe('true');
      fireEvent.click(toggle);
      expect(screen.queryByRole('region', { name: 'Preview' })).toBeNull();
      expect(toggle.getAttribute('aria-pressed')).toBe('false');
      cleanup();

      renderEditor();
      expect(screen.queryByRole('region', { name: 'Preview' })).toBeNull();
    });

    it('switches between Write and Preview on phones, without autofocus', () => {
      mocks.isMobile = true;
      renderEditor(doc({ content: '# Plan' }));
      expect(document.activeElement).not.toBe(textarea());
      expect(screen.queryByRole('region', { name: 'Preview' })).toBeNull();

      type('# Plan B');
      fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
      // Opening the Preview pane shows the current draft at once.
      expect(screen.getByRole('heading', { name: 'Plan B' })).toBeTruthy();
      expect(textarea().classList.contains('doc-editor-input--hidden')).toBe(true);

      fireEvent.click(screen.getByRole('button', { name: 'Write' }));
      expect(screen.queryByRole('region', { name: 'Preview' })).toBeNull();
      expect(textarea().classList.contains('doc-editor-input--hidden')).toBe(false);
    });
  });
});
