// MemorySuggestionRow's doc kind: the Instructions / Memory pill and the doc
// link (or "Unavailable doc" with Accept disabled and described by the
// line explaining why, or neither with showDocLink={false}), the
// instructions diff from the current text with its labels, the legend
// chips of the changes it has and the moved notice, the doc-memory
// replacement diff and orphan pill, the cap counter (code points of the
// trimmed draft, " characters" for screen readers) that disables Accept
// over the cap, the over-cap line in a polite live region, and the textarea
// labels. A personal row keeps none of the doc additions, and a personal or
// project row's Accept is disabled on a blank text. An archive-only
// personal or project row (a dream retiring a memory) shows the memory it
// retires (struck through, or a line saying it is already gone) in a group
// named by its label and that text, the row's focus target, instead of a
// textarea, with Accept enabled; while busy the group is aria-disabled.
// Every row carries its id for useSuggestionListFocus. The row is
// presentational, so a small harness owns the draft. DocTitleLink, shared
// with the inbox, takes its classes from a prefix and its icon on request.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { MemoryRouter } from 'react-router-dom';
import type { DocNoteSuggestion, MemorySuggestion } from '../api/types';
import { DocTitleLink, MemorySuggestionRow } from './MemorySuggestionRow';

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
  onDismiss?: () => void;
}

function DocRowHarness({
  suggestion,
  draft,
  showDocLink,
  onOpenDoc,
  onAccept,
  onDismiss,
}: HarnessProps) {
  const [text, setText] = useState(draft ?? suggestion.content);
  return (
    <MemorySuggestionRow
      kind="doc"
      suggestion={suggestion}
      draft={text}
      onDraftChange={setText}
      busy={false}
      onAccept={onAccept ?? (() => {})}
      onDismiss={onDismiss ?? (() => {})}
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

/** The diff legend's chips, in order (none: "(same text)" or no diff). */
function legendChips(): string[] {
  return Array.from(document.querySelectorAll('.memory-suggestion-diff-legend > span'))
    .map((chip) => chip.textContent ?? '');
}

const UNAVAILABLE_NOTICE = 'This doc was deleted or you no longer have access to it. '
  + 'You can only dismiss this suggestion.';

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

  it('labels new instructions when the doc has none, with only the "added" chip', () => {
    renderDocRow({ suggestion: docNote({ current_instructions: '' }) });
    expect(screen.getByTestId('memory-suggestion-diff').textContent)
      .toContain('New instructions (the doc has none)');
    expect(diffBody().querySelector('.memory-suggestion-diff-add')!.textContent)
      .toBe('Prices are in EUR.');
    expect(legendChips()).toEqual(['added']);
  });

  it('shows the legend chips of the changes the diff has', () => {
    renderDocRow({ suggestion: docNote() });
    expect(legendChips()).toEqual(['removed', 'added']);
    const textarea = screen.getByLabelText('Doc instructions text');
    // A pure deletion.
    fireEvent.change(textarea, { target: { value: 'Prices are' } });
    expect(legendChips()).toEqual(['removed']);
    // A pure addition.
    fireEvent.change(textarea, { target: { value: 'Prices are in USD. Always.' } });
    expect(legendChips()).toEqual(['added']);
    // No change: no legend, the note instead.
    fireEvent.change(textarea, { target: { value: 'Prices are in USD.' } });
    expect(legendChips()).toEqual([]);
    expect(screen.getByTestId('memory-suggestion-diff').textContent).toContain('(same text)');
  });

  it('shows no "removed" chip for current instructions that only end in whitespace', () => {
    renderDocRow({
      suggestion: docNote({ current_instructions: 'Prices are in USD.\n', content: 'Prices are in USD. Always.' }),
    });
    expect(legendChips()).toEqual(['added']);
    expect(diffBody().textContent).toBe('Prices are in USD. Always.');
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
  it('ignores whitespace around the replaced text in the diff and its chips', () => {
    renderDocRow({
      suggestion: memoryRow({
        replaces_memory_id: 'm1',
        replaces_content: 'The board meets weekly.  \n',
        content: 'The board meets weekly. On Mondays.',
      }),
    });
    expect(diffBody().querySelector('.memory-suggestion-diff-del')).toBeNull();
    expect(diffBody().querySelector('.memory-suggestion-diff-add')!.textContent).toBe('On Mondays.');
    expect(legendChips()).toEqual(['added']);
  });

  it('shows only the "removed" chip for a replacement that only drops words', () => {
    renderDocRow({
      suggestion: memoryRow({
        replaces_memory_id: 'm1',
        replaces_content: 'The board meets weekly, on Mondays.',
        content: 'The board meets weekly,',
      }),
    });
    expect(diffBody().querySelector('.memory-suggestion-diff-add')).toBeNull();
    expect(legendChips()).toEqual(['removed']);
  });

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
  it('names an unavailable doc without a link, says why and only lets it be dismissed', () => {
    const onDismiss = vi.fn();
    const { container } = renderDocRow({ suggestion: docNote({ doc_title: '' }), onDismiss });
    expect(screen.queryByRole('link')).toBeNull();
    const label = container.querySelector('.memory-suggestion-doc-unavailable')!;
    expect(label.textContent).toBe('Unavailable doc');
    expect(label.getAttribute('title'))
      .toBe('This doc was deleted or you no longer have access to it');
    const notice = screen.getByText(UNAVAILABLE_NOTICE);
    expect(notice.className).toBe('memory-suggestion-unavailable');
    const accept = screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement;
    expect(accept.disabled).toBe(true);
    expect(accept.getAttribute('aria-describedby')).toBe(notice.id);
    expect(accept.getAttribute('title')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('explains an unavailable doc where the row names no doc', () => {
    renderDocRow({ suggestion: memoryRow({ doc_title: '' }), showDocLink: false });
    expect(screen.queryByText('Unavailable doc')).toBeNull();
    const notice = screen.getByText(UNAVAILABLE_NOTICE);
    expect(screen.getByRole('button', { name: 'Accept' }).getAttribute('aria-describedby'))
      .toBe(notice.id);
  });

  it('gives an available doc an Accept without a title or description', () => {
    renderDocRow({ suggestion: docNote() });
    const accept = screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement;
    expect(accept.disabled).toBe(false);
    expect(accept.getAttribute('title')).toBeNull();
    expect(accept.getAttribute('aria-describedby')).toBeNull();
    expect(screen.queryByText(UNAVAILABLE_NOTICE)).toBeNull();
  });

  it('carries the suggestion id for the list focus hook', () => {
    const { container } = renderDocRow({ suggestion: docNote() });
    expect(container.querySelector('.memory-suggestion-row')!.getAttribute('data-suggestion-id'))
      .toBe('dn1');
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

describe('DocTitleLink', () => {
  it('links the title with classes from the prefix, the icon only on request', () => {
    const { container, rerender } = render(
      <MemoryRouter>
        <DocTitleLink suggestion={docNote()} classPrefix="memory-inbox-doc" />
      </MemoryRouter>,
    );
    const link = screen.getByRole('link', { name: 'Quarterly plan' });
    expect(link.getAttribute('href')).toBe('/docs/doc-1');
    expect(link.className).toBe('memory-inbox-doc-link');
    expect(link.getAttribute('title')).toBe('Quarterly plan');
    expect(link.querySelector('.memory-inbox-doc-title')?.textContent).toBe('Quarterly plan');
    expect(link.querySelector('svg')).toBeNull();

    rerender(
      <MemoryRouter>
        <DocTitleLink suggestion={docNote()} classPrefix="memory-suggestion-doc" icon />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: 'Quarterly plan' }).querySelector('svg')).not.toBeNull();

    rerender(
      <MemoryRouter>
        <DocTitleLink suggestion={docNote({ doc_title: '' })} classPrefix="memory-inbox-doc" />
      </MemoryRouter>,
    );
    expect(screen.queryByRole('link')).toBeNull();
    const label = container.querySelector('.memory-inbox-doc-unavailable')!;
    expect(label.textContent).toBe('Unavailable doc');
    expect(label.getAttribute('title')).toBe('This doc was deleted or you no longer have access to it');
  });
});

describe('MemorySuggestionRow kind="doc": cap counter', () => {
  it('shows n / max for each note kind', () => {
    const { container, unmount } = renderDocRow({ suggestion: docNote() });
    // " characters" is for screen readers only.
    expect(container.querySelector('.memory-suggestion-counter .memory-suggestion-sr-only')!
      .textContent).toBe(' characters');
    expect(container.querySelector('.memory-suggestion-counter')!.textContent).toBe('18 / 4,000 characters');
    unmount();

    const memory = renderDocRow({ suggestion: memoryRow() });
    expect(memory.container.querySelector('.memory-suggestion-counter')!.textContent)
      .toBe('23 / 1,000 characters');
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
    expect(counter.textContent).toBe('1,001 / 1,000 characters');
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
    expect(counter.textContent).toBe('1,000 / 1,000 characters');
    expect(textarea.getAttribute('aria-invalid')).toBeNull();
    expect(accept.disabled).toBe(false);
  });

  it('announces the over-cap line through a polite live region kept in the DOM', () => {
    const { container } = renderDocRow({ suggestion: memoryRow() });
    const textarea = screen.getByLabelText('Doc memory text');
    const regions = container.querySelectorAll('[aria-live]');
    expect(regions).toHaveLength(1);
    const region = regions[0];
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(region.childElementCount).toBe(0);
    expect(region.textContent).toBe('');

    fireEvent.change(textarea, { target: { value: 'x'.repeat(1001) } });
    expect(container.querySelector('[aria-live]')).toBe(region);
    expect(region.textContent).toBe('Use at most 1,000 characters.');
    const line = region.querySelector('p.memory-suggestion-over-cap')!;
    expect(textarea.getAttribute('aria-describedby')!.split(' ')).toContain(line.id);

    // Further keystrokes over the cap leave the region's text as it is.
    fireEvent.change(textarea, { target: { value: 'x'.repeat(1002) } });
    expect(region.textContent).toBe('Use at most 1,000 characters.');

    fireEvent.change(textarea, { target: { value: 'x'.repeat(10) } });
    expect(container.querySelector('[aria-live]')).toBe(region);
    expect(region.childElementCount).toBe(0);
  });

  it('counts code points, so an emoji is one character', () => {
    const { container } = renderDocRow({ suggestion: memoryRow(), draft: '😀'.repeat(1000) });
    const counter = container.querySelector('.memory-suggestion-counter')!;
    expect(counter.textContent).toBe('1,000 / 1,000 characters');
    expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.change(screen.getByLabelText('Doc memory text'), {
      target: { value: '😀'.repeat(1001) },
    });
    expect(counter.textContent).toBe('1,001 / 1,000 characters');
    expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('MemorySuggestionRow textarea', () => {
  it('grows to the text plus padding and borders (border-box)', () => {
    const scrollHeight = vi.spyOn(HTMLTextAreaElement.prototype, 'scrollHeight', 'get')
      .mockReturnValue(60);
    const realStyle = window.getComputedStyle.bind(window);
    const computed = vi.spyOn(window, 'getComputedStyle').mockImplementation((el, pseudo) => {
      const style = realStyle(el, pseudo);
      if (!(el instanceof HTMLTextAreaElement)) return style;
      return { ...style, borderTopWidth: '1px', borderBottomWidth: '1.5px' } as CSSStyleDeclaration;
    });
    try {
      renderDocRow({ suggestion: memoryRow() });
      expect((screen.getByLabelText('Doc memory text') as HTMLTextAreaElement).style.height)
        .toBe('62.5px');
    } finally {
      scrollHeight.mockRestore();
      computed.mockRestore();
    }
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
    expect(container.querySelector('[aria-live]')).toBeNull();
    expect(container.querySelector('.memory-suggestion-unavailable')).toBeNull();
    expect(container.querySelector('.memory-suggestion-row')!.getAttribute('data-suggestion-id'))
      .toBe('s1');
    expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(false);
  });
});

/** A personal or project memory suggestion; by default a conversation's. */
const memorySuggestion = (extra: Partial<MemorySuggestion> = {}): MemorySuggestion => ({
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
  ...extra,
});

/** A dream's archive-only suggestion: no text of its own, no conversation. */
const archiveOnly = (extra: Partial<MemorySuggestion> = {}): MemorySuggestion => memorySuggestion({
  id: 'a1',
  conversation_id: null,
  source: 'dream',
  dream_run_id: 'run-1',
  content: null,
  reasoning: 'Superseded by a newer memory.',
  replaces_memory_id: 'm1',
  replaces_content: 'Prefers tea',
  ...extra,
});

function renderMemoryRow(
  kind: 'user' | 'project',
  suggestion: MemorySuggestion,
  { draft = suggestion.content ?? '', busy = false, onAccept = () => {} } = {},
) {
  return render(
    <MemorySuggestionRow
      kind={kind}
      suggestion={suggestion}
      draft={draft}
      onDraftChange={() => {}}
      busy={busy}
      onAccept={onAccept}
      onDismiss={() => {}}
    />,
  );
}

const ARCHIVE_LABELS = {
  user: {
    label: 'Archives this memory',
    title: 'Accepting archives this memory; nothing new is saved',
  },
  project: {
    label: 'Archives this project memory',
    title: 'Accepting archives this project memory; nothing new is saved',
  },
} as const;

const GONE_LINE = 'This memory is already gone; accepting changes nothing.';

describe('MemorySuggestionRow kind="user" / "project": Accept', () => {
  it('is disabled on a blank text', () => {
    for (const kind of ['user', 'project'] as const) {
      const { unmount } = renderMemoryRow(kind, memorySuggestion(), { draft: ' \n ' });
      expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(true);
      unmount();
    }
  });
});

describe('MemorySuggestionRow kind="user" / "project": archive-only', () => {
  it('shows the memory it retires, struck through, instead of a textarea, with Accept enabled', () => {
    for (const kind of ['user', 'project'] as const) {
      const { label, title } = ARCHIVE_LABELS[kind];
      const onAccept = vi.fn();
      const { container, unmount } = renderMemoryRow(kind, archiveOnly(), { onAccept });
      expect(container.querySelector(`.memory-suggestion-row--${kind}`)).not.toBeNull();
      expect(screen.queryByRole('textbox')).toBeNull();

      // A group named by its label and the memory's text: the row's focus
      // target, out of the Tab order.
      const block = screen.getByRole('group', { name: `${label} Prefers tea` });
      expect(block).toBe(screen.getByTestId('memory-suggestion-archive'));
      expect(block.getAttribute('tabindex')).toBe('-1');
      expect(block.getAttribute('aria-disabled')).toBeNull();
      expect(screen.getByText(label).getAttribute('title')).toBe(title);
      // The body is struck through (MemorySuggestions.css); "gone" is not.
      const body = block.querySelector('.memory-suggestion-archive-body')!;
      expect(body.textContent).toBe('Prefers tea');
      expect(body.classList.contains('memory-suggestion-archive-gone')).toBe(false);

      // Nothing new is saved: no replacement diff or pill, just the reason.
      expect(screen.queryByTestId('memory-suggestion-diff')).toBeNull();
      expect(container.querySelector('.memory-suggestion-replaces')).toBeNull();
      expect(container.textContent).toContain('Superseded by a newer memory.');

      const accept = screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement;
      expect(accept.disabled).toBe(false);
      fireEvent.click(accept);
      expect(onAccept).toHaveBeenCalledTimes(1);
      unmount();
    }
  });

  it('says when the memory it retires is already gone, Accept still enabled', () => {
    for (const kind of ['user', 'project'] as const) {
      const { unmount } = renderMemoryRow(kind, archiveOnly({ replaces_content: null }));
      const block = screen.getByRole('group', { name: `${ARCHIVE_LABELS[kind].label} ${GONE_LINE}` });
      const body = block.querySelector('.memory-suggestion-archive-body')!;
      expect(body.textContent).toBe(GONE_LINE);
      expect(body.classList.contains('memory-suggestion-archive-gone')).toBe(true);
      expect(screen.queryByRole('textbox')).toBeNull();
      expect(document.querySelector('.memory-suggestion-replaces')).toBeNull();
      expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(false);
      unmount();
    }
  });

  it('marks the block aria-disabled and disables both buttons while busy', () => {
    renderMemoryRow('user', archiveOnly(), { busy: true });
    expect(screen.getByTestId('memory-suggestion-archive').getAttribute('aria-disabled')).toBe('true');
    expect((screen.getByRole('button', { name: 'Accept' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Dismiss' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
