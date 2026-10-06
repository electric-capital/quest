// DocHistory: the version list (current first, unique entry names, author
// descriptions, conversation links, sizes, empty state), selecting a version
// and its detail fetch (body cache, stale responses, errors + Retry), the
// "Show changes" diff toggle, Restore (token frozen when the confirm opens,
// stale 409 -> re-fetched current body, gone 404, lost access), Copy
// (default / explicit title, duplicate_title, invalid_title, gone), the
// lost-access state, list refetches (new token, stale response dropped,
// refresh banner, selected revision dropping out), phones, and the keyboard.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type {
  Doc,
  DocContentResponse,
  DocDetail,
  DocRevisionDetail,
  DocRevisionsResponse,
  DocVersion,
} from '../../api/types';
import { DocHistory } from './DocHistory';

const mocks = vi.hoisted(() => ({
  fetchDoc: vi.fn(),
  fetchDocRevisions: vi.fn(),
  fetchDocRevision: vi.fn(),
  restoreDocRevision: vi.fn(),
  copyDocRevision: vi.fn(),
  isMobile: false,
}));

vi.mock('../../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/docsApi')>()),
  fetchDoc: mocks.fetchDoc,
  fetchDocRevisions: mocks.fetchDocRevisions,
  fetchDocRevision: mocks.fetchDocRevision,
  restoreDocRevision: mocks.restoreDocRevision,
  copyDocRevision: mocks.copyDocRevision,
}));

vi.mock('../../hooks/useIsMobile', () => ({
  useIsMobile: () => mocks.isMobile,
}));

// Message.tsx (markdownComponents) reaches pdfjs-dist through its card
// imports; pdf.js needs browser canvas APIs jsdom lacks.
vi.mock('../PdfViewer', () => ({ PdfViewer: () => null }));

const TOKEN = '2026-10-06T10:00:00.000001';
const NEWER_TOKEN = '2026-10-06T10:05:00.000002';
const NEWEST_TOKEN = '2026-10-06T10:09:00.000003';

const REV_1 = '20261006T080000Z-1';
const REV_2 = '20261005T090000Z-1';
const REV_3 = '20261004T090000Z-1';

const STALE_TEXT =
  'This doc changed while you were looking at History. The list was refreshed; check the version and try again.';
const GONE_TEXT = 'That version is no longer available.';
const NO_ACCESS_TEXT = 'You no longer have edit access to this doc.';
const RETENTION_TEXT =
  'Each change keeps the previous version here: up to 200 earlier versions, each kept for 30 days (the most recent earlier version is always kept).';

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function ago(ms: number): string {
  return new Date(Date.now() - ms).toISOString();
}

const READ_ONLY_ACCESS: DocDetail['access'] = {
  can_rename: false, can_switch_mode: false, can_delete: false, write: 'approval', can_edit: false, can_share: false, can_delete_assets: false,
};

function doc(overrides: Partial<DocDetail> = {}): DocDetail {
  return {
    id: 'd1',
    owner_id: 1,
    project_id: null,
    title: 'Roadmap',
    description: '',
    mode: 'private',
    content_size: 40,
    asset_count: 1,
    last_write_source: 'ui:1',
    created_at: '2026-10-01T00:00:00',
    updated_at: TOKEN,
    scope: 'user',
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free', can_edit: true, can_share: true, can_delete_assets: true },
    shares: [],
    content: '# Current plan\n\n![chart](assets/chart.png)',
    last_write_conversation: null,
    assets: [{ name: 'chart.png', size: 2048, mime: 'image/png' }],
    ...overrides,
  };
}

function version(overrides: Partial<DocVersion> = {}): DocVersion {
  return {
    id: null,
    written_at: ago(5 * MINUTE),
    replaced_at: null,
    size: 1200,
    source: 'ui:1',
    source_kind: 'ui',
    source_label: 'you',
    conversation: null,
    ...overrides,
  };
}

const REVISION_1 = version({
  id: REV_1,
  written_at: ago(2 * HOUR),
  replaced_at: ago(5 * MINUTE),
  size: 800,
  source: 'conversation:c9',
  source_kind: 'conversation',
  source_label: 'Quarterly planning',
  conversation: { id: 'c9', title: 'Quarterly planning', project_id: 'p1' },
});
const REVISION_2 = version({
  id: REV_2,
  written_at: ago(1 * DAY + HOUR),
  replaced_at: ago(2 * HOUR),
  size: 2048,
  source: 'conversation:c7',
  source_kind: 'conversation',
  source_label: 'Notes chat',
  conversation: { id: 'c7', title: 'Notes chat', project_id: null },
});
const REVISION_3 = version({
  id: REV_3,
  written_at: ago(2 * DAY + HOUR),
  replaced_at: ago(1 * DAY + HOUR),
  size: 100,
  source: 'ui:2',
  source_kind: 'ui',
  source_label: 'Bea Smith',
});

function revisions(revs: DocVersion[] = [REVISION_1, REVISION_2, REVISION_3]): DocRevisionsResponse {
  return { current: version(), revisions: revs };
}

function revisionDetail(
  rev: DocVersion,
  content: string,
  extra: Partial<DocRevisionDetail> = {},
): DocRevisionDetail {
  return { ...rev, content, ...extra };
}

const OLD_PLAN_DIFF = {
  added: 1,
  removed: 1,
  lines: [
    { type: 'del' as const, old_line: 1, new_line: null, text: '# Old plan' },
    { type: 'add' as const, old_line: null, new_line: 1, text: '# Current plan' },
    { type: 'context' as const, old_line: 2, new_line: 2, text: '' },
  ],
  truncated: false,
  total_old_lines: 3,
  total_new_lines: 3,
};

function defaultRevisionFetch(_id: string, rev: string, opts: { diff?: boolean } = {}) {
  const meta = [REVISION_1, REVISION_2, REVISION_3].find((r) => r.id === rev)!;
  return Promise.resolve(
    revisionDetail(meta, '# Old plan\n\n![chart](assets/chart.png)', opts.diff ? { diff: OLD_PLAN_DIFF } : {}),
  );
}

/** A row (no body) of the doc() fixture. */
function row(overrides: Partial<Doc> = {}): Doc {
  const { content: _content, last_write_conversation: _conv, assets: _assets, ...rest } = doc();
  void _content;
  void _conv;
  void _assets;
  return { ...rest, ...overrides };
}

function contentRow(overrides: Partial<DocContentResponse> = {}): DocContentResponse {
  return { ...row(), content: '# Old plan', changed: true, ...overrides };
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderHistory(d: DocDetail = doc()) {
  const handlers = {
    onClose: vi.fn(),
    onRestored: vi.fn(),
    onCopied: vi.fn(),
  };
  const ui = (value: DocDetail) => (
    <MemoryRouter initialEntries={['/docs/d1']}>
      <DocHistory doc={value} {...handlers} />
    </MemoryRouter>
  );
  const result = render(ui(d));
  return { ...result, handlers, rerenderDoc: (value: DocDetail) => result.rerender(ui(value)) };
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Entry names are "<prefix> (<full timestamp>)", e.g. "Version written 2h
 * ago (10/6/2026, 8:00:00 AM)"; match the locale-independent prefix.
 */
function entryName(prefix: string): RegExp {
  return new RegExp(`^${escapeRegExp(prefix)} \\(`);
}

function findEntry(prefix: string) {
  return screen.findByRole('button', { name: entryName(prefix) });
}

function getEntry(prefix: string) {
  return screen.getByRole('button', { name: entryName(prefix) });
}

const CURRENT = 'Current version, written 5m ago';
const V1 = 'Version written 2h ago';
const V2 = 'Version written 1d ago';
const V3 = 'Version written 2d ago';

function items(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('.doc-history-item')];
}

function detail(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('.doc-history-detail');
  if (!el) throw new Error('no detail pane');
  return el;
}

/** Select REVISION_1 and wait for its body. */
async function openRevision1(container: HTMLElement) {
  fireEvent.click(await findEntry(V1));
  await screen.findByRole('heading', { name: 'Old plan' });
  return detail(container);
}

function clickRestore(pane: HTMLElement) {
  fireEvent.click(within(pane).getByRole('button', { name: 'Restore this version' }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Restore' }));
}

function staleError(currentUpdatedAt: string): ApiClientError {
  return new ApiClientError('stale', 409, 'stale_update', undefined, {
    error: 'stale_update',
    message: 'stale',
    current: row({ updated_at: currentUpdatedAt }),
  });
}

describe('DocHistory', () => {
  beforeEach(() => {
    mocks.isMobile = false;
    mocks.fetchDoc.mockReset();
    mocks.fetchDocRevisions.mockReset().mockResolvedValue(revisions());
    mocks.fetchDocRevision.mockReset().mockImplementation(defaultRevisionFetch);
    mocks.restoreDocRevision.mockReset();
    mocks.copyDocRevision.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  describe('list', () => {
    it('shows the current version first, then the revisions newest first, with authors and sizes', async () => {
      const { container } = renderHistory();

      const current = await findEntry(CURRENT);
      expect(mocks.fetchDocRevisions).toHaveBeenCalledWith('d1');
      const entries = items(container);
      expect(entries).toHaveLength(4);

      // Current version: marked, selected, "by you".
      expect(within(entries[0]).getByText('Current')).toBeTruthy();
      expect(current.getAttribute('aria-current')).toBe('true');
      expect(entries[0].querySelector('.doc-history-item-meta')?.textContent).toBe('by you · 1.2 KB');

      // Revisions newest first; relative time with the full timestamp on hover.
      const whens = entries.map((li) => li.querySelector('.doc-history-item-when')!);
      expect(whens.map((el) => el.textContent)).toEqual(['5m ago', '2h ago', '1d ago', '2d ago']);
      const full = new Date(REVISION_1.written_at).toLocaleString();
      expect(whens[1].getAttribute('title')).toBe(full);
      expect(entries[1].querySelector('.doc-history-item-meta')?.textContent).toBe(
        'by Quarterly planning · 800 B',
      );
      expect(entries[2].querySelector('.doc-history-item-meta')?.textContent).toBe('by Notes chat · 2.0 KB');
      expect(entries[3].querySelector('.doc-history-item-meta')?.textContent).toBe('by Bea Smith · 100 B');
      expect(within(entries[1]).queryByText('Current')).toBeNull();

      // Unique names (with the full timestamp); the author line describes them.
      expect(getEntry(V1).textContent).toContain(`(${full})`);
      const describedBy = getEntry(V1).getAttribute('aria-describedby')!;
      expect(document.getElementById(describedBy)?.textContent).toBe('by Quarterly planning · 800 B');

      // Conversation authors link to the conversation (project or standalone).
      expect(within(entries[1]).getByRole('link', { name: 'Quarterly planning' }).getAttribute('href')).toBe(
        '/projects/p1/c9',
      );
      expect(within(entries[2]).getByRole('link', { name: 'Notes chat' }).getAttribute('href')).toBe('/chats/c7');
      expect(within(entries[3]).queryByRole('link')).toBeNull();

      expect(screen.getByText(RETENTION_TEXT)).toBeTruthy();
      expect(screen.queryByText('No earlier versions yet.')).toBeNull();
    });

    it('says when there are no earlier versions, with the retention note', async () => {
      mocks.fetchDocRevisions.mockResolvedValue(revisions([]));
      const { container } = renderHistory();

      expect(await screen.findByText('No earlier versions yet.')).toBeTruthy();
      expect(screen.getByText(RETENTION_TEXT)).toBeTruthy();
      expect(items(container)).toHaveLength(1);
    });

    it('shows a load error with a Retry that refetches', async () => {
      mocks.fetchDocRevisions.mockRejectedValueOnce(new ApiClientError('Server exploded', 500));
      renderHistory();

      expect(await screen.findByText('Server exploded')).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
      expect(await findEntry(CURRENT)).toBeTruthy();
      expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(2);
    });

    it('does not treat a docs_disabled 403 as lost access', async () => {
      mocks.fetchDocRevisions.mockRejectedValue(
        new ApiClientError('Docs are turned off.', 403, 'docs_disabled'),
      );
      renderHistory();

      expect(await screen.findByText('Docs are turned off.')).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
      expect(screen.queryByText(NO_ACCESS_TEXT)).toBeNull();
    });

    it('refetches the list when the doc gets a new updated_at', async () => {
      const { rerenderDoc } = renderHistory();
      await findEntry(CURRENT);
      expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(1);

      // Re-rendering with the same token fetches nothing.
      rerenderDoc(doc());
      expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(1);

      const newer = version({ id: '20261006T100500Z-1', written_at: ago(5 * MINUTE), replaced_at: ago(0) });
      mocks.fetchDocRevisions.mockResolvedValue({
        current: version({ written_at: ago(0) }),
        revisions: [newer, REVISION_1, REVISION_2, REVISION_3],
      });
      rerenderDoc(doc({ updated_at: NEWER_TOKEN, content: '# Newest plan' }));

      await waitFor(() => expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(2));
      expect(await findEntry('Version written 5m ago')).toBeTruthy();
      expect(await findEntry('Current version, written just now')).toBeTruthy();
      expect(screen.getByRole('heading', { name: 'Newest plan' })).toBeTruthy();
    });

    it('drops a stale list response', async () => {
      const slow = deferred<DocRevisionsResponse>();
      mocks.fetchDocRevisions
        .mockReturnValueOnce(slow.promise)
        .mockResolvedValueOnce(revisions([REVISION_1]));
      const { container, rerenderDoc } = renderHistory();

      rerenderDoc(doc({ updated_at: NEWER_TOKEN }));
      await findEntry(V1);
      expect(items(container)).toHaveLength(2);

      await act(async () => {
        slow.resolve(revisions([]));
      });
      expect(items(container)).toHaveLength(2);
      expect(screen.queryByText('No earlier versions yet.')).toBeNull();
    });

    it('keeps the list and offers Retry when a refresh fails', async () => {
      const { container, rerenderDoc } = renderHistory();
      await findEntry(CURRENT);

      mocks.fetchDocRevisions.mockRejectedValueOnce(new ApiClientError('Server exploded', 500));
      rerenderDoc(doc({ updated_at: NEWER_TOKEN }));

      const banner = (await screen.findByText("Couldn't refresh the versions: Server exploded"))
        .closest('.doc-history-notice') as HTMLElement;
      expect(items(container)).toHaveLength(4);
      fireEvent.click(within(banner).getByRole('button', { name: 'Retry' }));
      await waitFor(() =>
        expect(screen.queryByText("Couldn't refresh the versions: Server exploded")).toBeNull(),
      );
      expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(3);
    });

    it('reports a selected revision that drops out of a refreshed list', async () => {
      const { container, rerenderDoc } = renderHistory();
      await openRevision1(container);

      mocks.fetchDocRevisions.mockResolvedValue(revisions([REVISION_2, REVISION_3]));
      rerenderDoc(doc({ updated_at: NEWER_TOKEN }));

      expect((await screen.findByRole('alert')).textContent).toContain(GONE_TEXT);
      expect(within(detail(container)).getByRole('heading', { name: 'Current version' })).toBeTruthy();
      expect(getEntry(CURRENT).getAttribute('aria-current')).toBe('true');
    });
  });

  describe('detail', () => {
    it('shows the current body for the current version, without Restore or Copy', async () => {
      const { container } = renderHistory();
      await findEntry(CURRENT);

      const pane = detail(container);
      expect(within(pane).getByRole('heading', { name: 'Current version' })).toBeTruthy();
      expect(within(pane).getByRole('heading', { name: 'Current plan' })).toBeTruthy();
      expect(within(pane).getByRole('img', { name: 'chart' }).getAttribute('src')).toBe(
        '/app/api/docs/d1/assets/chart.png',
      );
      expect(within(pane).queryByRole('button', { name: /Restore/ })).toBeNull();
      expect(within(pane).queryByRole('button', { name: /Copy/ })).toBeNull();
      expect(within(pane).queryByRole('button', { name: /Show changes/ })).toBeNull();
      expect(mocks.fetchDocRevision).not.toHaveBeenCalled();
    });

    it('fetches and renders a selected revision with the current assets', async () => {
      const { container } = renderHistory();
      const pane = await openRevision1(container);

      expect(mocks.fetchDocRevision).toHaveBeenCalledWith('d1', REV_1);
      expect(
        within(pane).getByRole('heading', { level: 3 }).textContent?.startsWith('Version from '),
      ).toBe(true);
      expect(within(pane).getByRole('img', { name: 'chart' }).getAttribute('src')).toBe(
        '/app/api/docs/d1/assets/chart.png',
      );
      expect(within(pane).getByRole('link', { name: 'Quarterly planning' }).getAttribute('href')).toBe(
        '/projects/p1/c9',
      );
      expect(getEntry(V1).getAttribute('aria-current')).toBe('true');
      expect(getEntry(CURRENT).getAttribute('aria-current')).toBeNull();
      expect(within(pane).getByRole('button', { name: 'Restore this version' })).toBeTruthy();
      expect(within(pane).getByRole('button', { name: 'Copy to a new doc' })).toBeTruthy();

      // Back to the current version and to the revision: its body is cached.
      fireEvent.click(getEntry(CURRENT));
      expect(await screen.findByRole('heading', { name: 'Current plan' })).toBeTruthy();
      fireEvent.click(getEntry(V1));
      expect(await screen.findByRole('heading', { name: 'Old plan' })).toBeTruthy();
      expect(mocks.fetchDocRevision).toHaveBeenCalledTimes(1);
    });

    it('offers Restore and Copy only once the body has loaded', async () => {
      const body = deferred<DocRevisionDetail>();
      mocks.fetchDocRevision.mockReturnValueOnce(body.promise);
      const { container } = renderHistory();

      fireEvent.click(await findEntry(V1));
      const pane = detail(container);
      expect(within(pane).getByText('Loading this version...')).toBeTruthy();
      expect(within(pane).getByRole('button', { name: 'Show changes' })).toBeTruthy();
      expect(within(pane).queryByRole('button', { name: 'Restore this version' })).toBeNull();
      expect(within(pane).queryByRole('button', { name: 'Copy to a new doc' })).toBeNull();

      await act(async () => {
        body.resolve(revisionDetail(REVISION_1, '# Old plan'));
      });
      expect(within(pane).getByRole('button', { name: 'Restore this version' })).toBeTruthy();
      expect(within(pane).getByRole('button', { name: 'Copy to a new doc' })).toBeTruthy();
    });

    it('does not restart a body load when the doc is written meanwhile', async () => {
      const body = deferred<DocRevisionDetail>();
      mocks.fetchDocRevision.mockReturnValueOnce(body.promise);
      const { rerenderDoc } = renderHistory();

      fireEvent.click(await findEntry(V1));
      rerenderDoc(doc({ updated_at: NEWER_TOKEN }));
      await waitFor(() => expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(2));
      expect(mocks.fetchDocRevision).toHaveBeenCalledTimes(1);

      await act(async () => {
        body.resolve(revisionDetail(REVISION_1, '# Old plan'));
      });
      expect(screen.getByRole('heading', { name: 'Old plan' })).toBeTruthy();
      expect(mocks.fetchDocRevision).toHaveBeenCalledTimes(1);
    });

    it('toggles "Show changes" between the diff since the version and its body', async () => {
      const { container } = renderHistory();
      const pane = await openRevision1(container);

      const toggle = within(pane).getByRole('button', { name: 'Show changes' });
      expect(toggle.getAttribute('aria-pressed')).toBe('false');
      fireEvent.click(toggle);

      expect(await within(pane).findByText('Changes since this version')).toBeTruthy();
      expect(mocks.fetchDocRevision).toHaveBeenLastCalledWith('d1', REV_1, { diff: true });
      expect(toggle.getAttribute('aria-pressed')).toBe('true');
      const diff = pane.querySelector('.skill-diff') as HTMLElement;
      expect(diff).not.toBeNull();
      expect(diff.querySelector('.skill-diff-line.add .skill-diff-text')?.textContent).toBe('# Current plan');
      expect(diff.querySelector('.skill-diff-line.del .skill-diff-text')?.textContent).toBe('# Old plan');
      expect(within(pane).queryByRole('heading', { name: 'Old plan' })).toBeNull();

      fireEvent.click(toggle);
      expect(within(pane).getByRole('heading', { name: 'Old plan' })).toBeTruthy();
      expect(pane.querySelector('.skill-diff')).toBeNull();
      expect(mocks.fetchDocRevision).toHaveBeenCalledTimes(2);
    });

    it('refetches the diff when the doc changes while Show changes is on', async () => {
      const { container, rerenderDoc } = renderHistory();
      const pane = await openRevision1(container);
      fireEvent.click(within(pane).getByRole('button', { name: 'Show changes' }));
      await within(pane).findByText('Changes since this version');
      expect(mocks.fetchDocRevision).toHaveBeenCalledTimes(2);

      rerenderDoc(doc({ updated_at: NEWER_TOKEN }));
      await waitFor(() => expect(mocks.fetchDocRevision).toHaveBeenCalledTimes(3));
      expect(mocks.fetchDocRevision).toHaveBeenLastCalledWith('d1', REV_1, { diff: true });
      expect(await within(pane).findByText('Changes since this version')).toBeTruthy();
    });

    it('says so when the version matches the current one', async () => {
      mocks.fetchDocRevision.mockImplementation(async (_id: string, _rev: string, opts: { diff?: boolean } = {}) =>
        revisionDetail(REVISION_1, '# Old plan', opts.diff ? {
          diff: { added: 0, removed: 0, lines: [{ type: 'context', old_line: 1, new_line: 1, text: '# Old plan' }], truncated: false, total_old_lines: 1, total_new_lines: 1 },
        } : {}),
      );
      const { container } = renderHistory();
      const pane = await openRevision1(container);
      fireEvent.click(within(pane).getByRole('button', { name: 'Show changes' }));
      expect(
        await within(pane).findByText('No changes: the current version is the same as this one.'),
      ).toBeTruthy();
      expect(pane.querySelector('.skill-diff')).toBeNull();
    });

    it('caches a late body for an earlier selection without showing it', async () => {
      const first = deferred<DocRevisionDetail>();
      const second = deferred<DocRevisionDetail>();
      mocks.fetchDocRevision.mockImplementation((_id: string, rev: string) =>
        rev === REV_1 ? first.promise : second.promise,
      );
      const { container } = renderHistory();

      fireEvent.click(await findEntry(V1));
      fireEvent.click(getEntry(V2));
      await act(async () => {
        second.resolve(revisionDetail(REVISION_2, '# Second body'));
      });
      expect(screen.getByRole('heading', { name: 'Second body' })).toBeTruthy();

      await act(async () => {
        first.resolve(revisionDetail(REVISION_1, '# First body'));
      });
      expect(screen.getByRole('heading', { name: 'Second body' })).toBeTruthy();
      expect(screen.queryByRole('heading', { name: 'First body' })).toBeNull();
      expect(getEntry(V2).getAttribute('aria-current')).toBe('true');

      // The late body was immutable and is now cached: no refetch.
      fireEvent.click(getEntry(V1));
      expect(screen.getByRole('heading', { name: 'First body' })).toBeTruthy();
      expect(detail(container).textContent).not.toContain('Second body');
      expect(mocks.fetchDocRevision).toHaveBeenCalledTimes(2);
    });

    it('drops a late error for an earlier selection', async () => {
      const first = deferred<DocRevisionDetail>();
      mocks.fetchDocRevision.mockImplementation((_id: string, rev: string) =>
        rev === REV_1 ? first.promise : defaultRevisionFetch(_id, rev),
      );
      renderHistory();

      fireEvent.click(await findEntry(V1));
      fireEvent.click(getEntry(V2));
      await screen.findByRole('heading', { name: 'Old plan' });
      await act(async () => {
        first.reject(new ApiClientError('Revision not found.', 404, 'revision_not_found'));
      });
      expect(screen.queryByRole('alert')).toBeNull();
      expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(1);
    });

    it('shows a failed load with a Retry', async () => {
      mocks.fetchDocRevision.mockRejectedValueOnce(new ApiClientError('Server exploded', 500));
      const { container } = renderHistory();

      fireEvent.click(await findEntry(V1));
      const pane = detail(container);
      expect(await within(pane).findByText('Server exploded')).toBeTruthy();
      expect(within(pane).queryByRole('button', { name: 'Restore this version' })).toBeNull();
      expect(within(pane).queryByRole('button', { name: 'Copy to a new doc' })).toBeNull();

      fireEvent.click(within(pane).getByRole('button', { name: 'Retry' }));
      expect(await within(pane).findByRole('heading', { name: 'Old plan' })).toBeTruthy();
      expect(mocks.fetchDocRevision).toHaveBeenCalledTimes(2);
    });

    it('reports a revision that is gone and hides its actions', async () => {
      mocks.fetchDocRevision.mockRejectedValue(
        new ApiClientError('Revision not found.', 404, 'revision_not_found'),
      );
      const { container } = renderHistory();
      fireEvent.click(await findEntry(V1));

      expect((await screen.findByRole('alert')).textContent).toContain(GONE_TEXT);
      await waitFor(() => expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(2));
      // (This list still has it, so it stays selected, marked gone.)
      const pane = detail(container);
      expect(within(pane).getByText(GONE_TEXT)).toBeTruthy();
      expect(within(pane).queryByRole('button', { name: 'Retry' })).toBeNull();
      expect(within(pane).queryByRole('button', { name: 'Show changes' })).toBeNull();
      expect(within(pane).queryByRole('button', { name: 'Restore this version' })).toBeNull();
      expect(within(pane).queryByRole('button', { name: 'Copy to a new doc' })).toBeNull();
    });
  });

  describe('restore', () => {
    it('confirms, sends the doc updated_at as the token and hands the response over', async () => {
      const restored = contentRow({ updated_at: NEWER_TOKEN });
      mocks.restoreDocRevision.mockResolvedValue(restored);
      const { container, handlers } = renderHistory();
      const pane = await openRevision1(container);

      fireEvent.click(within(pane).getByRole('button', { name: 'Restore this version' }));
      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByText('Restore this version?')).toBeTruthy();
      expect(
        within(dialog).getByText('It replaces the current version, written 5m ago by you.'),
      ).toBeTruthy();
      expect(
        within(dialog).getByText('The current version is kept in History, so you can undo this.'),
      ).toBeTruthy();
      fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));

      await waitFor(() => expect(handlers.onRestored).toHaveBeenCalledWith(restored));
      expect(mocks.restoreDocRevision).toHaveBeenCalledWith('d1', REV_1, { expected_updated_at: TOKEN });
    });

    it('sends the token from when the confirm opened, even if the doc changed behind it', async () => {
      mocks.restoreDocRevision.mockRejectedValue(staleError(NEWER_TOKEN));
      mocks.fetchDoc.mockResolvedValue(doc({ updated_at: NEWER_TOKEN, content: '# Newer plan' }));
      const { container, rerenderDoc, handlers } = renderHistory();
      const pane = await openRevision1(container);

      fireEvent.click(within(pane).getByRole('button', { name: 'Restore this version' }));
      rerenderDoc(doc({ updated_at: NEWER_TOKEN, content: '# Newer plan' }));
      await waitFor(() => expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(2));
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Restore' }));

      expect((await screen.findByRole('alert')).textContent).toContain(STALE_TEXT);
      expect(mocks.restoreDocRevision).toHaveBeenCalledWith('d1', REV_1, { expected_updated_at: TOKEN });
      expect(handlers.onRestored).not.toHaveBeenCalled();
    });

    it('on a stale token: shows the re-fetched current body and retries with ITS token', async () => {
      mocks.restoreDocRevision.mockRejectedValueOnce(staleError(NEWER_TOKEN));
      // This tab missed the realtime event: the prop keeps TOKEN.
      mocks.fetchDoc.mockResolvedValue(doc({ updated_at: NEWER_TOKEN, content: '# Edited elsewhere' }));
      const { container, handlers, rerenderDoc } = renderHistory();
      const pane = await openRevision1(container);

      clickRestore(pane);

      expect((await screen.findByRole('alert')).textContent).toContain(STALE_TEXT);
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(mocks.fetchDoc).toHaveBeenCalledWith('d1');
      await waitFor(() => expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(2));
      expect(handlers.onRestored).not.toHaveBeenCalled();

      // The current version on screen is the re-fetched body...
      fireEvent.click(getEntry(CURRENT));
      expect(await screen.findByRole('heading', { name: 'Edited elsewhere' })).toBeTruthy();

      // ...and the retry sends its token.
      const restored = contentRow({ updated_at: NEWEST_TOKEN });
      mocks.restoreDocRevision.mockResolvedValueOnce(restored);
      fireEvent.click(getEntry(V1));
      clickRestore(await screen.findByRole('article', { name: 'Selected version' }));
      await waitFor(() => expect(handlers.onRestored).toHaveBeenCalledWith(restored));
      expect(mocks.restoreDocRevision).toHaveBeenLastCalledWith('d1', REV_1, {
        expected_updated_at: NEWER_TOKEN,
      });

      // The prop wins again once it has caught up.
      rerenderDoc(doc({ updated_at: NEWEST_TOKEN, content: '# Newest plan' }));
      fireEvent.click(getEntry(CURRENT));
      expect(await screen.findByRole('heading', { name: 'Newest plan' })).toBeTruthy();
    });

    it('on a stale token whose doc cannot be re-fetched: keeps the token of the body on screen', async () => {
      mocks.restoreDocRevision.mockRejectedValue(staleError(NEWER_TOKEN));
      mocks.fetchDoc.mockRejectedValue(new ApiClientError('Offline', 503));
      const { container } = renderHistory();
      const pane = await openRevision1(container);

      clickRestore(pane);
      expect((await screen.findByRole('alert')).textContent).toContain(STALE_TEXT);
      await waitFor(() => expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(2));

      clickRestore(detail(container));
      await waitFor(() => expect(mocks.restoreDocRevision).toHaveBeenCalledTimes(2));
      expect(mocks.restoreDocRevision).toHaveBeenLastCalledWith('d1', REV_1, { expected_updated_at: TOKEN });
    });

    it('on a revision that is gone: notice and a refreshed list without it', async () => {
      mocks.restoreDocRevision.mockRejectedValue(
        new ApiClientError('Revision not found.', 404, 'revision_not_found'),
      );
      const { container } = renderHistory();
      const pane = await openRevision1(container);

      mocks.fetchDocRevisions.mockResolvedValue(revisions([REVISION_2, REVISION_3]));
      clickRestore(pane);

      expect((await screen.findByRole('alert')).textContent).toContain(GONE_TEXT);
      expect(screen.queryByRole('dialog')).toBeNull();
      await waitFor(() => expect(items(container)).toHaveLength(3));
      expect(within(detail(container)).getByRole('heading', { name: 'Current version' })).toBeTruthy();
    });

    it('on a 403: the lost-access state with a way back to the doc', async () => {
      mocks.restoreDocRevision.mockRejectedValue(new ApiClientError('Forbidden.', 403, 'forbidden'));
      const { container, handlers } = renderHistory();
      const pane = await openRevision1(container);

      clickRestore(pane);

      expect((await screen.findByRole('alert')).textContent).toContain(NO_ACCESS_TEXT);
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(container.querySelector('.doc-history-layout')).toBeNull();
      fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Back to doc' }));
      expect(handlers.onClose).toHaveBeenCalledTimes(1);
    });

    it('keeps the dialog open with the message on another failure', async () => {
      mocks.restoreDocRevision.mockRejectedValue(new ApiClientError('Disk full.', 500, 'doc_storage_error'));
      const { container } = renderHistory();
      const pane = await openRevision1(container);

      clickRestore(pane);

      expect(await within(screen.getByRole('dialog')).findByText('Disk full.')).toBeTruthy();
    });
  });

  describe('lost edit access', () => {
    it('shows the lost-access state without fetching for a viewer who cannot edit', async () => {
      const { container, handlers } = renderHistory(doc({
        owner_id: 2,
        shared_with_me: true,
        permission: 'read',
        access: READ_ONLY_ACCESS,
      }));

      expect(screen.getByRole('alert').textContent).toContain(NO_ACCESS_TEXT);
      expect(mocks.fetchDocRevisions).not.toHaveBeenCalled();
      expect(container.querySelector('.doc-history-layout')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Restore this version' })).toBeNull();
      fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Back to doc' }));
      expect(handlers.onClose).toHaveBeenCalledTimes(1);
    });

    it('switches to the lost-access state when the doc loses can_edit, and drops open dialogs', async () => {
      const { container, rerenderDoc } = renderHistory();
      const pane = await openRevision1(container);
      fireEvent.click(within(pane).getByRole('button', { name: 'Copy to a new doc' }));
      expect(screen.getByRole('dialog')).toBeTruthy();

      rerenderDoc(doc({ access: READ_ONLY_ACCESS }));
      expect(screen.getByRole('alert').textContent).toContain(NO_ACCESS_TEXT);
      expect(container.querySelector('.doc-history-layout')).toBeNull();
      expect(screen.queryByRole('dialog')).toBeNull();

      // Access back: the view returns, the dialog does not.
      rerenderDoc(doc());
      expect(await findEntry(V1)).toBeTruthy();
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('treats a 403 from the list fetch as lost access', async () => {
      mocks.fetchDocRevisions.mockRejectedValue(new ApiClientError('Forbidden.', 403, 'forbidden'));
      renderHistory();
      expect((await screen.findByRole('alert')).textContent).toContain(NO_ACCESS_TEXT);
      expect(screen.queryByText('Forbidden.')).toBeNull();
    });

    it('treats a 403 from the version fetch as lost access', async () => {
      mocks.fetchDocRevision.mockRejectedValue(new ApiClientError('Forbidden.', 403, 'forbidden'));
      const { container } = renderHistory();
      fireEvent.click(await findEntry(V1));
      expect((await screen.findByRole('alert')).textContent).toContain(NO_ACCESS_TEXT);
      expect(container.querySelector('.doc-history-layout')).toBeNull();
    });

    it('treats a 403 from copy as lost access', async () => {
      mocks.copyDocRevision.mockRejectedValue(new ApiClientError('Forbidden.', 403, 'forbidden'));
      const { container, handlers } = renderHistory();
      const pane = await openRevision1(container);

      fireEvent.click(within(pane).getByRole('button', { name: 'Copy to a new doc' }));
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Copy' }));

      expect((await screen.findByRole('alert')).textContent).toContain(NO_ACCESS_TEXT);
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(handlers.onCopied).not.toHaveBeenCalled();
    });
  });

  describe('copy', () => {
    it('copies with the server default title when the field is left empty', async () => {
      const copied = row({ id: 'd2', title: 'Roadmap (copy)' });
      mocks.copyDocRevision.mockResolvedValue(copied);
      const { container, handlers } = renderHistory();
      const pane = await openRevision1(container);

      fireEvent.click(within(pane).getByRole('button', { name: 'Copy to a new doc' }));
      const dialog = screen.getByRole('dialog');
      const input = within(dialog).getByLabelText('Title') as HTMLInputElement;
      expect(input.placeholder).toBe('Leave empty for a default title');
      expect(document.activeElement).toBe(input);
      fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }));

      await waitFor(() => expect(handlers.onCopied).toHaveBeenCalledWith(copied));
      expect(mocks.copyDocRevision).toHaveBeenCalledWith('d1', REV_1, {});
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('sends a typed title (Enter submits)', async () => {
      const copied = row({ id: 'd2', title: 'Old roadmap' });
      mocks.copyDocRevision.mockResolvedValue(copied);
      const { container, handlers } = renderHistory();
      const pane = await openRevision1(container);

      fireEvent.click(within(pane).getByRole('button', { name: 'Copy to a new doc' }));
      const input = within(screen.getByRole('dialog')).getByLabelText('Title');
      fireEvent.change(input, { target: { value: '  Old roadmap  ' } });
      fireEvent.keyDown(input, { key: 'Enter' });

      await waitFor(() => expect(handlers.onCopied).toHaveBeenCalledWith(copied));
      expect(mocks.copyDocRevision).toHaveBeenCalledWith('d1', REV_1, { title: 'Old roadmap' });
    });

    it.each([
      ['duplicate_title', 409, 'A doc named "Roadmap" already exists.'],
      ['invalid_title', 400, 'Title must not contain control characters.'],
    ])('keeps the dialog open with the server message on %s', async (code, status, message) => {
      mocks.copyDocRevision.mockRejectedValue(new ApiClientError(message, status, code));
      const { container, handlers } = renderHistory();
      const pane = await openRevision1(container);

      fireEvent.click(within(pane).getByRole('button', { name: 'Copy to a new doc' }));
      const dialog = screen.getByRole('dialog');
      fireEvent.change(within(dialog).getByLabelText('Title'), { target: { value: 'Roadmap' } });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }));

      expect(await within(dialog).findByText(message)).toBeTruthy();
      expect(screen.getByRole('dialog')).toBe(dialog);
      expect((within(dialog).getByLabelText('Title') as HTMLInputElement).value).toBe('Roadmap');
      expect(handlers.onCopied).not.toHaveBeenCalled();
    });

    it('on a revision that is gone: closes with a notice and refreshes the list', async () => {
      mocks.copyDocRevision.mockRejectedValue(
        new ApiClientError('Revision not found.', 404, 'revision_not_found'),
      );
      const { container, handlers } = renderHistory();
      const pane = await openRevision1(container);

      fireEvent.click(within(pane).getByRole('button', { name: 'Copy to a new doc' }));
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Copy' }));

      expect((await screen.findByRole('alert')).textContent).toContain(GONE_TEXT);
      expect(screen.queryByRole('dialog')).toBeNull();
      await waitFor(() => expect(mocks.fetchDocRevisions).toHaveBeenCalledTimes(2));
      expect(handlers.onCopied).not.toHaveBeenCalled();
    });
  });

  describe('phone', () => {
    it('shows the list, opens a version full width, and goes back to All versions', async () => {
      mocks.isMobile = true;
      const { container } = renderHistory();

      await findEntry(CURRENT);
      expect(container.querySelector('.doc-history-detail')).toBeNull();

      fireEvent.click(getEntry(V1));
      expect(await screen.findByRole('heading', { name: 'Old plan' })).toBeTruthy();
      expect(container.querySelector('.doc-history-list')).toBeNull();
      const back = screen.getByRole('button', { name: 'All versions' });
      expect(document.activeElement).toBe(back);

      fireEvent.click(back);
      expect(container.querySelector('.doc-history-detail')).toBeNull();
      const entry = getEntry(V1);
      expect(entry.getAttribute('aria-current')).toBe('true');
      expect(document.activeElement).toBe(entry);
    });

    it('Escape in the detail goes back to the list first, then to the doc', async () => {
      mocks.isMobile = true;
      const { container, handlers } = renderHistory();

      fireEvent.click(await findEntry(V1));
      await screen.findByRole('heading', { name: 'Old plan' });
      fireEvent.keyDown(screen.getByRole('button', { name: 'All versions' }), { key: 'Escape' });
      expect(handlers.onClose).not.toHaveBeenCalled();
      expect(container.querySelector('.doc-history-detail')).toBeNull();

      fireEvent.keyDown(getEntry(V1), { key: 'Escape' });
      expect(handlers.onClose).toHaveBeenCalledTimes(1);
    });
  });

  describe('keyboard', () => {
    it('Escape inside History goes back to the doc, but not while a dialog is open', async () => {
      const { container, handlers } = renderHistory();
      const pane = await openRevision1(container);

      fireEvent.click(within(pane).getByRole('button', { name: 'Copy to a new doc' }));
      const input = within(screen.getByRole('dialog')).getByLabelText('Title');
      fireEvent.keyDown(input, { key: 'Escape' });
      expect(handlers.onClose).not.toHaveBeenCalled();
      expect(screen.queryByRole('dialog')).toBeNull();

      fireEvent.keyDown(getEntry(V1), { key: 'Escape' });
      expect(handlers.onClose).toHaveBeenCalledTimes(1);
    });

    it('Back to doc closes, and arrow / Home / End keys move between the entries', async () => {
      const { handlers } = renderHistory();
      const current = await findEntry(CURRENT);

      current.focus();
      fireEvent.keyDown(current, { key: 'ArrowDown' });
      expect(document.activeElement).toBe(getEntry(V1));
      fireEvent.keyDown(document.activeElement!, { key: 'End' });
      expect(document.activeElement).toBe(getEntry(V3));
      fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
      expect(document.activeElement).toBe(getEntry(V2));
      fireEvent.keyDown(document.activeElement!, { key: 'Home' });
      expect(document.activeElement).toBe(getEntry(CURRENT));

      fireEvent.click(screen.getByRole('button', { name: 'Back to doc' }));
      expect(handlers.onClose).toHaveBeenCalledTimes(1);
    });
  });
});
