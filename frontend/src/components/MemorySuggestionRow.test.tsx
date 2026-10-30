// MemorySuggestionRow's doc kind: the Instructions / Memory pill and the doc
// link (or "Unavailable doc", or neither with showDocLink={false}), the
// instructions diff from the current text with its labels and the moved
// notice, the doc-memory replacement diff and orphan pill, the cap counter
// (code points of the trimmed draft) that disables Accept over the cap, and
// the textarea labels. A personal row keeps none of the doc additions. The
// row is presentational, so a small harness owns the draft.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { MemoryRouter } from 'react-router-dom';
import type { DocNoteSuggestion, MemorySuggestion } from '../api/types';
import { MemorySuggestionRow } from './MemorySuggestionRow';

const docNote = (extra: Partial<DocNoteSuggestion> = {}): DocNoteSuggestion => ({
  id: 'dn1',
  user_id: 1,
  doc_id: 'doc-1',
  conversation_id: 'c1',
  note_kind: 'instructions',
  content: 'Prices are in EUR.',
  reasoning: null,
  base_instructions_version: 3,
  replaces_memory_id: null,
  status: 'pending',
  memory_id: null,
  created_at: '2026-10-29T00:00:00Z',
  resolved_at: null,
  doc_title: 'Quarterly plan',
  replaces_content: null,
  current_instructions: 'Prices are in USD.',
  current_instructions_version: 3,
  instructions_moved: false,
  ...extra,
});

const memoryRow = (extra: Partial<DocNoteSuggestion> = {}): DocNoteSuggestion => docNote({
  note_kind: 'memory',
  content: 'The board meets weekly.',
  base_instructions_version: null,
  current_instructions: null,
  current_instructions_version: null,
  ...extra,
});

interface HarnessProps {
  suggestion: DocNoteSuggestion;
  draft?: string;
  showDocLink?: boolean;
  onOpenDoc?: () => void;
  onAccept?: () => void;
}

function DocRowHarness({ suggestion, draft, showDocLink, onOpenDoc, onAccept }: HarnessProps) {
  const [text, setText] = useState(draft ?? suggestion.content);
  return (
    <MemorySuggestionRow
      kind="doc"
      suggestion={suggestion}
      draft={text}
      onDraftChange={setText}
      busy={false}
      onAccept={onAccept ?? (() => {})}
      onDismiss={() => {}}
      showDocLink={showDocLink}
      onOpenDoc={onOpenDoc}
    />
  );
}

function renderDocRow(props: HarnessProps) {
  return render(
    <MemoryRouter>
      <DocRowHarness {...props} />
    </MemoryRouter>,
  );
}

function diffBody(): Element {
  const diffs = screen.getAllByTestId('memory-suggestion-diff');
  expect(diffs).toHaveLength(1);
  return diffs[0].querySelector('.memory-suggestion-diff-body')!;
}

afterEach(cleanup);

describe('MemorySuggestionRow kind="doc": instructions row', () => {
  it('shows the pill, the doc link and the diff from the current instructions', () => {
    const { container } = renderDocRow({ suggestion: docNote() });
    expect(container.querySelector('.memory-suggestion-row--doc')).not.toBeNull();
    expect(container.querySelector('.memory-suggestion-pill')!.textContent).toBe('Instructions');
    const link = screen.getByRole('link', { name: 'Quarterly plan' });
    expect(link.getAttribute('href')).toBe('/docs/doc-1');
    expect(link.className).toBe('memory-suggestion-doc-link');

    const diff = screen.getByTestId('memory-suggestion-diff');
    expect(diff.textContent).toContain('Changes to the instructions');
    const body = diffBody();
    expect(body.textContent).toBe('Prices are in USD. EUR.');
    expect(body.querySelector('.memory-suggestion-diff-del')!.textContent).toBe('USD.');
    expect(body.querySelector('.memory-suggestion-diff-add')!.textContent).toBe('EUR.');
    expect(container.querySelector('.memory-suggestion-moved')).toBeNull();
  });

  it('re-diffs as the draft changes', () => {
    renderDocRow({ suggestion: docNote() });
    const textarea = screen.getByLabelText('Doc instructions text');
    fireEvent.change(textarea, { target: { value: 'Prices are in GBP.' } });
    const body = diffBody();
    expect(body.querySelector('.memory-suggestion-diff-add')!.textContent).toBe('GBP.');
    fireEvent.change(textarea, { target: { value: 'Prices are in USD. ' } });
    expect(body.querySelector('.memory-suggestion-diff-del')).toBeNull();
    expect(body.querySelector('.memory-suggestion-diff-add')).toBeNull();
    expect(screen.getByTestId('memory-suggestion-diff').textContent).toContain('(same text)');
  });

  it('labels new instructions when the doc has none', () => {
    renderDocRow({ suggestion: docNote({ current_instructions: '' }) });
    expect(screen.getByTestId('memory-suggestion-diff').textContent)
      .toContain('New instructions (the doc has none)');
    expect(diffBody().querySelector('.memory-suggestion-diff-add')!.textContent)
      .toBe('Prices are in EUR.');
  });

  it('shows no diff when the current instructions are unknown', () => {
    renderDocRow({ suggestion: docNote({ current_instructions: null }) });
    expect(screen.queryAllByTestId('memory-suggestion-diff')).toHaveLength(0);
  });

  it('says when the instructions changed since the suggestion, and only then', () => {
    const { container, unmount } = renderDocRow({ suggestion: docNote({ instructions_moved: false }) });
    expect(container.querySelector('.memory-suggestion-moved')).toBeNull();
    unmount();

    const moved = renderDocRow({ suggestion: docNote({ instructions_moved: true }) });
    const notice = moved.container.querySelector('.memory-suggestion-moved')!;
    expect(notice.textContent).toBe('The instructions changed since this was suggested');
    expect(notice.querySelector('svg')!.getAttribute('aria-hidden')).toBe('true');
  });
});

describe('MemorySuggestionRow kind="doc": memory row', () => {
  it('shows the pill and the diff from the doc memory it replaces', () => {
    const { container } = renderDocRow({
      suggestion: memoryRow({ replaces_memory_id: 'm1', replaces_content: 'The board meets monthly.' }),
    });
    expect(container.querySelector('.memory-suggestion-pill')!.textContent).toBe('Memory');
    expect(screen.getByLabelText('Doc memory text')).not.toBeNull();
    const diff = screen.getByTestId('memory-suggestion-diff');
    expect(diff.textContent).toContain('Replaces an existing doc memory');
    expect(diff.textContent).not.toContain('Changes to the instructions');
    expect(diffBody().querySelector('.memory-suggestion-diff-del')!.textContent).toBe('monthly.');
    expect(diffBody().querySelector('.memory-suggestion-diff-add')!.textContent).toBe('weekly.');
    expect(container.querySelector('.memory-suggestion-replaces')).toBeNull();
  });

  it('keeps the plain pill when the replaced memory is gone', () => {
    const { container } = renderDocRow({
      suggestion: memoryRow({ replaces_memory_id: 'm-gone', replaces_content: null }),
    });
    expect(screen.queryAllByTestId('memory-suggestion-diff')).toHaveLength(0);
    const pill = container.querySelector('.memory-suggestion-replaces')!;
    expect(pill.textContent).toBe('Replaces an existing doc memory');
    expect(pill.getAttribute('title')).toBe('Accepting overwrites the doc memory this one updates');
  });

  it('shows no diff for a new memory', () => {
    const { container } = renderDocRow({ suggestion: memoryRow() });
    expect(screen.queryAllByTestId('memory-suggestion-diff')).toHaveLength(0);
    expect(container.querySelector('.memory-suggestion-replaces')).toBeNull();
    expect(container.querySelector('.memory-suggestion-moved')).toBeNull();
  });
});

describe('MemorySuggestionRow kind="doc": doc link', () => {
  it('names an unavailable doc without a link', () => {
    const { container } = renderDocRow({ suggestion: docNote({ doc_title: '' }) });
    expect(screen.queryByRole('link')).toBeNull();
    const label = container.querySelector('.memory-suggestion-doc-unavailable')!;
    expect(label.textContent).toBe('Unavailable doc');
    expect(label.getAttribute('title'))
      .toBe('This doc was deleted or you no longer have access to it');
  });

  it('shows neither link nor label with showDocLink={false}', () => {
    for (const docTitle of ['Quarterly plan', '']) {
      const { container, unmount } = renderDocRow({
        suggestion: docNote({ doc_title: docTitle }),
        showDocLink: false,
      });
      expect(screen.queryByRole('link')).toBeNull();
      expect(screen.queryByText('Unavailable doc')).toBeNull();
      expect(container.querySelector('.memory-suggestion-pill')!.textContent).toBe('Instructions');
      unmount();
    }
  });

  it('calls onOpenDoc on a plain click of the link, not on a modified one', () => {
    const onOpenDoc = vi.fn();
    renderDocRow({ suggestion: docNote(), onOpenDoc });
    const link = screen.getByRole('link', { name: 'Quarterly plan' });
    // jsdom cannot open the new tab a modified click asks the browser for.
    link.addEventListener('click', (e) => {
      if (e.ctrlKey) e.preventDefault();
    });
    fireEvent.click(link, { ctrlKey: true });
    expect(onOpenDoc).not.toHaveBeenCalled();
    fireEvent.click(link);
    expect(onOpenDoc).toHaveBeenCalledTimes(1);
  });
});

describe('MemorySuggestionRow kind="doc": cap counter', () => {
  it('shows n / max for each note kind', () => {
    const { container, unmount } = renderDocRow({ suggestion: docNote() });
    expect(container.querySelector('.memory-suggestion-counter')!.textContent).toBe('18 / 4,000');
    unmount();

    const memory = renderDocRow({ suggestion: memoryRow() });
    expect(memory.container.querySelector('.memory-suggestion-counter')!.textContent)
      .toBe('23 / 1,000');
  });

  it('marks the text invalid and disables Accept over the cap', () => {
    const onAccept = vi.fn();
    const { container } = renderDocRow({ suggestion: memoryRow(), onAccept });
    const textarea = screen.getByLabelText('Doc memory text');
    const counter = container.querySelector('.memory-suggestion-counter')!;
    expect(textarea.getAttribute('aria-invalid')).toBeNull();
    expect(textarea.getAttribute('aria-describedby')).toBe(counter.id);
    expect(container.querySelector('.memory-suggestion-over-cap')).toBeNull();

    fireEvent.change(textarea, { target: { value: 'x'.repeat(1001) } });
    expect(counter.textContent).toBe('1,001 / 1,000');
    expect(counter.classList.contains('memory-suggestion-counter--over')).toBe(true);
    const overCap = container.querySelector('.memory-suggestion-over-cap')!;
    expect(overCap.textContent).toBe('Use at most 1,000 characters.');
    expect(textarea.getAttribute('aria-invalid')).toBe('true');
    expect(textarea.getAttribute('aria-describedby')).toBe(`${counter.id} ${overCap.id}`);
    const accept = screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement;
    expect(accept.disabled).toBe(true);
    fireEvent.click(accept);
    expect(onAccept).not.toHaveBeenCalled();

    // Back at the cap (surrounding whitespace does not count).
    fireEvent.change(textarea, { target: { value: ` ${'x'.repeat(1000)}\n` } });
    expect(counter.textContent).toBe('1,000 / 1,000');
    expect(textarea.getAttribute('aria-invalid')).toBeNull();
    expect(accept.disabled).toBe(false);
  });

  it('counts code points, so an emoji is one character', () => {
    const { container } = renderDocRow({ suggestion: memoryRow(), draft: '😀'.repeat(1000) });
    const counter = container.querySelector('.memory-suggestion-counter')!;
    expect(counter.textContent).toBe('1,000 / 1,000');
    expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.change(screen.getByLabelText('Doc memory text'), {
      target: { value: '😀'.repeat(1001) },
    });
    expect(counter.textContent).toBe('1,001 / 1,000');
    expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('MemorySuggestionRow kind="user"', () => {
  it('has none of the doc additions', () => {
    const suggestion: MemorySuggestion = {
      id: 's1',
      user_id: 1,
      conversation_id: 'c1',
      source: 'conversation',
      dream_run_id: null,
      content: 'Likes tea',
      reasoning: null,
      replaces_memory_id: null,
      status: 'pending',
      memory_id: null,
      created_at: null,
      resolved_at: null,
    };
    // No router: a personal row renders no link.
    const { container } = render(
      <MemorySuggestionRow
        kind="user"
        suggestion={suggestion}
        draft={'x'.repeat(5000)}
        onDraftChange={() => {}}
        busy={false}
        onAccept={() => {}}
        onDismiss={() => {}}
      />,
    );
    const textarea = screen.getByLabelText('Memory text');
    expect(textarea.getAttribute('aria-invalid')).toBeNull();
    expect(textarea.getAttribute('aria-describedby')).toBeNull();
    expect(container.querySelector('.memory-suggestion-doc-header')).toBeNull();
    expect(container.querySelector('.memory-suggestion-counter')).toBeNull();
    expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(false);
  });
});
