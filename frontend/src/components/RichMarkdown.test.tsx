// RichMarkdown and the lazy code-and-math chunk (hooks/useCodeAndMath.ts):
// markdown without a fence or `$` never requests the chunk; markdown with
// them renders plain first, then highlighted / typeset once it lands.
//
// The chunk is module state shared by the whole file, so the tests run in
// order: the first one runs before anything has requested it.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { RichMarkdown, needsCodeAndMath } from './RichMarkdown';

const mocks = vi.hoisted(() => ({ chunkImports: 0 }));

// The real chunk, counting how often it is requested.
vi.mock('./codeAndMath', async (importOriginal) => {
  mocks.chunkImports += 1;
  return importOriginal();
});

describe('needsCodeAndMath', () => {
  it.each([
    ['plain **markdown** with `inline code`', false],
    ['    indented code (no language, never highlighted)', false],
    ['```python\nprint(1)\n```', true],
    ['~~~\nfenced\n~~~', true],
    ['costs $5', true],
    ['$$x^2$$', true],
  ])('%j -> %s', (markdown, expected) => {
    expect(needsCodeAndMath(markdown)).toBe(expected);
  });

  it('ignores fences when highlighting is off', () => {
    expect(needsCodeAndMath('```python\nprint(1)\n```', false)).toBe(false);
    expect(needsCodeAndMath('```python\nprint(1)\n```\n$x$', false)).toBe(true);
  });
});

describe('RichMarkdown', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders markdown without a fence or $ and never requests the chunk', async () => {
    render(<RichMarkdown>{'**Bold** and `code`'}</RichMarkdown>);
    expect(screen.getByText('Bold').tagName).toBe('STRONG');
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(mocks.chunkImports).toBe(0);
  });

  it('renders fenced code and math plain first, then highlighted and typeset', async () => {
    const { container } = render(
      <RichMarkdown>{'```python\nimport os\n```\n\nArea: $\\pi r^2$'}</RichMarkdown>,
    );
    expect(container.querySelector('pre code')?.textContent).toBe('import os\n');
    expect(container.querySelector('.hljs-keyword')).toBeNull();
    expect(container.querySelector('.katex')).toBeNull();

    await waitFor(
      () => expect(container.querySelector('.hljs-keyword')?.textContent).toBe('import'),
      { timeout: 10_000 },
    );
    expect(container.querySelector('.katex')).not.toBeNull();
    expect(mocks.chunkImports).toBe(1);
  });

  it('typesets math but leaves code plain with highlight={false}', () => {
    const { container } = render(
      <RichMarkdown highlight={false}>{'```python\nimport os\n```\n\n$x^2$'}</RichMarkdown>,
    );
    // The chunk is already in: the first render has it.
    expect(container.querySelector('.katex')).not.toBeNull();
    expect(container.querySelector('.hljs-keyword')).toBeNull();
    expect(mocks.chunkImports).toBe(1);
  });
});
