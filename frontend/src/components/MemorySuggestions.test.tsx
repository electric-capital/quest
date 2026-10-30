// Suggested-memories bubble + panel: hidden at zero, opens on click and
// lists the conversation's pending suggestions, accept sends the edited
// text only when it changed, dismiss removes the row, a 409 from another
// tab drops the row silently, a count change while open re-fetches, and
// Accept all (personal and project) skips a row whose draft is blank.
// The doc kind adds its icon, wording and store count, the accept options
// with the instructions version, the re-base on a 409 stale_instructions
// (against the row in state, then a re-fetch), the fetch the accept's own
// count event starts kept (it re-bases a second instructions row on the
// same doc), and an Accept all that skips a row its own Accept refuses.
// Focus stays in the panel as rows leave (the next row's text field, else
// the panel's title) and comes back to the bubble button when the panel
// closes; the panel's loading and empty lines.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import type { DocNoteSuggestion, MemorySuggestion } from '../api/types';
import { ApiClientError } from '../api/request';
import { conversationStore } from '../store/conversationStore';
import { MemorySuggestions } from './MemorySuggestions';

const mocks = vi.hoisted(() => ({
  fetchMemorySuggestions: vi.fn(),
  acceptMemorySuggestion: vi.fn(),
  dismissMemorySuggestion: vi.fn(),
  fetchProjectMemorySuggestions: vi.fn(),
  acceptProjectMemorySuggestion: vi.fn(),
  dismissProjectMemorySuggestion: vi.fn(),
  fetchConversationDocNoteSuggestions: vi.fn(),
  acceptDocNoteSuggestion: vi.fn(),
  dismissDocNoteSuggestion: vi.fn(),
}));

vi.mock('../api/client', () => ({
  fetchMemorySuggestions: mocks.fetchMemorySuggestions,
  acceptMemorySuggestion: mocks.acceptMemorySuggestion,
  dismissMemorySuggestion: mocks.dismissMemorySuggestion,
  fetchProjectMemorySuggestions: mocks.fetchProjectMemorySuggestions,
  acceptProjectMemorySuggestion: mocks.acceptProjectMemorySuggestion,
  dismissProjectMemorySuggestion: mocks.dismissProjectMemorySuggestion,
}));

// Only the doc note calls are faked; the conflict helpers and caps stay real.
vi.mock('../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/docsApi')>()),
  fetchConversationDocNoteSuggestions: mocks.fetchConversationDocNoteSuggestions,
  acceptDocNoteSuggestion: mocks.acceptDocNoteSuggestion,
  dismissDocNoteSuggestion: mocks.dismissDocNoteSuggestion,
}));

const suggestion = (id: string, content: string, extra: Partial<MemorySuggestion> = {}): MemorySuggestion => ({
  id,
  user_id: 1,
  conversation_id: 'c1',
  source: 'conversation',
  dream_run_id: null,
  content,
  reasoning: null,
  replaces_memory_id: null,
  status: 'pending',
  memory_id: null,
  created_at: '2026-10-14T00:00:00Z',
  resolved_at: null,
  ...extra,
});

function serve(list: MemorySuggestion[]) {
  mocks.fetchMemorySuggestions.mockResolvedValue({ suggestions: list, pending_count: list.length });
}

beforeEach(() => {
  mocks.fetchMemorySuggestions.mockReset();
  mocks.acceptMemorySuggestion.mockReset().mockResolvedValue({});
  mocks.dismissMemorySuggestion.mockReset().mockResolvedValue({});
  mocks.fetchProjectMemorySuggestions.mockReset();
  mocks.acceptProjectMemorySuggestion.mockReset().mockResolvedValue({});
  mocks.dismissProjectMemorySuggestion.mockReset().mockResolvedValue({});
  mocks.fetchConversationDocNoteSuggestions.mockReset();
  mocks.acceptDocNoteSuggestion.mockReset().mockResolvedValue({});
  mocks.dismissDocNoteSuggestion.mockReset().mockResolvedValue({});
});

afterEach(cleanup);

describe('MemorySuggestions', () => {
  it('renders nothing while no suggestion is pending', () => {
    const { container } = render(<MemorySuggestions conversationId="c1" pendingCount={0} />);
    expect(container.childElementCount).toBe(0);
    expect(mocks.fetchMemorySuggestions).not.toHaveBeenCalled();
  });

  it('shows the count and opens the panel with the fetched list', async () => {
    serve([
      suggestion('s1', 'Prefers Python examples', { reasoning: 'Asked twice.' }),
      suggestion('s2', 'Works in UTC', { replaces_memory_id: 'm-old' }),
    ]);
    render(<MemorySuggestions conversationId="c1" pendingCount={2} />);

    const bubble = screen.getByRole('button', { name: /2 suggested memories/ });
    expect(mocks.fetchMemorySuggestions).not.toHaveBeenCalled();
    fireEvent.click(bubble);

    const dialog = await screen.findByRole('dialog', { name: 'Suggested memories' });
    expect(mocks.fetchMemorySuggestions).toHaveBeenCalledWith('c1');
    await waitFor(() => expect(dialog.textContent).toContain('Asked twice.'));
    expect(screen.getAllByLabelText('Memory text').map((el) => (el as HTMLTextAreaElement).value))
      .toEqual(['Prefers Python examples', 'Works in UTC']);
    expect(dialog.textContent).toContain('Replaces an existing memory');
    expect(screen.getByRole('button', { name: 'Accept all' })).not.toBeNull();
  });

  it('shows a word diff against the replaced memory that follows the draft', async () => {
    serve([
      suggestion('s1', 'Drinks pour-over coffee', {
        replaces_memory_id: 'm-old',
        replaces_content: 'Drinks drip coffee',
      }),
      // Replaced memory deleted meanwhile: no diff, just the plain pill.
      suggestion('s2', 'Works in UTC', { replaces_memory_id: 'm-gone', replaces_content: null }),
    ]);
    render(<MemorySuggestions conversationId="c1" pendingCount={2} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memories/ }));
    await screen.findByDisplayValue('Drinks pour-over coffee');

    const diffs = screen.getAllByTestId('memory-suggestion-diff');
    expect(diffs).toHaveLength(1);
    const body = diffs[0].querySelector('.memory-suggestion-diff-body')!;
    expect(body.textContent).toBe('Drinks drip pour-over coffee');
    expect(body.querySelector('.memory-suggestion-diff-del')!.textContent).toBe('drip');
    expect(body.querySelector('.memory-suggestion-diff-add')!.textContent).toBe('pour-over');
    expect(screen.getAllByText('Replaces an existing memory')).toHaveLength(2);
    expect(document.querySelectorAll('.memory-suggestion-replaces')).toHaveLength(1);

    // Editing the draft re-diffs; restoring the old text shows no change.
    fireEvent.change(screen.getByDisplayValue('Drinks pour-over coffee'), {
      target: { value: 'Drinks drip coffee ' },
    });
    expect(body.querySelector('.memory-suggestion-diff-del')).toBeNull();
    expect(body.querySelector('.memory-suggestion-diff-add')).toBeNull();
    expect(diffs[0].textContent).toContain('(same text)');
  });

  it('accepts with the proposed text, or the edited text when changed', async () => {
    serve([suggestion('s1', 'Likes tea'), suggestion('s2', 'Likes rain')]);
    render(<MemorySuggestions conversationId="c1" pendingCount={2} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memories/ }));
    await screen.findByDisplayValue('Likes tea');

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(mocks.acceptMemorySuggestion).toHaveBeenCalledWith('s1', undefined));
    await waitFor(() => expect(screen.queryByDisplayValue('Likes tea')).toBeNull());

    const rain = screen.getByDisplayValue('Likes rain');
    fireEvent.change(rain, { target: { value: 'Likes walking in the rain ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptMemorySuggestion)
      .toHaveBeenCalledWith('s2', 'Likes walking in the rain'));
  });

  it('dismisses a row and surfaces a failure inline', async () => {
    serve([suggestion('s1', 'A'), suggestion('s2', 'B')]);
    mocks.dismissMemorySuggestion
      .mockRejectedValueOnce(new ApiClientError('boom', 500, 'server_error'))
      .mockResolvedValueOnce({});
    render(<MemorySuggestions conversationId="c1" pendingCount={2} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memories/ }));
    await screen.findByDisplayValue('A');

    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[0]);
    expect((await screen.findByRole('alert')).textContent).toContain('boom');
    expect(screen.getByDisplayValue('A')).not.toBeNull();

    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[0]);
    await waitFor(() => expect(screen.queryByDisplayValue('A')).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(mocks.dismissMemorySuggestion).toHaveBeenCalledTimes(2);
  });

  it('drops a row resolved from another tab without an error', async () => {
    serve([suggestion('s1', 'A')]);
    mocks.acceptMemorySuggestion.mockRejectedValueOnce(
      new ApiClientError('already accepted', 409, 'suggestion_resolved'),
    );
    render(<MemorySuggestions conversationId="c1" pendingCount={1} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memory/ }));
    await screen.findByDisplayValue('A');
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(screen.queryByDisplayValue('A')).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('re-fetches when the count changes while open and closes at zero', async () => {
    serve([suggestion('s1', 'A')]);
    const { rerender } = render(<MemorySuggestions conversationId="c1" pendingCount={1} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memory/ }));
    await screen.findByDisplayValue('A');

    serve([suggestion('s1', 'A'), suggestion('s2', 'B')]);
    rerender(<MemorySuggestions conversationId="c1" pendingCount={2} />);
    await screen.findByDisplayValue('B');
    expect(mocks.fetchMemorySuggestions).toHaveBeenCalledTimes(2);

    rerender(<MemorySuggestions conversationId="c1" pendingCount={0} />);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByRole('button', { name: /suggested/ })).toBeNull();
  });

  it('reconciles the store count from the list, keeping a late fetch minus the row resolved here', async () => {
    serve([suggestion('s1', 'A'), suggestion('s2', 'B')]);
    const { rerender } = render(<MemorySuggestions conversationId="c-stale" pendingCount={1} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memory/ }));
    await screen.findByDisplayValue('A');
    // The list said 2 while the (stale) store count said 1.
    expect(conversationStore.getConversation('c-stale').pendingMemorySuggestions).toBe(2);

    // A count change starts a second fetch that stays in flight...
    let resolveStale: (value: { suggestions: MemorySuggestion[]; pending_count: number }) => void = () => {};
    mocks.fetchMemorySuggestions.mockImplementationOnce(
      () => new Promise((resolve) => { resolveStale = resolve; }),
    );
    rerender(<MemorySuggestions conversationId="c-stale" pendingCount={2} />);
    await waitFor(() => expect(mocks.fetchMemorySuggestions).toHaveBeenCalledTimes(2));

    // ...while the user dismisses A; the late result must not bring A back.
    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[0]);
    await waitFor(() => expect(screen.queryByDisplayValue('A')).toBeNull());
    await act(async () => {
      resolveStale({ suggestions: [suggestion('s1', 'A'), suggestion('s2', 'B')], pending_count: 2 });
    });
    expect(screen.queryByDisplayValue('A')).toBeNull();
    expect(screen.getByDisplayValue('B')).not.toBeNull();
    // Its count leaves out the row dismissed here too.
    expect(conversationStore.getConversation('c-stale').pendingMemorySuggestions).toBe(1);
  });

  it('says it is loading, then that nothing is open', async () => {
    let resolveList: (value: { suggestions: MemorySuggestion[]; pending_count: number }) => void = () => {};
    mocks.fetchMemorySuggestions.mockImplementationOnce(
      () => new Promise((resolve) => { resolveList = resolve; }),
    );
    render(<MemorySuggestions conversationId="c1" pendingCount={1} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memory/ }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.querySelector('.memory-suggestions-empty')!.textContent).toBe('Loading suggestions...');
    // Resolved elsewhere before the list came back.
    await act(async () => {
      resolveList({ suggestions: [], pending_count: 0 });
    });
    await waitFor(() => expect(dialog.querySelector('.memory-suggestions-empty')!.textContent)
      .toBe('No open suggestions.'));
  });

  it('closes on Escape and on the backdrop', async () => {
    serve([suggestion('s1', 'A')]);
    const { container } = render(<MemorySuggestions conversationId="c1" pendingCount={1} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memory/ }));
    await screen.findByRole('dialog');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: /suggested memory/ }));
    await screen.findByRole('dialog');
    fireEvent.click(container.querySelector('.memory-suggestions-backdrop')!);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});

describe('MemorySuggestions kind="project"', () => {
  it('uses the project endpoints, wording and store count', async () => {
    mocks.fetchProjectMemorySuggestions.mockResolvedValue({
      suggestions: [
        suggestion('p1', 'Revenue is reported in EUR', {
          replaces_memory_id: 'pm-old',
          replaces_content: 'Revenue is reported in USD',
        }),
        suggestion('p2', 'Board meets on the first Monday'),
      ],
      pending_count: 2,
    });
    const { container } = render(
      <MemorySuggestions conversationId="c-proj" pendingCount={1} kind="project" />,
    );
    expect(container.querySelector('.memory-suggestions--project')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /1 suggested project memory/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Suggested project memories' });
    expect(mocks.fetchProjectMemorySuggestions).toHaveBeenCalledWith('c-proj');
    expect(mocks.fetchMemorySuggestions).not.toHaveBeenCalled();
    await screen.findByDisplayValue('Revenue is reported in EUR');
    expect(dialog.textContent).toContain('Replaces an existing project memory');
    expect(screen.getAllByLabelText('Project memory text')).toHaveLength(2);
    // The list reconciles the project count, never the personal one.
    expect(conversationStore.getConversation('c-proj').pendingProjectMemorySuggestions).toBe(2);
    expect(conversationStore.getConversation('c-proj').pendingMemorySuggestions).toBe(0);

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(mocks.acceptProjectMemorySuggestion).toHaveBeenCalledWith('p1', undefined));
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    await waitFor(() => expect(mocks.dismissProjectMemorySuggestion).toHaveBeenCalledWith('p2'));
    expect(mocks.acceptMemorySuggestion).not.toHaveBeenCalled();
  });
});

describe('MemorySuggestions Accept all', () => {
  it('skips a personal or project row whose draft is blank', async () => {
    const kinds = [
      { kind: 'user', fetch: mocks.fetchMemorySuggestions, accept: mocks.acceptMemorySuggestion, label: 'Memory text' },
      { kind: 'project', fetch: mocks.fetchProjectMemorySuggestions, accept: mocks.acceptProjectMemorySuggestion, label: 'Project memory text' },
    ] as const;
    for (const { kind, fetch, accept, label } of kinds) {
      fetch.mockResolvedValue({
        suggestions: [
          suggestion('s1', 'Likes tea'),
          suggestion('s2', 'Likes rain'),
          suggestion('s3', 'Works in UTC'),
        ],
        pending_count: 3,
      });
      render(<MemorySuggestions conversationId={`c-bulk-${kind}`} pendingCount={3} kind={kind} />);
      fireEvent.click(screen.getByRole('button', { name: /3 suggested/ }));
      await screen.findByDisplayValue('Works in UTC');
      // A blank draft would save an empty memory: its own Accept is
      // disabled, and Accept all leaves it too.
      fireEvent.change(screen.getByDisplayValue('Likes rain'), { target: { value: ' \n ' } });
      const accepts = screen.getAllByRole('button', { name: 'Accept' }) as HTMLButtonElement[];
      expect(accepts.map((b) => b.disabled)).toEqual([false, true, false]);

      fireEvent.click(screen.getByRole('button', { name: 'Accept all' }));
      await waitFor(() => expect(screen.queryByDisplayValue('Works in UTC')).toBeNull());
      expect(accept.mock.calls).toEqual([['s1', undefined], ['s3', undefined]]);
      expect(screen.getAllByLabelText(label).map((el) => (el as HTMLTextAreaElement).value))
        .toEqual([' \n ']);
      expect(screen.queryByRole('alert')).toBeNull();
      cleanup();
    }
  });
});

describe('MemorySuggestions focus', () => {
  /** Focus a button and click it, as a browser does on a click. */
  function press(button: HTMLElement) {
    act(() => button.focus());
    fireEvent.click(button);
  }

  async function openPanel(rows: MemorySuggestion[]) {
    serve(rows);
    const view = render(<MemorySuggestions conversationId="c-focus" pendingCount={rows.length} />);
    const bubble = screen.getByRole('button', { name: /suggested memor/ });
    press(bubble);
    // A conversation's suggestion always has text (only a dream's can be archive-only).
    await screen.findByDisplayValue(rows[0].content ?? '');
    return { ...view, bubble };
  }

  it("moves focus to the panel's title when the only row is dismissed, then to the bubble on close", async () => {
    const { bubble } = await openPanel([suggestion('s1', 'Likes tea')]);
    press(screen.getByRole('button', { name: 'Dismiss' }));
    await waitFor(() => expect(screen.queryByDisplayValue('Likes tea')).toBeNull());
    expect(screen.getByRole('dialog')).not.toBeNull();
    const title = screen.getByRole('heading', { name: 'Suggested memories' });
    expect(title.getAttribute('tabindex')).toBe('-1');
    expect(document.activeElement).toBe(title);

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(document.activeElement).toBe(bubble);
  });

  it("moves focus to the panel's title, not the bubble, when the row being typed in leaves remotely", async () => {
    serve([suggestion('s1', 'Likes tea'), suggestion('s2', 'Likes rain')]);
    const { rerender } = render(<MemorySuggestions conversationId="c-remote" pendingCount={2} />);
    fireEvent.click(screen.getByRole('button', { name: /suggested memories/ }));
    const tea = await screen.findByDisplayValue('Likes tea');
    act(() => tea.focus());
    // Accepted in another tab: the count event re-reads the list without it.
    serve([suggestion('s2', 'Likes rain')]);
    rerender(<MemorySuggestions conversationId="c-remote" pendingCount={1} />);
    await waitFor(() => expect(screen.queryByDisplayValue('Likes tea')).toBeNull());
    expect(document.activeElement)
      .toBe(screen.getByRole('heading', { name: 'Suggested memories' }));
    expect(screen.getByRole('dialog')).not.toBeNull();
  });

  it('returns focus to the bubble button on Escape, the X button and the backdrop', async () => {
    const { bubble, container } = await openPanel([suggestion('s1', 'Likes tea')]);
    act(() => screen.getByDisplayValue('Likes tea').focus());
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(document.activeElement).toBe(bubble);

    press(bubble);
    await screen.findByDisplayValue('Likes tea');
    press(screen.getByRole('button', { name: 'Close suggested memories' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(document.activeElement).toBe(bubble);

    press(bubble);
    act(() => screen.getByDisplayValue('Likes tea').focus());
    // A click on the backdrop (inside the bubble's surface) drops focus to
    // <body> before it closes the panel.
    const backdrop = container.querySelector('.memory-suggestions-backdrop')!;
    const down = new Event('pointerdown', { bubbles: true });
    Object.defineProperty(down, 'pointerType', { value: 'mouse' });
    act(() => {
      backdrop.dispatchEvent(down);
    });
    act(() => (document.activeElement as HTMLElement).blur());
    fireEvent.click(backdrop);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(document.activeElement).toBe(bubble);
  });

  it('leaves focus where it is when the panel closes with focus outside it', async () => {
    serve([suggestion('s1', 'Likes tea')]);
    render(
      <>
        <MemorySuggestions conversationId="c-focus" pendingCount={1} />
        <textarea aria-label="Composer" />
      </>,
    );
    fireEvent.click(screen.getByRole('button', { name: /suggested memory/ }));
    await screen.findByDisplayValue('Likes tea');
    const composer = screen.getByLabelText('Composer');
    act(() => composer.focus());
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(document.activeElement).toBe(composer);
  });
});

const STALE_MESSAGE = "The doc's instructions changed since this suggestion was shown. "
  + 'Review it against the current text and accept again.';

const docNote = (id: string, extra: Partial<DocNoteSuggestion> = {}): DocNoteSuggestion => ({
  id,
  user_id: 1,
  doc_id: `doc-${id}`,
  conversation_id: 'c-doc',
  note_kind: 'instructions',
  content: 'Prices are in EUR.',
  reasoning: null,
  base_instructions_version: 3,
  replaces_memory_id: null,
  status: 'pending',
  memory_id: null,
  created_at: '2026-10-29T00:00:00Z',
  resolved_at: null,
  doc_title: `Doc ${id}`,
  replaces_content: null,
  current_instructions: 'Prices are in USD.',
  current_instructions_version: 3,
  instructions_moved: false,
  ...extra,
});

const docMemory = (id: string, content: string): DocNoteSuggestion => docNote(id, {
  note_kind: 'memory',
  content,
  base_instructions_version: null,
  current_instructions: null,
  current_instructions_version: null,
});

function serveDoc(list: DocNoteSuggestion[]) {
  mocks.fetchConversationDocNoteSuggestions.mockResolvedValue({
    suggestions: list,
    pending_count: list.length,
  });
}

/** The flat 409 stale_instructions as request.ts maps it. */
function staleError(current: { instructions: string; instructions_version: number } | null) {
  const body: Record<string, unknown> = { error: 'stale_instructions', message: STALE_MESSAGE };
  if (current) body.current = current;
  return new ApiClientError(STALE_MESSAGE, 409, 'stale_instructions', undefined, body);
}

// Doc rows link to their doc, so they render inside a router.
function renderInRouter(ui: ReactElement) {
  const result = render(<MemoryRouter>{ui}</MemoryRouter>);
  return {
    ...result,
    rerender: (next: ReactElement) => result.rerender(<MemoryRouter>{next}</MemoryRouter>),
  };
}

async function openDocPanel(conversationId = 'c-doc', pendingCount = 1) {
  const view = renderInRouter(
    <MemorySuggestions conversationId={conversationId} pendingCount={pendingCount} kind="doc" />,
  );
  fireEvent.click(screen.getByRole('button', { name: /suggested doc note/ }));
  await screen.findByRole('dialog', { name: 'Suggested doc notes' });
  return view;
}

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  let reject: (err: unknown) => void = () => {};
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type DocList = { suggestions: DocNoteSuggestion[]; pending_count: number };

function diffBodies(): Element[] {
  return screen.getAllByTestId('memory-suggestion-diff')
    .map((diff) => diff.querySelector('.memory-suggestion-diff-body')!);
}

describe('MemorySuggestions kind="doc"', () => {
  it('uses the doc note endpoint, wording and store count', async () => {
    serveDoc([docNote('d1'), docMemory('d2', 'The board meets monthly.')]);
    const { container, rerender } = renderInRouter(
      <MemorySuggestions conversationId="c-doc-count" pendingCount={1} kind="doc" />,
    );
    expect(container.querySelector('.memory-suggestions--doc')).not.toBeNull();
    const bubble = screen.getByRole('button', { name: /^1 suggested doc note$/ });
    // The sidebar's Docs icon.
    expect(bubble.querySelector('svg.lucide-file-text')).not.toBeNull();
    rerender(<MemorySuggestions conversationId="c-doc-count" pendingCount={2} kind="doc" />);
    fireEvent.click(screen.getByRole('button', { name: /^2 suggested doc notes$/ }));

    const dialog = await screen.findByRole('dialog', { name: 'Suggested doc notes' });
    expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledWith('c-doc-count');
    expect(mocks.fetchMemorySuggestions).not.toHaveBeenCalled();
    expect(mocks.fetchProjectMemorySuggestions).not.toHaveBeenCalled();
    await screen.findByDisplayValue('The board meets monthly.');
    expect(dialog.textContent).toContain('instructions or memories for docs in this chat');
    expect(screen.getByLabelText('Doc instructions text')).not.toBeNull();
    expect(screen.getByRole('link', { name: 'Doc d1' }).getAttribute('href')).toBe('/docs/doc-d1');
    expect(container.querySelectorAll('.memory-suggestion-row--doc')).toHaveLength(2);
    // The list reconciles the doc note count, never the other two.
    await waitFor(() => expect(
      conversationStore.getConversation('c-doc-count').pendingDocNoteSuggestions,
    ).toBe(2));
    expect(conversationStore.getConversation('c-doc-count').pendingMemorySuggestions).toBe(0);
    expect(conversationStore.getConversation('c-doc-count').pendingProjectMemorySuggestions).toBe(0);
  });

  it('accepts with the instructions version, adding the edited text when changed', async () => {
    serveDoc([
      docNote('d1'),
      docNote('d2', { current_instructions_version: 5 }),
      docMemory('d3', 'The board meets monthly.'),
    ]);
    await openDocPanel('c-doc', 3);
    await screen.findByDisplayValue('The board meets monthly.');

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenCalledWith('d1', { expectedInstructionsVersion: 3 }));
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Accept' })).toHaveLength(2));

    fireEvent.change(screen.getAllByLabelText('Doc instructions text')[0], {
      target: { value: 'Prices are in GBP. ' },
    });
    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledWith(
      'd2',
      { content: 'Prices are in GBP.', expectedInstructionsVersion: 5 },
    ));

    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Accept' })).toHaveLength(1));
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledWith('d3', {}));
  });

  it("moves focus to the next row's text field when a row is accepted", async () => {
    serveDoc([docNote('d1'), docMemory('d2', 'The board meets monthly.')]);
    await openDocPanel('c-doc-focus', 2);
    await screen.findByDisplayValue('The board meets monthly.');
    const accept = screen.getAllByRole('button', { name: 'Accept' })[0];
    act(() => accept.focus());
    fireEvent.click(accept);
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR.')).toBeNull());
    expect(document.activeElement).toBe(screen.getByDisplayValue('The board meets monthly.'));
  });

  it('dismisses a row', async () => {
    serveDoc([docNote('d1')]);
    await openDocPanel();
    await screen.findByDisplayValue('Prices are in EUR.');
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    await waitFor(() => expect(mocks.dismissDocNoteSuggestion).toHaveBeenCalledWith('d1'));
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR.')).toBeNull());
    expect(mocks.acceptDocNoteSuggestion).not.toHaveBeenCalled();
  });

  it('re-bases an instructions row on the current text of a 409 stale_instructions', async () => {
    serveDoc([docNote('d1')]);
    mocks.acceptDocNoteSuggestion
      .mockRejectedValueOnce(staleError({ instructions: 'Prices are in GBP.', instructions_version: 4 }))
      .mockResolvedValueOnce({});
    const { container } = await openDocPanel();
    const textarea = await screen.findByDisplayValue('Prices are in EUR.');
    expect(diffBodies()[0].textContent).toBe('Prices are in USD. EUR.');
    fireEvent.change(textarea, { target: { value: 'Prices are in EUR, always.' } });

    // What the re-fetch after the conflict returns: the same current text.
    serveDoc([docNote('d1', {
      current_instructions: 'Prices are in GBP.',
      current_instructions_version: 4,
      instructions_moved: true,
    })]);
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
    expect(mocks.acceptDocNoteSuggestion).toHaveBeenLastCalledWith(
      'd1',
      { content: 'Prices are in EUR, always.', expectedInstructionsVersion: 3 },
    );
    // The diff now starts from the current text, the draft is kept, and the
    // row says the instructions moved past the proposal's base.
    const body = diffBodies()[0];
    expect(body.querySelector('.memory-suggestion-diff-del')!.textContent).toBe('GBP.');
    expect(body.querySelector('.memory-suggestion-diff-add')!.textContent).toBe('EUR, always.');
    expect(screen.getByDisplayValue('Prices are in EUR, always.')).not.toBeNull();
    expect(container.querySelector('.memory-suggestion-moved')!.textContent)
      .toBe('The instructions changed since this was suggested');
    await waitFor(() => expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(2));
    expect(screen.getByDisplayValue('Prices are in EUR, always.')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenLastCalledWith(
      'd1',
      { content: 'Prices are in EUR, always.', expectedInstructionsVersion: 4 },
    ));
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR, always.')).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('re-fetches after the re-base, past a list fetch in flight when the 409 lands', async () => {
    serveDoc([docNote('d1')]);
    const accept = deferred<unknown>();
    mocks.acceptDocNoteSuggestion
      .mockImplementationOnce(() => accept.promise)
      .mockResolvedValueOnce({});
    const { rerender } = await openDocPanel('c-doc-race', 1);
    await screen.findByDisplayValue('Prices are in EUR.');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledTimes(1));
    // A count change starts a list fetch that is still in flight (with the
    // old row) when the 409 lands...
    const stale = deferred<DocList>();
    mocks.fetchConversationDocNoteSuggestions.mockImplementationOnce(() => stale.promise);
    rerender(<MemorySuggestions conversationId="c-doc-race" pendingCount={2} kind="doc" />);
    await waitFor(() => expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(2));

    // ...and the re-fetch after the conflict brings the newest list.
    const fresh = deferred<DocList>();
    mocks.fetchConversationDocNoteSuggestions.mockImplementationOnce(() => fresh.promise);
    await act(async () => {
      accept.reject(staleError({ instructions: 'Prices are in GBP.', instructions_version: 4 }));
    });
    expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('GBP.');
    await waitFor(() => expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(3));

    await act(async () => {
      fresh.resolve({
        suggestions: [
          docNote('d1', { current_instructions: 'Prices are in CHF.', current_instructions_version: 5 }),
          docMemory('d2', 'The board meets monthly.'),
        ],
        pending_count: 2,
      });
    });
    await screen.findByDisplayValue('The board meets monthly.');
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('CHF.');
    expect(conversationStore.getConversation('c-doc-race').pendingDocNoteSuggestions).toBe(2);
    // The fetch that was in flight lands last and changes nothing.
    await act(async () => {
      stale.resolve({ suggestions: [docNote('d1')], pending_count: 1 });
    });
    expect(screen.getByDisplayValue('The board meets monthly.')).not.toBeNull();
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('CHF.');
    expect(conversationStore.getConversation('c-doc-race').pendingDocNoteSuggestions).toBe(2);

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenLastCalledWith('d1', { expectedInstructionsVersion: 5 }));
  });

  it('drops a fetch in flight that lands right after the 409, before its re-fetch starts', async () => {
    serveDoc([docNote('d1')]);
    const accept = deferred<unknown>();
    mocks.acceptDocNoteSuggestion.mockImplementationOnce(() => accept.promise);
    const { rerender } = await openDocPanel('c-doc-gap', 1);
    await screen.findByDisplayValue('Prices are in EUR.');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledTimes(1));
    const stale = deferred<DocList>();
    mocks.fetchConversationDocNoteSuggestions.mockImplementationOnce(() => stale.promise);
    rerender(<MemorySuggestions conversationId="c-doc-gap" pendingCount={2} kind="doc" />);
    await waitFor(() => expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(2));

    const refetch = deferred<DocList>();
    mocks.fetchConversationDocNoteSuggestions.mockImplementationOnce(() => refetch.promise);
    // The 409 and the older list land together, before React re-renders
    // and its effect cleanup cancels that fetch.
    await act(async () => {
      accept.reject(staleError({ instructions: 'Prices are in GBP.', instructions_version: 4 }));
      await Promise.resolve();
      await Promise.resolve();
      stale.resolve({ suggestions: [docNote('d1')], pending_count: 1 });
      await Promise.resolve();
    });
    expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('GBP.');
    await waitFor(() => expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(3));

    // The re-fetch failing leaves the re-base standing.
    await act(async () => {
      refetch.reject(new Error('offline'));
    });
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('GBP.');
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenLastCalledWith('d1', { expectedInstructionsVersion: 4 }));
  });

  it("keeps the fetch the accept's own event starts, re-basing a second instructions row", async () => {
    // Two instructions proposals for one doc, both diffed against version 3.
    serveDoc([docNote('d1'), docNote('d2', { doc_id: 'doc-d1', content: 'Prices are in GBP.' })]);
    const accept = deferred<unknown>();
    mocks.acceptDocNoteSuggestion
      .mockImplementationOnce(() => accept.promise)
      .mockResolvedValueOnce({});
    const { rerender, container } = await openDocPanel('c-doc-event', 2);
    await screen.findByDisplayValue('Prices are in GBP.');

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenCalledWith('d1', { expectedInstructionsVersion: 3 }));
    // The server publishes the count event inside the accept: the fetch it
    // starts is still in flight when the accept's response lands...
    const eventFetch = deferred<DocList>();
    mocks.fetchConversationDocNoteSuggestions.mockImplementationOnce(() => eventFetch.promise);
    rerender(<MemorySuggestions conversationId="c-doc-event" pendingCount={1} kind="doc" />);
    await waitFor(() => expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(2));
    await act(async () => {
      accept.resolve({});
    });
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR.')).toBeNull());

    // ...and still lands: the second row's diff starts from the accepted text.
    await act(async () => {
      eventFetch.resolve({
        suggestions: [docNote('d2', {
          doc_id: 'doc-d1',
          content: 'Prices are in GBP.',
          current_instructions: 'Prices are in EUR.',
          current_instructions_version: 4,
          instructions_moved: true,
        })],
        pending_count: 1,
      });
    });
    const body = diffBodies()[0];
    expect(body.querySelector('.memory-suggestion-diff-del')!.textContent).toBe('EUR.');
    expect(body.querySelector('.memory-suggestion-diff-add')!.textContent).toBe('GBP.');
    expect(container.querySelector('.memory-suggestion-moved')).not.toBeNull();
    expect(conversationStore.getConversation('c-doc-event').pendingDocNoteSuggestions).toBe(1);

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenLastCalledWith('d2', { expectedInstructionsVersion: 4 }));
    expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(2);
  });

  it('keeps a newer row that a list fetch brought in during the accept', async () => {
    serveDoc([docNote('d1')]);
    const accept = deferred<unknown>();
    mocks.acceptDocNoteSuggestion.mockImplementationOnce(() => accept.promise);
    const { rerender } = await openDocPanel('c-doc-newer', 1);
    await screen.findByDisplayValue('Prices are in EUR.');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledTimes(1));
    serveDoc([docNote('d1', { current_instructions: 'Prices are in CHF.', current_instructions_version: 5 })]);
    rerender(<MemorySuggestions conversationId="c-doc-newer" pendingCount={2} kind="doc" />);
    await waitFor(() => expect(
      diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent,
    ).toBe('CHF.'));

    // The 409 carries an older current text (version 4): no roll-back.
    const refetch = deferred<DocList>();
    mocks.fetchConversationDocNoteSuggestions.mockImplementationOnce(() => refetch.promise);
    await act(async () => {
      accept.reject(staleError({ instructions: 'Prices are in GBP.', instructions_version: 4 }));
    });
    expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('CHF.');
    await waitFor(() => expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(3));
  });

  it('re-fetches when the doc note count changes while open', async () => {
    serveDoc([docNote('d1')]);
    const { rerender } = await openDocPanel('c-doc-count-change', 1);
    await screen.findByDisplayValue('Prices are in EUR.');
    expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(1);

    serveDoc([docNote('d1'), docMemory('d2', 'Figures are unaudited.')]);
    rerender(<MemorySuggestions conversationId="c-doc-count-change" pendingCount={2} kind="doc" />);
    await screen.findByDisplayValue('Figures are unaudited.');
    expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(2);
    expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenLastCalledWith('c-doc-count-change');
  });

  it('re-fetches the list when a 409 stale_instructions carries no current text', async () => {
    serveDoc([docNote('d1')]);
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(staleError(null));
    await openDocPanel();
    await screen.findByDisplayValue('Prices are in EUR.');

    serveDoc([docNote('d1', {
      current_instructions: 'Prices are in GBP.',
      current_instructions_version: 6,
    })]);
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
    await waitFor(() => expect(mocks.fetchConversationDocNoteSuggestions).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(
      diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent,
    ).toBe('GBP.'));

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenLastCalledWith('d1', { expectedInstructionsVersion: 6 }));
  });

  it('drops a row resolved from another tab without an error', async () => {
    serveDoc([docNote('d1')]);
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(
      new ApiClientError('already resolved', 409, 'suggestion_resolved'),
    );
    await openDocPanel();
    await screen.findByDisplayValue('Prices are in EUR.');
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR.')).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('accepts and dismisses all in turn, skipping a row its own Accept refuses', async () => {
    serveDoc([
      docNote('d1'),
      docMemory('d2', 'The board meets monthly.'),
      docMemory('d3', 'Q3 ends in September.'),
      { ...docMemory('d4', 'Costs are in EUR.'), doc_title: '' },
      docMemory('d5', 'Figures are audited.'),
    ]);
    await openDocPanel('c-doc', 5);
    await screen.findByDisplayValue('Figures are audited.');
    // A blank draft would clear the instructions, a draft over the cap would
    // be refused, and an unavailable doc takes no notes: Accept all leaves
    // all three.
    fireEvent.change(screen.getByLabelText('Doc instructions text'), { target: { value: '  ' } });
    fireEvent.change(screen.getByDisplayValue('Q3 ends in September.'), {
      target: { value: 'x'.repeat(1001) },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Accept all' }));
    await waitFor(() => expect(screen.queryByDisplayValue('Figures are audited.')).toBeNull());
    expect(mocks.acceptDocNoteSuggestion.mock.calls.map(([id]) => id)).toEqual(['d2', 'd5']);
    expect(screen.getByLabelText('Doc instructions text')).not.toBeNull();
    expect(screen.getByDisplayValue('x'.repeat(1001))).not.toBeNull();
    expect(screen.getByDisplayValue('Costs are in EUR.')).not.toBeNull();

    cleanup();
    serveDoc([docNote('d1'), docMemory('d4', 'Figures are unaudited.')]);
    await openDocPanel('c-doc-2', 2);
    await screen.findByDisplayValue('Figures are unaudited.');
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss all' }));
    await waitFor(() => expect(mocks.dismissDocNoteSuggestion.mock.calls.map(([id]) => id))
      .toEqual(['d1', 'd4']));
  });
});
