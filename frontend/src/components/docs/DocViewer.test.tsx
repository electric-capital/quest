// DocViewer: the loading / not-found / error states, the rendered body with
// doc asset resolution and the Show source toggle, and the footer's
// "Last written by" subject per `last_write_source`.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { DocDetail } from '../../api/types';
import type { DocView } from '../../hooks/useDoc';
import { DocViewer } from './DocViewer';

const mocks = vi.hoisted(() => ({
  view: null as unknown as DocView,
}));

vi.mock('../../hooks/useDoc', () => ({
  useDoc: () => mocks.view,
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
    last_write_source: 'ui',
    created_at: '2026-10-01T00:00:00',
    updated_at: new Date(Date.now() - 5 * 60 * 1000).toISOString().replace('Z', ''),
    scope: 'user',
    shared: false,
    access: { can_rename: true, can_switch_mode: true, can_delete: true, write: 'free' },
    shares: [],
    content: '# Plan\n\n![chart](assets/chart.png)',
    last_write_conversation: null,
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
    ...view,
  };
}

function renderViewer() {
  return render(
    <MemoryRouter initialEntries={['/docs/d1']}>
      <DocViewer docId="d1" />
    </MemoryRouter>,
  );
}

function footerText(container: HTMLElement): string {
  return container.querySelector('.doc-viewer-footer')?.textContent ?? '';
}

describe('DocViewer', () => {
  beforeEach(() => {
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

  it('shows the empty-doc line for blank content', () => {
    setView({ doc: doc({ content: '  \n' }) });
    renderViewer();
    expect(screen.getByText('This doc is empty. Ask Quest to add to it.')).toBeTruthy();
  });

  describe('footer', () => {
    it('names "you" for a UI write, then the relative update time', () => {
      setView({ doc: doc({ last_write_source: 'ui' }) });
      const { container } = renderViewer();
      expect(footerText(container)).toBe('Last written by you · Updated 5m ago');
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
});
