// The shared markdown `img` renderer (MarkdownImage via markdownComponents):
// Quest Doc asset resolution through MarkdownWorkspaceContext.assetBase, and
// the unchanged conversation-workspace behaviour without it; links and the
// chat:// / proj:// / autoproject:// space references at the end.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import ReactMarkdown from 'react-markdown';
import { API_BASE_URL } from '../api/config';
import { RichMarkdown } from './RichMarkdown';
import {
  MarkdownWorkspaceContext,
  markdownComponents,
  type MarkdownWorkspaceContextValue,
} from './Message';

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
// hrefs resolve to the asset route by the image rule and everything else is
// left alone; outside a doc a relative href is a workspace path, resolved to
// the files/download URL and opened through `onOpenFile` on a plain click.
// Every link opens in a new tab.
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

  it('resolves relative hrefs against the conversation files route outside a doc', () => {
    renderMarkdown(
      '[file](assets/chart.png) [note](./note.txt) [sp](reports/q3%20final.pdf) [ext](https://x/y) [anchor](#top)',
      { conversationId: 'c1' },
    );
    const href = (name: string) => screen.getByRole('link', { name }).getAttribute('href');
    const download = (path: string) =>
      `${API_BASE_URL}/conversations/c1/files/download?path=${encodeURIComponent(path)}`;
    expect(href('file')).toBe(download('assets/chart.png'));
    expect(href('note')).toBe(download('note.txt'));
    expect(href('sp')).toBe(download('reports/q3 final.pdf'));
    expect(href('ext')).toBe('https://x/y');
    expect(href('anchor')).toBe('#top');
    expect(screen.getByRole('link', { name: 'file' }).getAttribute('target')).toBe('_blank');
  });

  it('resolves relative hrefs against the project files route when only projectId is set', () => {
    renderMarkdown('[r](out/report.md)', { projectId: 'p1' });
    expect(screen.getByRole('link', { name: 'r' }).getAttribute('href')).toBe(
      `${API_BASE_URL}/projects/p1/files/download?path=${encodeURIComponent('out/report.md')}`,
    );
  });

  it('leaves relative hrefs as written with no workspace to resolve against', () => {
    renderMarkdown('[n](note.txt)', {});
    expect(screen.getByRole('link', { name: 'n' }).getAttribute('href')).toBe('note.txt');
  });

  it('opens a workspace link through onOpenFile on a plain click only', () => {
    const onOpenFile = vi.fn();
    renderMarkdown('[note](sub/note.txt) [ext](https://x/y)', { conversationId: 'c1', onOpenFile });
    const note = screen.getByRole('link', { name: 'note' });

    const plain = fireEvent.click(note);
    expect(plain).toBe(false); // default prevented: no new tab
    expect(onOpenFile).toHaveBeenCalledWith('sub/note.txt', 'note.txt', { kind: 'conversation', id: 'c1' });

    onOpenFile.mockClear();
    expect(fireEvent.click(note, { ctrlKey: true })).toBe(true);
    expect(fireEvent.click(note, { metaKey: true })).toBe(true);
    expect(fireEvent.click(screen.getByRole('link', { name: 'ext' }))).toBe(true);
    expect(onOpenFile).not.toHaveBeenCalled();
  });
});

// Space references through the full renderer (RichMarkdown keeps them past
// react-markdown's URL sanitiser): each scheme resolves against its own id
// in MarkdownWorkspaceContext, and one whose id the context lacks stays
// unresolved (link left as written, image shown as a missing ref).
describe('workspace references', () => {
  afterEach(() => {
    cleanup();
  });

  function renderRich(markdown: string, ctx: MarkdownWorkspaceContextValue) {
    return render(
      <MarkdownWorkspaceContext.Provider value={ctx}>
        <RichMarkdown components={markdownComponents}>{markdown}</RichMarkdown>
      </MarkdownWorkspaceContext.Provider>,
    );
  }

  const download = (segment: string, id: string, path: string) =>
    `${API_BASE_URL}/${segment}/${id}/files/download?path=${encodeURIComponent(path)}`;

  it('resolves each scheme against its own space in a run of an AutoProject', () => {
    renderRich(
      '[a](autoproject://reports/q3%20final.md) [c](chat://out.csv) [b](notes.md) [p](proj://x.md)',
      { conversationId: 'c1', autoprojectId: 'ap1' },
    );
    const href = (name: string) => screen.getByRole('link', { name }).getAttribute('href');
    expect(href('a')).toBe(download('autoprojects', 'ap1', 'reports/q3 final.md'));
    expect(href('c')).toBe(download('conversations', 'c1', 'out.csv'));
    // A bare path is the conversation's.
    expect(href('b')).toBe(download('conversations', 'c1', 'notes.md'));
    // No project in an AutoProject run: proj:// stays as written.
    expect(href('p')).toBe('proj://x.md');
  });

  it('resolves a bare path against the AutoProject when it is the only space', () => {
    renderRich('[n](sub/n.md)', { autoprojectId: 'ap1' });
    expect(screen.getByRole('link', { name: 'n' }).getAttribute('href')).toBe(
      download('autoprojects', 'ap1', 'sub/n.md'),
    );
  });

  it('leaves autoproject:// unresolved outside an AutoProject and never opens it', () => {
    const onOpenFile = vi.fn();
    const { container } = renderRich(
      '[a](autoproject://notes.md) ![img](autoproject://chart.png)',
      { conversationId: 'c1', onOpenFile },
    );
    const link = screen.getByRole('link', { name: 'a' });
    expect(link.getAttribute('href')).toBe('autoproject://notes.md');
    fireEvent.click(link);
    expect(onOpenFile).not.toHaveBeenCalled();
    expect(screen.queryByRole('img')).toBeNull();
    expect(container.querySelector('code.markdown-image-missing')?.getAttribute('title')).toBe(
      'autoproject://chart.png',
    );
  });

  it('opens an autoproject:// link and image in the AutoProject space', () => {
    const onOpenFile = vi.fn();
    renderRich('[a](autoproject://d/notes.md) ![chart](autoproject://chart.png)', {
      conversationId: 'c1',
      autoprojectId: 'ap1',
      onOpenFile,
    });
    fireEvent.click(screen.getByRole('link', { name: 'a' }));
    expect(onOpenFile).toHaveBeenCalledWith('d/notes.md', 'notes.md', { kind: 'autoproject', id: 'ap1' });
    expect(screen.getByRole('img', { name: 'chart' }).getAttribute('src')).toBe(
      download('autoprojects', 'ap1', 'chart.png'),
    );
    fireEvent.click(screen.getByTitle('Open chart.png'));
    expect(onOpenFile).toHaveBeenLastCalledWith('chart.png', 'chart.png', { kind: 'autoproject', id: 'ap1' });
  });

  it('shows a space reference as a missing ref inside a doc', () => {
    const { container } = renderRich('![x](autoproject://a.png)', { ...DOC_CTX, autoprojectId: 'ap1' });
    expect(screen.queryByRole('img')).toBeNull();
    expect(container.querySelector('code.markdown-image-missing')).not.toBeNull();
  });

  it('still blanks unsafe protocols', () => {
    renderRich('[x](javascript:alert(1))', { conversationId: 'c1' });
    expect(screen.getByText('x').closest('a')?.getAttribute('href') ?? '').not.toContain('javascript');
  });
});
