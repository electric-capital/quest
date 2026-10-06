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
