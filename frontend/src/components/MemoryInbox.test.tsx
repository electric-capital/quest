// The inbox's Memories tab: open personal, project and doc note suggestions
// with their source line, accept / dismiss through the kind's endpoint
// moving the row into the reviewed list, a 409 dropping the row silently,
// the reviewed list paging in through the scroll sentinel (and the Load
// more fallback), a per-user global re-reading the open list, and rows of
// a kind this tab does not know left out of both lists. Doc rows add their
// doc link (or "Unavailable doc"), the instructions version on accept, the
// re-base on a 409 stale_instructions, the read the accept's own event
// starts kept (it re-bases a second instructions row on the same doc), the
// server's message on any other refusal, the Instructions / Memory pill on
// reviewed rows, and an Accept all that skips a doc row its own Accept
// refuses.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter, useLocation } from 'react-router-dom';
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
  acceptDocNoteSuggestion: vi.fn(),
  dismissDocNoteSuggestion: vi.fn(),
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

// Only the doc note calls are faked; the conflict helpers and caps stay real.
vi.mock('../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/docsApi')>()),
  acceptDocNoteSuggestion: mocks.acceptDocNoteSuggestion,
  dismissDocNoteSuggestion: mocks.dismissDocNoteSuggestion,
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
    current_instructions_version: null,
    instructions_moved: false,
    kind: 'doc',
    project_id: null,
    project_name: null,
    conversation_title: `Chat ${id}`,
    ...extra,
  };
}

/** A pending instructions row whose diff starts from the doc's current text. */
function docInstructions(
  id: string,
  content: string,
  extra: Partial<DocNoteInboxSuggestion> = {},
): DocNoteInboxSuggestion {
  return docRow(id, content, {
    note_kind: 'instructions',
    base_instructions_version: 3,
    current_instructions: 'Prices are in USD.',
    current_instructions_version: 3,
    ...extra,
  });
}

/** The doc note row as the accept / dismiss routes return it: no review fields. */
function resolvedDoc(s: DocNoteInboxSuggestion, status: 'accepted' | 'dismissed') {
  return {
    suggestion: {
      id: s.id,
      user_id: s.user_id,
      doc_id: s.doc_id,
      conversation_id: s.conversation_id,
      note_kind: s.note_kind,
      content: s.content,
      reasoning: s.reasoning,
      base_instructions_version: s.base_instructions_version,
      replaces_memory_id: s.replaces_memory_id,
      status,
      memory_id: status === 'accepted' && s.note_kind === 'memory' ? `m-${s.id}` : null,
      created_at: s.created_at,
      resolved_at: '2026-10-15T00:00:00',
    },
    memory: null,
  };
}

const STALE_MESSAGE = "The doc's instructions changed since this suggestion was shown. "
  + 'Review it against the current text and accept again.';

/** The flat 409 stale_instructions as request.ts maps it. */
function staleError(current: { instructions: string; instructions_version: number } | null) {
  const body: Record<string, unknown> = { error: 'stale_instructions', message: STALE_MESSAGE };
  if (current) body.current = current;
  return new ApiClientError(STALE_MESSAGE, 409, 'stale_instructions', undefined, body);
}

/** Where the router is, so a test can see a doc link navigate. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

// Doc rows link to their doc, so the inbox renders inside a router.
function renderInbox(ui: ReactElement) {
  return render(
    <MemoryRouter initialEntries={['/inbox/memories']}>
      {ui}
      <LocationProbe />
    </MemoryRouter>,
  );
}

function openRegion() {
  return screen.getByRole('region', { name: /Open suggestions/ });
}

function reviewedRegion() {
  return screen.getByRole('region', { name: 'Recently reviewed' });
}

function reviewedContents(): (string | null)[] {
  return Array.from(reviewedRegion().querySelectorAll('.memory-inbox-reviewed-content'))
    .map((el) => el.textContent);
}

function textboxValues(container: HTMLElement): string[] {
  return within(container).getAllByRole('textbox').map((el) => (el as HTMLTextAreaElement).value);
}

function diffBodies(): Element[] {
  return screen.getAllByTestId('memory-suggestion-diff')
    .map((diff) => diff.querySelector('.memory-suggestion-diff-body')!);
}

type PendingList = { suggestions: MemoryInboxRow[]; pending_count: number };

/** A promise the test settles by hand. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
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
  mocks.acceptDocNoteSuggestion.mockReset();
  mocks.dismissDocNoteSuggestion.mockReset();
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
    renderInbox(<MemoryInbox onGoToConversation={goTo} />);

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
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
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
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
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
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
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
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
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
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByText('First');
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await screen.findByText('Second');
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('re-reads the open list on a suggestions event', async () => {
    servePending([row('s1', 'A')]);
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('A');

    servePending([row('s1', 'A'), row('p1', 'P', { kind: 'project' })]);
    emit({ type: 'project_memory_suggestions_changed', conversation_id: 'x', pending_count: 1, total_pending: 1 });
    await screen.findByDisplayValue('P');
    expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2);

    // Unrelated globals are ignored.
    emit({ type: 'request_count_changed', counts: { open: 1 } });
    expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2);
  });

  it('lists doc note rows among the open ones with their doc and conversation', async () => {
    servePending([
      row('s1', 'Likes tea'),
      docInstructions('d1', 'Cite sources.'),
      row('p1', 'Ships on Fridays', { kind: 'project', project_id: 'proj', project_name: 'Launch' }),
      docRow('d2', 'Uses metric units', { project_id: 'proj', project_name: 'Launch' }),
    ]);
    const goTo = vi.fn();
    renderInbox(<MemoryInbox onGoToConversation={goTo} />);
    await screen.findByDisplayValue('Uses metric units');

    // The server's merged order, all four counted.
    const open = openRegion();
    expect(textboxValues(open)).toEqual(['Likes tea', 'Cite sources.', 'Ships on Fridays', 'Uses metric units']);
    expect(open.querySelector('.memory-inbox-section-count')?.textContent).toBe('4');
    expect(open.querySelectorAll('.memory-suggestion-row--doc')).toHaveLength(2);
    expect(screen.getByLabelText('Doc instructions text')).not.toBeNull();
    expect(screen.getByLabelText('Doc memory text')).not.toBeNull();

    // The row's own pill says which note; the source line says what and where.
    const docRows = Array.from(open.querySelectorAll<HTMLElement>('.memory-suggestion-row--doc'));
    expect(docRows.map((el) => el.querySelector('.memory-suggestion-pill')?.textContent))
      .toEqual(['Instructions', 'Memory']);
    const kindPills = open.querySelectorAll('.memory-inbox-kind--doc');
    expect(Array.from(kindPills).map((el) => el.textContent)).toEqual(['Doc note', 'Doc note']);
    expect(kindPills[0].getAttribute('title')).toBe('A doc note: instructions or a memory for one doc');
    expect(docRows[0].textContent).toContain('Why d1');
    expect(docRows[1].textContent).toContain('in Launch');

    // One link per doc (the source line's; the row hides its own).
    expect(within(open).getAllByRole('link', { name: 'Doc d1' })).toHaveLength(1);
    expect(within(open).getByRole('link', { name: 'Doc d1' }).getAttribute('href')).toBe('/docs/doc-d1');
    expect(within(open).getByRole('link', { name: 'Doc d2' }).getAttribute('href')).toBe('/docs/doc-d2');
    expect(open.querySelector('.memory-suggestion-doc-link')).toBeNull();

    fireEvent.click(within(docRows[0]).getByRole('button', { name: 'Chat d1' }));
    expect(goTo).toHaveBeenCalledWith('conv-d1', null);
    fireEvent.click(within(docRows[1]).getByRole('button', { name: 'Chat d2' }));
    expect(goTo).toHaveBeenCalledWith('conv-d2', 'proj');
  });

  it('shows "Unavailable doc" without a link for a doc that is gone or hidden', async () => {
    servePending([docRow('d1', 'Uses metric units', { doc_title: '' })]);
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Uses metric units');

    const open = openRegion();
    const unavailable = within(open).getByText('Unavailable doc');
    expect(unavailable.getAttribute('title')).toBe('This doc was deleted or you no longer have access to it');
    expect(within(open).queryAllByRole('link')).toHaveLength(0);
    expect(within(open).getByRole('button', { name: 'Chat d1' })).not.toBeNull();
  });

  it('calls onOpenDoc on a plain click of the doc link, which then opens the doc', async () => {
    servePending([docRow('d1', 'Uses metric units')]);
    const onOpenDoc = vi.fn();
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} onOpenDoc={onOpenDoc} />);
    const link = await screen.findByRole('link', { name: 'Doc d1' });

    // jsdom cannot open the new tab a modified click asks the browser for.
    link.addEventListener('click', (e) => {
      if (e.ctrlKey) e.preventDefault();
    });
    fireEvent.click(link, { ctrlKey: true });
    expect(onOpenDoc).not.toHaveBeenCalled();
    expect(screen.getByTestId('location').textContent).toBe('/inbox/memories');

    fireEvent.click(link);
    expect(onOpenDoc).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('location').textContent).toBe('/docs/doc-d1');
  });

  it('accepts each kind through its own endpoint, a doc row with its instructions version', async () => {
    const d1 = docInstructions('d1', 'Prices are in EUR.');
    const d2 = docRow('d2', 'Uses metric units');
    servePending([row('s1', 'Likes tea'), d1, d2]);
    serveResolved([reviewed('r1', 'Older', 'dismissed')]);
    mocks.acceptMemorySuggestion.mockResolvedValue({
      suggestion: { ...row('s1', 'Likes tea'), status: 'accepted', resolved_at: '2026-10-15T00:00:00' },
      memory: {},
    });
    mocks.acceptDocNoteSuggestion
      .mockResolvedValueOnce(resolvedDoc(d1, 'accepted'))
      .mockResolvedValueOnce(resolvedDoc(d2, 'accepted'));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Uses metric units');

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(screen.queryByDisplayValue('Likes tea')).toBeNull());
    expect(mocks.acceptMemorySuggestion).toHaveBeenCalledWith('s1', undefined);
    expect(mocks.acceptDocNoteSuggestion).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR.')).toBeNull());
    expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledWith('d1', { expectedInstructionsVersion: 3 });

    // An edited memory sends its text, and no version.
    fireEvent.change(screen.getByDisplayValue('Uses metric units'), {
      target: { value: 'Uses metric units only. ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(screen.queryByDisplayValue('Uses metric units only. ')).toBeNull());
    expect(mocks.acceptDocNoteSuggestion).toHaveBeenLastCalledWith('d2', { content: 'Uses metric units only.' });
    expect(mocks.acceptMemorySuggestion).toHaveBeenCalledTimes(1);
    expect(mocks.acceptProjectMemorySuggestion).not.toHaveBeenCalled();
    expect(screen.queryAllByRole('alert')).toHaveLength(0);

    // The accepted doc rows lead the reviewed list with the saved text and
    // keep their doc and conversation from the row (the response has neither).
    expect(reviewedContents()).toEqual(['Uses metric units only.', 'Prices are in EUR.', 'Likes tea', 'Older']);
    const done = reviewedRegion();
    expect(within(done).getAllByText('Accepted')).toHaveLength(3);
    expect(within(done).getAllByText('Doc note')).toHaveLength(2);
    expect(Array.from(done.querySelectorAll('.memory-inbox-note-pill')).map((el) => el.textContent))
      .toEqual(['Memory', 'Instructions']);
    expect(within(done).getByRole('link', { name: 'Doc d1' }).getAttribute('href')).toBe('/docs/doc-d1');
    expect(within(done).getByRole('link', { name: 'Doc d2' }).getAttribute('href')).toBe('/docs/doc-d2');
    expect(within(done).getByRole('button', { name: 'Chat d2' })).not.toBeNull();
    expect(screen.getByText(/No open suggestions/)).not.toBeNull();
  });

  it('dismisses a doc row through the doc note endpoint', async () => {
    const d1 = docInstructions('d1', 'Prices are in EUR.');
    servePending([row('s1', 'Likes tea'), d1]);
    mocks.dismissDocNoteSuggestion.mockResolvedValue(resolvedDoc(d1, 'dismissed'));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Prices are in EUR.');

    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[1]);
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR.')).toBeNull());
    expect(mocks.dismissDocNoteSuggestion).toHaveBeenCalledWith('d1');
    expect(mocks.dismissMemorySuggestion).not.toHaveBeenCalled();
    expect(mocks.acceptDocNoteSuggestion).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue('Likes tea')).not.toBeNull();

    const done = reviewedRegion();
    expect(reviewedContents()).toEqual(['Prices are in EUR.']);
    expect(within(done).getByText('Dismissed')).not.toBeNull();
    expect(within(done).getByRole('link', { name: 'Doc d1' })).not.toBeNull();
  });

  it('re-bases an instructions row on the current text of a 409 stale_instructions', async () => {
    const d1 = docInstructions('d1', 'Prices are in EUR.');
    servePending([d1]);
    mocks.acceptDocNoteSuggestion
      .mockRejectedValueOnce(staleError({ instructions: 'Prices are in GBP.', instructions_version: 4 }))
      .mockResolvedValueOnce(resolvedDoc(d1, 'accepted'));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    const textarea = await screen.findByDisplayValue('Prices are in EUR.');
    expect(diffBodies()[0].textContent).toBe('Prices are in USD. EUR.');
    fireEvent.change(textarea, { target: { value: 'Prices are in EUR, always.' } });

    // What the re-read after the conflict returns: the same current text.
    servePending([docInstructions('d1', 'Prices are in EUR.', {
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
    // The diff starts from the current text, the draft is kept, and the row
    // says the instructions moved past the proposal's base.
    const body = diffBodies()[0];
    expect(body.querySelector('.memory-suggestion-diff-del')!.textContent).toBe('GBP.');
    expect(body.querySelector('.memory-suggestion-diff-add')!.textContent).toBe('EUR, always.');
    expect(screen.getByDisplayValue('Prices are in EUR, always.')).not.toBeNull();
    expect(document.querySelector('.memory-suggestion-moved')!.textContent)
      .toBe('The instructions changed since this was suggested');
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2));
    expect(screen.getByDisplayValue('Prices are in EUR, always.')).not.toBeNull();
    expect(screen.getByRole('alert').textContent).toBe(STALE_MESSAGE);
    expect(reviewedContents()).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenLastCalledWith(
      'd1',
      { content: 'Prices are in EUR, always.', expectedInstructionsVersion: 4 },
    ));
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR, always.')).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(reviewedContents()).toEqual(['Prices are in EUR, always.']);
    expect(within(reviewedRegion()).getByRole('link', { name: 'Doc d1' })).not.toBeNull();
  });

  it('re-reads after the re-base, past a list read in flight when the 409 lands', async () => {
    servePending([docInstructions('d1', 'Prices are in EUR.')]);
    const accept = deferred<unknown>();
    mocks.acceptDocNoteSuggestion
      .mockImplementationOnce(() => accept.promise)
      .mockResolvedValueOnce(resolvedDoc(docInstructions('d1', 'Prices are in EUR.'), 'accepted'));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Prices are in EUR.');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledTimes(1));
    // An event starts a list read that is still in flight (with the old
    // row) when the 409 lands...
    const stale = deferred<PendingList>();
    mocks.fetchMemoryInboxPending.mockImplementationOnce(() => stale.promise);
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'x', pending_count: 2, total_pending: 2 });
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2));

    // ...and the re-read after the conflict brings the newest list.
    const fresh = deferred<PendingList>();
    mocks.fetchMemoryInboxPending.mockImplementationOnce(() => fresh.promise);
    await act(async () => {
      accept.reject(staleError({ instructions: 'Prices are in GBP.', instructions_version: 4 }));
    });
    expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('GBP.');
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(3));

    await act(async () => {
      fresh.resolve({
        suggestions: [
          docInstructions('d1', 'Prices are in EUR.', {
            current_instructions: 'Prices are in CHF.', current_instructions_version: 5,
          }),
          docRow('d2', 'The board meets monthly.'),
        ],
        pending_count: 2,
      });
    });
    await screen.findByDisplayValue('The board meets monthly.');
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('CHF.');
    // The read that was in flight lands last and changes nothing.
    await act(async () => {
      stale.resolve({ suggestions: [docInstructions('d1', 'Prices are in EUR.')], pending_count: 1 });
    });
    expect(screen.getByDisplayValue('The board meets monthly.')).not.toBeNull();
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('CHF.');

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenLastCalledWith('d1', { expectedInstructionsVersion: 5 }));
  });

  it('drops a read in flight when the 409 lands, even one landing before the re-read', async () => {
    servePending([docInstructions('d1', 'Prices are in EUR.')]);
    const accept = deferred<unknown>();
    mocks.acceptDocNoteSuggestion.mockImplementationOnce(() => accept.promise);
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Prices are in EUR.');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledTimes(1));
    const stale = deferred<PendingList>();
    mocks.fetchMemoryInboxPending.mockImplementationOnce(() => stale.promise);
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'x', pending_count: 1, total_pending: 1 });
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2));

    const fresh = deferred<PendingList>();
    mocks.fetchMemoryInboxPending.mockImplementationOnce(() => fresh.promise);
    await act(async () => {
      accept.reject(staleError({ instructions: 'Prices are in GBP.', instructions_version: 4 }));
    });
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(3));
    // The read from before the 409 lands first, with the old row: dropped.
    await act(async () => {
      stale.resolve({ suggestions: [docInstructions('d1', 'Prices are in EUR.')], pending_count: 1 });
    });
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('GBP.');
    await act(async () => {
      fresh.resolve({
        suggestions: [docInstructions('d1', 'Prices are in EUR.', {
          current_instructions: 'Prices are in GBP.', current_instructions_version: 4,
        })],
        pending_count: 1,
      });
    });
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('GBP.');
  });

  it('keeps a newer row that a list read brought in during the accept', async () => {
    servePending([docInstructions('d1', 'Prices are in EUR.')]);
    const accept = deferred<unknown>();
    mocks.acceptDocNoteSuggestion.mockImplementationOnce(() => accept.promise);
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Prices are in EUR.');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledTimes(1));
    servePending([docInstructions('d1', 'Prices are in EUR.', {
      current_instructions: 'Prices are in CHF.', current_instructions_version: 5,
    })]);
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'x', pending_count: 1, total_pending: 1 });
    await waitFor(() => expect(
      diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent,
    ).toBe('CHF.'));

    // The 409 carries an older current text (version 4): no roll-back.
    const reread = deferred<PendingList>();
    mocks.fetchMemoryInboxPending.mockImplementationOnce(() => reread.promise);
    await act(async () => {
      accept.reject(staleError({ instructions: 'Prices are in GBP.', instructions_version: 4 }));
    });
    expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
    expect(diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent).toBe('CHF.');
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(3));
  });

  it('re-reads the open list when a 409 stale_instructions carries no current text', async () => {
    servePending([docInstructions('d1', 'Prices are in EUR.')]);
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(staleError(null));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    const textarea = await screen.findByDisplayValue('Prices are in EUR.');
    fireEvent.change(textarea, { target: { value: 'Prices are in EUR, always.' } });

    servePending([docInstructions('d1', 'Prices are in EUR.', {
      current_instructions: 'Prices are in GBP.',
      current_instructions_version: 6,
    })]);
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(
      diffBodies()[0].querySelector('.memory-suggestion-diff-del')!.textContent,
    ).toBe('GBP.'));
    expect(screen.getByDisplayValue('Prices are in EUR, always.')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion).toHaveBeenLastCalledWith(
      'd1',
      { content: 'Prices are in EUR, always.', expectedInstructionsVersion: 6 },
    ));
  });

  it("keeps the read the accept's own event starts, re-basing a second instructions row", async () => {
    // Two instructions proposals for one doc, both diffed against version 3.
    const d1 = docInstructions('d1', 'Prices are in EUR.');
    const d2 = docInstructions('d2', 'Prices are in GBP.', { doc_id: 'doc-d1' });
    servePending([d1, d2]);
    const accept = deferred<unknown>();
    mocks.acceptDocNoteSuggestion
      .mockImplementationOnce(() => accept.promise)
      .mockResolvedValueOnce(resolvedDoc(d2, 'accepted'));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Prices are in GBP.');

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[0]);
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenCalledWith('d1', { expectedInstructionsVersion: 3 }));
    // The server publishes the event inside the accept: the read it starts
    // is still in flight when the accept's response lands...
    const eventRead = deferred<PendingList>();
    mocks.fetchMemoryInboxPending.mockImplementationOnce(() => eventRead.promise);
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'conv-d1', pending_count: 1, total_pending: 1 });
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2));
    await act(async () => {
      accept.resolve(resolvedDoc(d1, 'accepted'));
    });
    await waitFor(() => expect(screen.queryByDisplayValue('Prices are in EUR.')).toBeNull());
    expect(reviewedContents()).toEqual(['Prices are in EUR.']);

    // ...and still lands: the second row's diff starts from the accepted text.
    await act(async () => {
      eventRead.resolve({
        suggestions: [docInstructions('d2', 'Prices are in GBP.', {
          doc_id: 'doc-d1',
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
    expect(document.querySelector('.memory-suggestion-moved')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(mocks.acceptDocNoteSuggestion)
      .toHaveBeenLastCalledWith('d2', { expectedInstructionsVersion: 4 }));
    expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2);
  });

  it('does not bring back a row resolved here when an older read lands', async () => {
    servePending([row('s1', 'Likes tea'), docRow('d1', 'Uses metric units')]);
    mocks.dismissMemorySuggestion.mockResolvedValue({
      suggestion: { ...row('s1', 'Likes tea'), status: 'dismissed', resolved_at: '2026-10-15T00:00:00' },
    });
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Likes tea');

    // A read starts before the dismiss and lands after it.
    const older = deferred<PendingList>();
    mocks.fetchMemoryInboxPending.mockImplementationOnce(() => older.promise);
    emit({ type: 'memory_suggestions_changed', conversation_id: 'conv-s1', pending_count: 1, total_pending: 1 });
    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[0]);
    await waitFor(() => expect(screen.queryByDisplayValue('Likes tea')).toBeNull());
    await act(async () => {
      older.resolve({
        suggestions: [row('s1', 'Likes tea'), docRow('d1', 'Uses metric units'), docRow('d2', 'Q3 ends in September.')],
        pending_count: 3,
      });
    });
    expect(screen.queryByDisplayValue('Likes tea')).toBeNull();
    expect(textboxValues(openRegion())).toEqual(['Uses metric units', 'Q3 ends in September.']);
  });

  it.each([
    ['forbidden', 403, "Only the doc's owner can change its instructions."],
    ['duplicate_memory', 409, 'This doc already has a memory with exactly this text.'],
    ['memories_cap_reached', 409, 'This doc already has the maximum of 50 memories. Delete or edit one instead.'],
  ])('keeps the doc row and shows the server message on %s', async (code, status, message) => {
    servePending([docInstructions('d1', 'Prices are in EUR.'), docRow('d2', 'Uses metric units')]);
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(new ApiClientError(message, status, code));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Uses metric units');

    fireEvent.click(screen.getAllByRole('button', { name: 'Accept' })[code === 'forbidden' ? 0 : 1]);
    expect((await screen.findByRole('alert')).textContent).toBe(message);
    expect(textboxValues(openRegion())).toEqual(['Prices are in EUR.', 'Uses metric units']);
    expect(reviewedContents()).toEqual([]);
    // Not a conflict: no re-base and no re-read.
    expect(diffBodies()[0].textContent).toBe('Prices are in USD. EUR.');
    expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(1);
  });

  it('falls back to "Failed to save doc note" for an error without a message', async () => {
    servePending([docRow('d1', 'Uses metric units')]);
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(new ApiClientError('', 500, 'server_error'));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Uses metric units');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    expect((await screen.findByRole('alert')).textContent).toBe('Failed to save doc note');
    expect(screen.getByDisplayValue('Uses metric units')).not.toBeNull();
  });

  it('drops a doc row resolved elsewhere without an error', async () => {
    servePending([docRow('d1', 'Uses metric units')]);
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(
      new ApiClientError('already resolved', 409, 'suggestion_resolved'),
    );
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Uses metric units');
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(screen.queryByDisplayValue('Uses metric units')).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(reviewedContents()).toEqual([]);
  });

  it.each(['accept', 'dismiss'] as const)('%s all reaches doc rows too', async (action) => {
    const d1 = docInstructions('d1', 'Cite sources.');
    const d2 = docRow('d2', 'Uses metric units');
    servePending([
      row('s1', 'Likes tea'),
      d1,
      row('p1', 'Ships on Fridays', { kind: 'project', project_id: 'proj', project_name: 'Launch' }),
      d2,
    ]);
    const status = action === 'accept' ? 'accepted' : 'dismissed';
    const resolvedAs = (s: MemoryInboxSuggestion) => ({
      suggestion: { ...s, status, resolved_at: '2026-10-15T00:00:00' },
      memory: {},
    });
    const memoryMock = action === 'accept' ? mocks.acceptMemorySuggestion : mocks.dismissMemorySuggestion;
    const projectMock = action === 'accept' ? mocks.acceptProjectMemorySuggestion : mocks.dismissProjectMemorySuggestion;
    const docMock = action === 'accept' ? mocks.acceptDocNoteSuggestion : mocks.dismissDocNoteSuggestion;
    memoryMock.mockResolvedValue(resolvedAs(row('s1', 'Likes tea')));
    projectMock.mockResolvedValue(resolvedAs(row('p1', 'Ships on Fridays')));
    docMock
      .mockResolvedValueOnce(resolvedDoc(d1, status))
      .mockResolvedValueOnce(resolvedDoc(d2, status));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Uses metric units');

    fireEvent.click(within(openRegion()).getByRole('button', { name: action === 'accept' ? 'Accept all' : 'Dismiss all' }));
    await screen.findByText(/No open suggestions/);

    expect(memoryMock.mock.calls.map(([id]) => id)).toEqual(['s1']);
    expect(projectMock.mock.calls.map(([id]) => id)).toEqual(['p1']);
    expect(docMock.mock.calls.map(([id]) => id)).toEqual(['d1', 'd2']);
    if (action === 'accept') {
      expect(docMock).toHaveBeenCalledWith('d1', { expectedInstructionsVersion: 3 });
    }
    // Newest first: the last one resolved leads.
    expect(reviewedContents()).toEqual(['Uses metric units', 'Ships on Fridays', 'Cite sources.', 'Likes tea']);
    expect(screen.queryAllByRole('alert')).toHaveLength(0);
  });

  it('accept all skips a doc row its own Accept refuses', async () => {
    const d2 = docRow('d2', 'Uses metric units');
    const d3 = docRow('d3', 'Q3 ends in September.', { doc_title: '' });
    servePending([docInstructions('d1', 'Cite sources.'), d2, d3]);
    mocks.acceptDocNoteSuggestion.mockResolvedValue(resolvedDoc(d2, 'accepted'));
    mocks.dismissDocNoteSuggestion.mockResolvedValue(resolvedDoc(d3, 'dismissed'));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Uses metric units');
    // A blank draft would clear the doc's instructions, and a doc that is
    // gone or hidden takes no note: Accept all leaves both.
    fireEvent.change(screen.getByLabelText('Doc instructions text'), { target: { value: '  ' } });

    fireEvent.click(screen.getByRole('button', { name: 'Accept all' }));
    await waitFor(() => expect(screen.queryByDisplayValue('Uses metric units')).toBeNull());
    expect(mocks.acceptDocNoteSuggestion.mock.calls.map(([id]) => id)).toEqual(['d2']);
    expect(screen.getByLabelText('Doc instructions text')).not.toBeNull();
    expect(screen.getByDisplayValue('Q3 ends in September.')).not.toBeNull();
    expect(screen.queryAllByRole('alert')).toHaveLength(0);

    // The unavailable doc's row can still be dismissed.
    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[1]);
    await waitFor(() => expect(screen.queryByDisplayValue('Q3 ends in September.')).toBeNull());
    expect(mocks.dismissDocNoteSuggestion).toHaveBeenCalledWith('d3');
  });

  it('renders reviewed doc rows with their status, text and source line', async () => {
    servePending([]);
    serveResolved([
      docRow('d1', 'Uses metric units', { status: 'accepted', resolved_at: '2026-10-14T12:00:00' }),
      reviewed('r1', 'Likes tea', 'accepted'),
      docRow('d2', 'Cite sources.', {
        note_kind: 'instructions', status: 'dismissed', resolved_at: '2026-10-14T11:00:00', doc_title: '',
      }),
    ]);
    const onOpenDoc = vi.fn();
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} onOpenDoc={onOpenDoc} />);
    await waitFor(() => expect(reviewedContents()).toEqual(['Uses metric units', 'Likes tea', 'Cite sources.']));

    const items = Array.from(reviewedRegion().querySelectorAll<HTMLElement>('.memory-inbox-reviewed'));
    expect(items.map((el) => el.querySelector('.memory-inbox-status-pill')?.textContent))
      .toEqual(['Accepted', 'Accepted', 'Dismissed']);
    expect(items.map((el) => el.querySelector('.memory-inbox-kind')?.textContent))
      .toEqual(['Doc note', 'Memory', 'Doc note']);
    // A doc note's header says which note it was; a memory's has no such pill.
    const notePills = items.map((el) => el.querySelector('.memory-inbox-reviewed-header .memory-inbox-note-pill'));
    expect(notePills.map((el) => el?.textContent ?? null)).toEqual(['Memory', null, 'Instructions']);
    expect(notePills[0]!.getAttribute('title')).toBe('A doc memory: a fact models read before they change the doc');
    expect(notePills[2]!.getAttribute('title'))
      .toBe("Instructions: the doc's rules, which models follow before they change it");
    expect(items.map((el) => el.classList.contains('memory-inbox-reviewed--doc'))).toEqual([true, false, true]);
    expect(within(items[0]).getByRole('link', { name: 'Doc d1' }).getAttribute('href')).toBe('/docs/doc-d1');
    expect(within(items[0]).getByRole('button', { name: 'Chat d1' })).not.toBeNull();
    expect(within(items[2]).getByText('Unavailable doc')).not.toBeNull();
    expect(within(items[2]).queryAllByRole('link')).toHaveLength(0);
    expect(within(items[2]).getByRole('button', { name: 'Chat d2' })).not.toBeNull();

    fireEvent.click(within(items[0]).getByRole('link', { name: 'Doc d1' }));
    expect(onOpenDoc).toHaveBeenCalledTimes(1);
  });

  it('drops an older read that lands after a newer one', async () => {
    servePending([row('s1', 'A')]);
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('A');

    const older = deferred<PendingList>();
    const newer = deferred<PendingList>();
    mocks.fetchMemoryInboxPending
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => newer.promise);
    emit({ type: 'memory_suggestions_changed', conversation_id: 'x', pending_count: 2, total_pending: 2 });
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'x', pending_count: 1, total_pending: 1 });
    await waitFor(() => expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(3));

    await act(async () => {
      newer.resolve({ suggestions: [row('s1', 'A'), docRow('d1', 'Uses metric units')], pending_count: 2 });
    });
    await screen.findByDisplayValue('Uses metric units');
    await act(async () => {
      older.resolve({ suggestions: [row('s1', 'A')], pending_count: 1 });
    });
    expect(textboxValues(openRegion())).toEqual(['A', 'Uses metric units']);
  });

  it('re-reads the open list on a doc note suggestions event', async () => {
    servePending([row('s1', 'A')]);
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('A');

    servePending([row('s1', 'A'), docRow('d1', 'Uses metric units')]);
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'x', pending_count: 1, total_pending: 1 });
    await screen.findByDisplayValue('Uses metric units');
    expect(mocks.fetchMemoryInboxPending).toHaveBeenCalledTimes(2);
  });

  it('leaves rows of a kind it has no endpoints for out of both lists', async () => {
    // A kind the server could add before this tab knows it.
    const future = (id: string, content: string, extra: Partial<MemoryInboxSuggestion> = {}) => (
      { ...row(id, content, extra), kind: 'future' } as unknown as MemoryInboxRow
    );
    servePending([row('s1', 'Likes tea'), future('f1', 'Unknown thing'), docRow('d1', 'Uses metric units')]);
    serveResolved([
      reviewed('r1', 'First', 'accepted'),
      future('f2', 'Old unknown', { status: 'accepted', resolved_at: '2026-10-14T11:00:00' }),
    ]);
    mocks.acceptMemorySuggestion.mockResolvedValue({
      suggestion: { ...row('s1', 'Likes tea'), status: 'accepted', resolved_at: '2026-10-15T00:00:00' },
      memory: {},
    });
    mocks.acceptDocNoteSuggestion.mockResolvedValue(resolvedDoc(docRow('d1', 'Uses metric units'), 'accepted'));
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    await screen.findByDisplayValue('Uses metric units');

    const open = openRegion();
    expect(textboxValues(open)).toEqual(['Likes tea', 'Uses metric units']);
    expect(open.querySelector('.memory-inbox-section-count')?.textContent).toBe('2');
    expect(open.textContent).not.toContain('Chat f1');
    await waitFor(() => expect(reviewedContents()).toEqual(['First']));

    fireEvent.click(within(open).getByRole('button', { name: 'Accept all' }));
    await screen.findByText(/No open suggestions/);
    expect(screen.queryAllByRole('alert')).toHaveLength(0);
    expect(reviewedContents()).toEqual(['Uses metric units', 'Likes tea', 'First']);
  });

  it('does not call the reviewed list empty while a page of left-out rows has a cursor', async () => {
    servePending([]);
    const future = { ...reviewed('f1', 'Old unknown', 'accepted'), kind: 'future' } as unknown as MemoryInboxRow;
    mocks.fetchMemoryInboxResolved
      .mockResolvedValueOnce({ suggestions: [future], next_cursor: 'c1' })
      .mockResolvedValueOnce({ suggestions: [], next_cursor: null });
    renderInbox(<MemoryInbox onGoToConversation={vi.fn()} />);
    const loadMore = await screen.findByRole('button', { name: 'Load more' });
    expect(screen.queryByText('Nothing reviewed yet.')).toBeNull();

    // The last page (no cursor) settles it.
    fireEvent.click(loadMore);
    await screen.findByText('Nothing reviewed yet.');
    expect(screen.queryByText('Old unknown')).toBeNull();
  });
});
