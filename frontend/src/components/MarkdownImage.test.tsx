// The shared markdown `img` renderer (MarkdownImage via markdownComponents):
// Quest Doc asset resolution through MarkdownWorkspaceContext.assetBase, and
// the unchanged conversation-workspace behaviour without it.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import ReactMarkdown from 'react-markdown';
import { API_BASE_URL } from '../api/config';
import {
  MarkdownWorkspaceContext,
  markdownComponents,
  type MarkdownWorkspaceContextValue,
} from './Message';

// Message.tsx reaches pdfjs-dist through its card imports (FileViewerModal);
// pdf.js needs browser canvas APIs jsdom lacks.
vi.mock('./PdfViewer', () => ({ PdfViewer: () => null }));

function renderMarkdown(markdown: string, ctx: MarkdownWorkspaceContextValue) {
  return render(
    <MarkdownWorkspaceContext.Provider value={ctx}>
      <ReactMarkdown components={markdownComponents}>{markdown}</ReactMarkdown>
    </MarkdownWorkspaceContext.Provider>,
  );
}

const DOC_CTX = { assetBase: '/app/api/docs/d1/assets' };

describe('MarkdownImage', () => {
  afterEach(() => {
    cleanup();
  });

  describe('with a doc assetBase', () => {
    it('resolves assets/<name> to the doc asset route', () => {
      renderMarkdown('![a](assets/chart.png)', DOC_CTX);
      const img = screen.getByRole('img', { name: 'a' });
      expect(img.getAttribute('src')).toBe('/app/api/docs/d1/assets/chart.png');
    });

    it('decodes then re-encodes the asset name and tolerates a leading ./', () => {
      renderMarkdown('![sp](./assets/my%20chart.png)', DOC_CTX);
      const img = screen.getByRole('img', { name: 'sp' });
      expect(img.getAttribute('src')).toBe('/app/api/docs/d1/assets/my%20chart.png');
    });

    it('renders any other relative path as the missing chip', () => {
      const { container } = renderMarkdown('![b](other/x.png)', DOC_CTX);
      expect(screen.queryByRole('img')).toBeNull();
      const chip = container.querySelector('code.markdown-image-missing');
      expect(chip?.textContent).toBe('b');
      expect(chip?.getAttribute('title')).toBe('other/x.png');
    });

    it('does not resolve nested or dot-segment asset paths', () => {
      const { container } = renderMarkdown(
        '![n](assets/sub/x.png) ![d](assets/..)',
        DOC_CTX,
      );
      expect(screen.queryByRole('img')).toBeNull();
      expect(container.querySelectorAll('code.markdown-image-missing')).toHaveLength(2);
    });

    it('never auto-fetches an external src: it becomes a link', () => {
      renderMarkdown('![c](https://x/y.png)', DOC_CTX);
      expect(screen.queryByRole('img')).toBeNull();
      const link = screen.getByRole('link', { name: 'c' });
      expect(link.getAttribute('href')).toBe('https://x/y.png');
      expect(link.getAttribute('target')).toBe('_blank');
    });

    it('ignores a conversationId in the same context', () => {
      renderMarkdown('![w](chart.png)', { ...DOC_CTX, conversationId: 'c1' });
      expect(screen.queryByRole('img')).toBeNull();
    });
  });

  describe('workspace space selection', () => {
    it('resolves bare paths against the conversation workspace', () => {
      renderMarkdown('![c](charts/a%20b.png)', { conversationId: 'c1' });
      expect(screen.getByRole('img', { name: 'c' }).getAttribute('src'))
        .toBe('/app/api/conversations/c1/files/download?path=charts%2Fa%20b.png');
    });

    it('resolves bare paths against a project workspace with projectId alone', () => {
      renderMarkdown('![p](charts/a.png)', { projectId: 'p1' });
      expect(screen.getByRole('img', { name: 'p' }).getAttribute('src'))
        .toBe('/app/api/projects/p1/files/download?path=charts%2Fa.png');
    });

    it('prefers the conversation workspace when both ids are set', () => {
      renderMarkdown('![b](a.png)', { conversationId: 'c1', projectId: 'p1' });
      expect(screen.getByRole('img', { name: 'b' }).getAttribute('src'))
        .toBe('/app/api/conversations/c1/files/download?path=a.png');
    });
  });

  describe('without an assetBase (conversation workspace)', () => {
    it('resolves a workspace path against the conversation files route', () => {
      renderMarkdown('![w](./workspace/plots/chart.png)', { conversationId: 'c1' });
      const img = screen.getByRole('img', { name: 'w' });
      expect(img.getAttribute('src')).toBe(
        `${API_BASE_URL}/conversations/c1/files/download?path=${encodeURIComponent('plots/chart.png')}`,
      );
    });

    it('resolves assets/<name> as an ordinary workspace path', () => {
      renderMarkdown('![a](assets/chart.png)', { conversationId: 'c1' });
      const img = screen.getByRole('img', { name: 'a' });
      expect(img.getAttribute('src')).toBe(
        `${API_BASE_URL}/conversations/c1/files/download?path=${encodeURIComponent('assets/chart.png')}`,
      );
    });

    it('renders the missing chip with no conversation', () => {
      const { container } = renderMarkdown('![x](chart.png)', {});
      expect(screen.queryByRole('img')).toBeNull();
      expect(container.querySelector('code.markdown-image-missing')?.textContent).toBe('x');
    });

    it('still turns external srcs into links', () => {
      renderMarkdown('![c](https://x/y.png)', { conversationId: 'c1' });
      expect(screen.getByRole('link', { name: 'c' }).getAttribute('href')).toBe('https://x/y.png');
    });
  });
});

// The shared `a` renderer (MarkdownLink): inside a doc, `assets/<name>`
// hrefs resolve to the asset route by the image rule; everything else is
// left alone, and every link opens in a new tab.
describe('MarkdownLink', () => {
  afterEach(() => {
    cleanup();
  });

  it('rewrites assets/<name> hrefs inside a doc and keeps other hrefs', () => {
    renderMarkdown(
      [
        '[file](./assets/q3%20report.pdf)',
        '[nested](assets/sub/x.png)',
        '[other](notes/a.md)',
        '[ext](https://example.com/assets/x.png)',
        '[anchor](#intro)',
      ].join(' '),
      DOC_CTX,
    );
    const href = (name: string) => screen.getByRole('link', { name }).getAttribute('href');
    expect(href('file')).toBe('/app/api/docs/d1/assets/q3%20report.pdf');
    expect(href('nested')).toBe('assets/sub/x.png');
    expect(href('other')).toBe('notes/a.md');
    expect(href('ext')).toBe('https://example.com/assets/x.png');
    expect(href('anchor')).toBe('#intro');
    const file = screen.getByRole('link', { name: 'file' });
    expect(file.getAttribute('target')).toBe('_blank');
    expect(file.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('leaves assets/<name> hrefs alone outside a doc', () => {
    renderMarkdown('[file](assets/chart.png)', { conversationId: 'c1' });
    const link = screen.getByRole('link', { name: 'file' });
    expect(link.getAttribute('href')).toBe('assets/chart.png');
    expect(link.getAttribute('target')).toBe('_blank');
  });
});
