// The inbox's Memories tab: open personal and project suggestions with their
// source line, accept / dismiss through the kind's endpoint moving the row
// into the reviewed list, a 409 dropping the row silently, the reviewed
// list paging in through the scroll sentinel (and the Load more fallback),
// a per-user global re-reading the open list, and doc note rows (a kind
// with no endpoints here) left out of both lists.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { DocNoteInboxSuggestion, MemoryInboxRow, MemoryInboxSuggestion } from '../api/types';
import { ApiClientError } from '../api/request';
import { MemoryInbox, RESOLVED_PAGE_SIZE } from './MemoryInbox';

const mocks = vi.hoisted(() => ({
  fetchMemoryInboxPending: vi.fn(),
  fetchMemoryInboxResolved: vi.fn(),
  acceptMemorySuggestion: vi.fn(),
  dismissMemorySuggestion: vi.fn(),
  acceptProjectMemorySuggestion: vi.fn(),
  dismissProjectMemorySuggestion: vi.fn(),
  globalHandlers: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('../api/client', () => ({
  fetchMemoryInboxPending: mocks.fetchMemoryInboxPending,
  fetchMemoryInboxResolved: mocks.fetchMemoryInboxResolved,
  acceptMemorySuggestion: mocks.acceptMemorySuggestion,
  dismissMemorySuggestion: mocks.dismissMemorySuggestion,
  acceptProjectMemorySuggestion: mocks.acceptProjectMemorySuggestion,
  dismissProjectMemorySuggestion: mocks.dismissProjectMemorySuggestion,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (handler: (event: Record<string, unknown>) => void) => {
      mocks.globalHandlers.add(handler);
      return () => mocks.globalHandlers.delete(handler);
    },
  },
}));

function row(
  id: string,
  content: string,
  extra: Partial<MemoryInboxSuggestion> = {},
): MemoryInboxSuggestion {
  return {
    id,
    user_id: 1,
    conversation_id: `conv-${id}`,
    source: 'conversation',
    dream_run_id: null,
    content,
    reasoning: null,
    replaces_memory_id: null,
    status: 'pending',
    memory_id: null,
    created_at: '2026-10-14T00:00:00',
    resolved_at: null,
    kind: 'user',
    project_id: null,
    project_name: null,
    conversation_title: `Chat ${id}`,
    ...extra,
  };
}

function reviewed(id: string, content: string, status: 'accepted' | 'dismissed', extra: Partial<MemoryInboxSuggestion> = {}) {
  return row(id, content, { status, resolved_at: '2026-10-14T12:00:00', ...extra });
}

/** A doc note suggestion as the inbox lists it (``kind: 'doc'``). */
function docRow(
  id: string,
  content: string,
  extra: Partial<DocNoteInboxSuggestion> = {},
): DocNoteInboxSuggestion {
  return {
    id,
    user_id: 1,
    doc_id: `doc-${id}`,
    conversation_id: `conv-${id}`,
    note_kind: 'memory',
    content,
    reasoning: `Why ${id}`,
    base_instructions_version: null,
    replaces_memory_id: null,
    status: 'pending',
    memory_id: null,
    created_at: '2026-10-14T00:00:00',
    resolved_at: null,
    doc_title: `Doc ${id}`,
    replaces_content: null,
    current_instructions: null,
    instructions_moved: false,
    kind: 'doc',
    project_id: null,
    project_name: null,
    conversation_title: `Chat ${id}`,
    ...extra,
  };
}

function servePending(list: MemoryInboxRow[]) {
  mocks.fetchMemoryInboxPending.mockResolvedValue({ suggestions: list, pending_count: list.length });
}

function serveResolved(list: MemoryInboxRow[], nextCursor: string | null = null) {
  mocks.fetchMemoryInboxResolved.mockResolvedValue({ suggestions: list, next_cursor: nextCursor });
}

function emit(event: Record<string, unknown>) {
  act(() => {
    mocks.globalHandlers.forEach((handler) => handler(event));
  });
}

/** Stand-in for the browser's IntersectionObserver: the test fires it. */
class FakeIntersectionObserver {
  static instances: FakeIntersectionObserver[] = [];
  callback: IntersectionObserverCallback;
  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
    FakeIntersectionObserver.instances.push(this);
  }
  observe() {}
  disconnect() {
    FakeIntersectionObserver.instances = FakeIntersectionObserver.instances.filter((o) => o !== this);
  }
  unobserve() {}
  takeRecords() { return []; }
  root = null;
  rootMargin = '';
  thresholds = [];
  intersect() {
    this.callback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
}

beforeEach(() => {
  mocks.fetchMemoryInboxPending.mockReset();
  mocks.fetchMemoryInboxResolved.mockReset();
  mocks.acceptMemorySuggestion.mockReset();
  mocks.dismissMemorySuggestion.mockReset();
  mocks.acceptProjectMemorySuggestion.mockReset();
  mocks.dismissProjectMemorySuggestion.mockReset();
  mocks.globalHandlers.clear();
  FakeIntersectionObserver.instances = [];
  serveResolved([]);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('MemoryInbox', () => {
  it('lists open suggestions of both kinds with where they came from', async () => {
    servePending([
      row('s1', 'Prefers Python examples', { reasoning: 'Asked twice.' }),
      row('p1', 'Ships on Fridays', {
        kind: 'project', project_id: 'proj', project_name: 'Launch', conversation_title: null,
      }),
    ]);
    serveResolved([reviewed('r1', 'Likes tea', 'accepted'), reviewed('r2', 'Hates rain', 'dismissed')]);
    const goTo = vi.fn();
    render(<MemoryInbox onGoToConversation={goTo} />);

    await screen.findByDisplayValue('Prefers Python examples');
    const open = screen.getByRole('region', { name: /Open suggestions/ });
    expect(within(open).getAllByRole('textbox').map((el) => (el as HTMLTextAreaElement).value))
      .toEqual(['Prefers Python examples', 'Ships on Fridays']);
    expect(open.textContent).toContain('Asked twice.');
    expect(open.textContent).toContain('Project memory');
    expect(open.textContent).toContain('in Launch');
    expect(within(open).getByRole('button', { name: 'Untitled chat' })).not.toBeNull();
    expect(within(open).getByRole('button', { name: 'Accept all' })).not.toBeNull();
    expect(document.querySelector('.memory-suggestion-row--project')).not.toBeNull();

    const done = screen.getByRole('region', { name: 'Recently reviewed' });
    await waitFor(() => expect(done.textContent).toContain('Likes tea'));
    expect(within(done).getByText('Accepted')).not.toBeNull();
    expect(within(done).getByText('Dismissed')).not.toBeNull();
    expect(within(done).queryByRole('button', { name: 'Load more' })).toBeNull();

    fireEvent.click(within(open).getByRole('button', { name: 'Chat s1' }));
    expect(goTo).toHaveBeenCalledWith('conv-s1', null);
    fireEvent.click(within(open).getByRole('button', { name: 'Untitled chat' }));
    expect(goTo).toHaveBeenCalledWith('conv-p1', 'proj');
  });

  it('shows a dream\'s archive-only row with its source and no chat link', async () => {
    servePending([
      row('d1', '', {
        content: null, source: 'dream', dream_run_id: 'run-1', conversation_id: null,
        conversation_title: null, replaces_memory_id: 'm1', replaces_content: 'Prefers tea',
        reasoning: 'Duplicate of a newer memory.',
      }),
    ]);
    mocks.acceptMemorySuggestion.mockResolvedValue({
      suggestion: { ...row('d1', '', { content: null, status: 'accepted', resolved_at: '2026-10-29T00:00:00' }), memory_id: null },
      memory: null,
    });
    const goTo = vi.fn();
    render(<MemoryInbox onGoToConversation={goTo} />);

    const open = screen.getByRole('region', { name: /Open suggestions/ });
    await waitFor(() => expect(open.textContent).toContain('From a memory review'));
    expect(within(open).queryByRole('textbox')).toBeNull();
    expect(within(open).queryByRole('button', { name: /chat/i })).toBeNull();
    expect(screen.getByTestId('memory-suggestion-archive').textContent).toContain('Prefers tea');
    expect(open.textContent).toContain('Archives this memory');
    expect(open.textContent).toContain('Duplicate of a newer memory.');

    const accept = within(open).getByRole('button', { name: 'Accept' });
    expect((accept as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(accept);
    await waitFor(() => expect(mocks.acceptMemorySuggestion).toHaveBeenCalledWith('d1', undefined));
    const done = screen.getByRole('region', { name: 'Recently reviewed' });
    await waitFor(() => expect(done.textContent).toContain('Archived a memory'));
    expect(goTo).not.toHaveBeenCalled();
  });

  it('shows the empty states', async () => {
    servePending([]);
    render(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByText(/No open suggestions/);
    await screen.findByText('Nothing reviewed yet.');
  });

  it('accepts through the kind\'s endpoint and moves the row into the reviewed list', async () => {
    servePending([
      row('s1', 'Likes tea'),
      row('p1', 'Ships on Fridays', { kind: 'project', project_id: 'proj', project_name: 'Launch' }),
    ]);
    serveResolved([reviewed('r1', 'Older', 'dismissed')]);
    mocks.acceptMemorySuggestion.mockResolvedValue({
      suggestion: { ...row('s1', 'Likes tea'), status: 'accepted', resolved_at: '2026-10-15T00:00:00' },
      memory: {},
    });
    mocks.acceptProjectMemorySuggestion.mockResolvedValue({
      suggestion: { ...row('p1', 'Ships on Fridays'), status: 'accepted', resolved_at: '2026-10-15T00:00:01' },
      memory: {},
    });
    render(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Likes tea');

    // Edited text is sent and shown as reviewed.
    fireEvent.change(screen.getByDisplayValue('Likes tea'), { target: { value: 'Likes green tea ' } });
    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(screen.queryByDisplayValue('Likes green tea ')).toBeNull());
    expect(mocks.acceptMemorySuggestion).toHaveBeenCalledWith('s1', 'Likes green tea');

    const done = screen.getByRole('region', { name: 'Recently reviewed' });
    const contents = () => Array.from(done.querySelectorAll('.memory-inbox-reviewed-content')).map((el) => el.textContent);
    expect(contents()).toEqual(['Likes green tea', 'Older']);
    expect(within(done).getAllByText('Accepted')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(screen.queryByDisplayValue('Ships on Fridays')).toBeNull());
    expect(mocks.acceptProjectMemorySuggestion).toHaveBeenCalledWith('p1', undefined);
    expect(mocks.acceptMemorySuggestion).toHaveBeenCalledTimes(1);
    expect(contents()).toEqual(['Ships on Fridays', 'Likes green tea', 'Older']);
    expect(done.textContent).toContain('in Launch');
    expect(screen.getByText(/No open suggestions/)).not.toBeNull();
  });

  it('dismisses, surfaces a failure inline, and drops a row resolved elsewhere', async () => {
    servePending([row('s1', 'A'), row('s2', 'B'), row('s3', 'C')]);
    mocks.dismissMemorySuggestion
      .mockResolvedValueOnce({ suggestion: { ...row('s1', 'A'), status: 'dismissed', resolved_at: '2026-10-15T00:00:00' } })
      .mockRejectedValueOnce(new Error('network down'))
      .mockRejectedValueOnce(new ApiClientError('already dismissed', 409, 'suggestion_resolved'));
    render(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('A');

    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[0]);
    await waitFor(() => expect(screen.queryByDisplayValue('A')).toBeNull());
    const done = screen.getByRole('region', { name: 'Recently reviewed' });
    expect(done.textContent).toContain('A');
    expect(within(done).getByText('Dismissed')).not.toBeNull();

    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[0]);
    await screen.findByRole('alert');
    expect(screen.getByRole('alert').textContent).toBe('network down');
    expect(screen.getByDisplayValue('B')).not.toBeNull();

    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[1]);
    await waitFor(() => expect(screen.queryByDisplayValue('C')).toBeNull());
    // The 409 row is gone without an error, and not shown as reviewed here.
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(Array.from(done.querySelectorAll('.memory-inbox-reviewed-content')).map((el) => el.textContent))
      .toEqual(['A']);
  });

  it('pages the reviewed list in as the sentinel scrolls into view', async () => {
    vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
    servePending([]);
    mocks.fetchMemoryInboxResolved
      .mockResolvedValueOnce({ suggestions: [reviewed('r1', 'First', 'accepted')], next_cursor: 'c1' })
      .mockResolvedValueOnce({ suggestions: [reviewed('r2', 'Second', 'dismissed')], next_cursor: 'c2' })
      .mockResolvedValueOnce({ suggestions: [], next_cursor: null });
    render(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByText('First');
    expect(mocks.fetchMemoryInboxResolved).toHaveBeenCalledWith(null, RESOLVED_PAGE_SIZE);

    await waitFor(() => expect(FakeIntersectionObserver.instances).toHaveLength(1));
    act(() => FakeIntersectionObserver.instances[0].intersect());
    await screen.findByText('Second');
    expect(mocks.fetchMemoryInboxResolved).toHaveBeenLastCalledWith('c1', RESOLVED_PAGE_SIZE);

    // A new observer for the new cursor; the last page ends the stream.
    await waitFor(() => expect(FakeIntersectionObserver.instances).toHaveLength(1));
    act(() => FakeIntersectionObserver.instances[0].intersect());
    await waitFor(() => expect(mocks.fetchMemoryInboxResolved).toHaveBeenLastCalledWith('c2', RESOLVED_PAGE_SIZE));
    await waitFor(() => expect(FakeIntersectionObserver.instances).toHaveLength(0));
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('falls back to a Load more button without IntersectionObserver', async () => {
    servePending([]);
    mocks.fetchMemoryInboxResolved
      .mockResolvedValueOnce({ suggestions: [reviewed('r1', 'First', 'accepted')], next_cursor: 'c1' })
      .mockResolvedValueOnce({ suggestions: [reviewed('r2', 'Second', 'accepted')], next_cursor: null });
    render(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByText('First');
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await screen.findByText('Second');
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('re-reads the open list on a suggestions event', async () => {
    servePending([row('s1', 'A')]);
    render(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('A');

    servePending([row('s1', 'A'), row('p1', 'P', { kind: 'project' })]);
    emit({ type: 'project_memory_suggestions_changed', conversation_id: 'x', pending_count: 1, total_pending: 1 });
    await screen.findByDisplayValue('P');
    expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2);

    // Unrelated globals are ignored.
    emit({ type: 'request_count_changed', counts: { open: 1 } });
    expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2);
  });

  it.each(['accept', 'dismiss'] as const)(
    'leaves doc note rows out of the open list, its count and %s all',
    async (action) => {
      servePending([
        row('s1', 'Likes tea'),
        docRow('d1', 'Cite sources', { note_kind: 'instructions', base_instructions_version: 2 }),
        row('p1', 'Ships on Fridays', { kind: 'project', project_id: 'proj', project_name: 'Launch' }),
        docRow('d2', 'Uses metric units'),
      ]);
      const resolvedAs = (s: MemoryInboxSuggestion) => ({
        suggestion: { ...s, status: action === 'accept' ? 'accepted' : 'dismissed', resolved_at: '2026-10-15T00:00:00' },
        memory: {},
      });
      mocks.acceptMemorySuggestion.mockResolvedValue(resolvedAs(row('s1', 'Likes tea')));
      mocks.dismissMemorySuggestion.mockResolvedValue(resolvedAs(row('s1', 'Likes tea')));
      mocks.acceptProjectMemorySuggestion.mockResolvedValue(resolvedAs(row('p1', 'Ships on Fridays')));
      mocks.dismissProjectMemorySuggestion.mockResolvedValue(resolvedAs(row('p1', 'Ships on Fridays')));
      render(<MemoryInbox onGoToConversation={vi.fn()} />);
      await screen.findByDisplayValue('Likes tea');

      const open = screen.getByRole('region', { name: /Open suggestions/ });
      expect(within(open).getAllByRole('textbox').map((el) => (el as HTMLTextAreaElement).value))
        .toEqual(['Likes tea', 'Ships on Fridays']);
      expect(open.querySelector('.memory-inbox-section-count')?.textContent).toBe('2');
      expect(screen.queryByDisplayValue('Cite sources')).toBeNull();
      expect(screen.queryByDisplayValue('Uses metric units')).toBeNull();
      for (const text of ['Why d1', 'Doc d1', 'Chat d1', 'Chat d2']) {
        expect(open.textContent).not.toContain(text);
      }

      fireEvent.click(within(open).getByRole('button', { name: action === 'accept' ? 'Accept all' : 'Dismiss all' }));
      await screen.findByText(/No open suggestions/);

      const calls = [
        mocks.acceptMemorySuggestion,
        mocks.dismissMemorySuggestion,
        mocks.acceptProjectMemorySuggestion,
        mocks.dismissProjectMemorySuggestion,
      ].flatMap((mock) => mock.mock.calls.map((args) => args[0]));
      expect(calls.sort()).toEqual(['p1', 's1']);
      const done = screen.getByRole('region', { name: 'Recently reviewed' });
      expect(Array.from(done.querySelectorAll('.memory-inbox-reviewed-content')).map((el) => el.textContent))
        .toEqual(['Ships on Fridays', 'Likes tea']);
      expect(screen.queryAllByRole('alert')).toHaveLength(0);
    },
  );

  it('leaves doc note rows out of the reviewed list, page after page', async () => {
    servePending([]);
    mocks.fetchMemoryInboxResolved
      .mockResolvedValueOnce({
        suggestions: [
          reviewed('r1', 'First', 'accepted'),
          docRow('d1', 'Cite sources', { status: 'accepted', resolved_at: '2026-10-14T11:00:00' }),
        ],
        next_cursor: 'c1',
      })
      .mockResolvedValueOnce({
        suggestions: [
          docRow('d2', 'Uses metric units', { status: 'dismissed', resolved_at: '2026-10-14T10:00:00' }),
          reviewed('r2', 'Second', 'dismissed'),
        ],
        next_cursor: null,
      });
    render(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByText('First');
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await screen.findByText('Second');

    const done = screen.getByRole('region', { name: 'Recently reviewed' });
    expect(Array.from(done.querySelectorAll('.memory-inbox-reviewed-content')).map((el) => el.textContent))
      .toEqual(['First', 'Second']);
    expect(done.querySelectorAll('.memory-inbox-reviewed')).toHaveLength(2);
    for (const text of ['Cite sources', 'Uses metric units', 'Doc d1', 'Chat d1', 'Chat d2']) {
      expect(done.textContent).not.toContain(text);
    }
  });

  it('does not call the reviewed list empty while a page of left-out rows has a cursor', async () => {
    servePending([]);
    mocks.fetchMemoryInboxResolved
      .mockResolvedValueOnce({
        suggestions: [docRow('d1', 'Cite sources', { status: 'accepted', resolved_at: '2026-10-14T11:00:00' })],
        next_cursor: 'c1',
      })
      .mockResolvedValueOnce({ suggestions: [], next_cursor: null });
    render(<MemoryInbox onGoToConversation={vi.fn()} />);
    const loadMore = await screen.findByRole('button', { name: 'Load more' });
    expect(screen.queryByText('Nothing reviewed yet.')).toBeNull();

    // The last page (no cursor) settles it.
    fireEvent.click(loadMore);
    await screen.findByText('Nothing reviewed yet.');
    expect(screen.queryByText('Cite sources')).toBeNull();
  });
});
