/**
 * The composer's suggestions bar (``MemorySuggestionsBar``) fed by
 * ``useConversation``'s three counts, as ChatPanel renders it: nothing while
 * all three are 0, the bubbles in the order personal, project, doc, each
 * hidden at 0. The doc bubble's count is seeded from the GET payload's
 * ``pending_doc_note_suggestions`` and replaced by
 * ``doc_note_suggestions_changed`` globals for this conversation; another
 * conversation's event and the other kinds' events leave it alone. A
 * bubble that goes away with focus inside it hands focus to the bubble left
 * in the bar, else to the composer field of its ``.chat-panel`` (unless
 * disabled, or the last input was a tap), both when the counts drop it and
 * when its open panel closes itself at zero; focus elsewhere, or after a
 * press outside the bar, stays. The API client and the realtime
 * singletons are mocked as in useConversation.test.tsx; the store is real.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import type { ConversationDetail, DocNoteSuggestion } from '../api/types';
import { useConversation } from '../hooks/useConversation';
import { MemorySuggestionsBar } from './MemorySuggestions';

type Envelope = Record<string, unknown>;
type Handler = (event: Envelope) => void;

const mocks = vi.hoisted(() => {
  const globals = new Set<Handler>();
  return {
    globals,
    fetchConversation: vi.fn<(id: string) => Promise<ConversationDetail>>(),
    fetchConversationDocNoteSuggestions: vi.fn(),
    dismissDocNoteSuggestion: vi.fn(),
    emitGlobal(event: Envelope) {
      for (const h of Array.from(globals)) h(event);
    },
  };
});

vi.mock('../api/client', () => ({
  fetchConversation: mocks.fetchConversation,
  fetchConversationTail: vi.fn(),
  fetchConversationLoadedSkills: vi.fn(),
  ApiClientError: class ApiClientError extends Error {},
  // MemorySuggestions' personal and project calls (never reached here).
  fetchMemorySuggestions: vi.fn(),
  acceptMemorySuggestion: vi.fn(),
  dismissMemorySuggestion: vi.fn(),
  fetchProjectMemorySuggestions: vi.fn(),
  acceptProjectMemorySuggestion: vi.fn(),
  dismissProjectMemorySuggestion: vi.fn(),
}));

// The bubble lists nothing until it is opened; fake the network calls anyway.
vi.mock('../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/docsApi')>()),
  fetchConversationDocNoteSuggestions: mocks.fetchConversationDocNoteSuggestions,
  dismissDocNoteSuggestion: mocks.dismissDocNoteSuggestion,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    subscribe: vi.fn(),
    onConversationEvent: () => () => {},
    onGlobalEvent: (handler: Handler) => {
      mocks.globals.add(handler);
      return () => {
        mocks.globals.delete(handler);
      };
    },
    getLastSeq: () => 0,
    setLastSeq: vi.fn(),
  },
}));

vi.mock('../services/WebSocketManager', () => ({
  webSocketManager: {
    syncRunState: vi.fn(),
    attachToResumeStream: vi.fn(),
    sendMessage: vi.fn(),
    onConversationRenamed: () => () => {},
  },
}));

/** ChatPanel's bar and its wiring, and its composer field, without the rest of the panel. */
function Harness({ conversationId, composerDisabled }: { conversationId: string; composerDisabled: boolean }) {
  const {
    isLoaded,
    pendingMemorySuggestions,
    pendingProjectMemorySuggestions,
    pendingDocNoteSuggestions,
  } = useConversation(conversationId);
  return (
    <div className="chat-panel" data-testid="harness" data-loaded={String(isLoaded)}>
      <MemorySuggestionsBar
        conversationId={conversationId}
        pending={{
          user: pendingMemorySuggestions,
          project: pendingProjectMemorySuggestions,
          doc: pendingDocNoteSuggestions,
        }}
      />
      <textarea
        className="message-input composer-input"
        aria-label="Message"
        disabled={composerDisabled}
      />
    </div>
  );
}

let counter = 0;

/** Mount the harness on a fresh conversation whose GET carries the counts. */
async function mountBar(counts: Partial<ConversationDetail> = {}, composerDisabled = false) {
  counter += 1;
  const id = `bubble-conv-${counter}`;
  mocks.fetchConversation.mockResolvedValueOnce({
    id,
    user_id: 1,
    created_at: '2026-10-30T00:00:00Z',
    messages: [],
    last_message_seq: 0,
    ...counts,
  });
  render(
    <MemoryRouter>
      <Harness conversationId={id} composerDisabled={composerDisabled} />
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(screen.getByTestId('harness').getAttribute('data-loaded')).toBe('true'),
  );
  return id;
}

function docNoteEvent(conversationId: string, pendingCount: number) {
  act(() => {
    mocks.emitGlobal({
      type: 'doc_note_suggestions_changed',
      conversation_id: conversationId,
      pending_count: pendingCount,
      total_pending: pendingCount + 4,
    });
  });
}

const bubble = () => screen.queryByRole('button', { name: /suggested doc note/ });
const bar = () => document.querySelector('.memory-suggestions-bar');
const bubbleTexts = () =>
  [...document.querySelectorAll('.memory-suggestions-bar .memory-suggestions-bubble')].map(
    (b) => b.textContent,
  );

describe('suggestions bar', () => {
  beforeEach(() => {
    mocks.fetchConversation.mockReset();
    mocks.globals.clear();
  });

  afterEach(cleanup);

  it('renders nothing while all three counts are 0', async () => {
    await mountBar({
      pending_memory_suggestions: 0,
      pending_project_memory_suggestions: 0,
      pending_doc_note_suggestions: 0,
    });
    expect(bar()).toBeNull();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('shows the doc bubble alone when only doc notes are pending', async () => {
    await mountBar({ pending_doc_note_suggestions: 2 });
    expect(bar()).not.toBeNull();
    expect(bubbleTexts()).toEqual(['2 suggested doc notes']);
    expect(bar()?.querySelector('.memory-suggestions--doc')).not.toBeNull();
  });

  it('orders the bubbles personal, project, doc', async () => {
    await mountBar({
      pending_memory_suggestions: 1,
      pending_project_memory_suggestions: 2,
      pending_doc_note_suggestions: 3,
    });
    expect(bubbleTexts()).toEqual([
      '1 suggested memory',
      '2 suggested project memories',
      '3 suggested doc notes',
    ]);
    const kinds = [...bar()!.children].map((el) =>
      [...el.classList].find((c) => c.startsWith('memory-suggestions--')),
    );
    expect(kinds).toEqual([
      'memory-suggestions--user',
      'memory-suggestions--project',
      'memory-suggestions--doc',
    ]);
  });

  it('keeps the bar while another kind is pending and drops it with the last', async () => {
    const id = await mountBar({ pending_memory_suggestions: 1, pending_doc_note_suggestions: 1 });
    expect(bubbleTexts()).toEqual(['1 suggested memory', '1 suggested doc note']);

    docNoteEvent(id, 0);
    expect(bubbleTexts()).toEqual(['1 suggested memory']);

    act(() => {
      mocks.emitGlobal({ type: 'memory_suggestions_changed', conversation_id: id, pending_count: 0, total_pending: 0 });
    });
    expect(bar()).toBeNull();
  });
});

describe('doc note suggestions bubble', () => {
  beforeEach(() => {
    mocks.fetchConversation.mockReset();
    mocks.fetchConversationDocNoteSuggestions.mockReset();
    mocks.globals.clear();
  });

  afterEach(cleanup);

  it('is seeded from pending_doc_note_suggestions and follows this conversation\'s events', async () => {
    const id = await mountBar({ pending_doc_note_suggestions: 1 });
    expect(bubble()?.textContent).toBe('1 suggested doc note');
    expect(document.querySelector('.memory-suggestions--doc')).not.toBeNull();

    docNoteEvent(id, 3);
    expect(bubble()?.textContent).toBe('3 suggested doc notes');

    // Another conversation's count is not this bubble's.
    docNoteEvent('another-conversation', 7);
    expect(bubble()?.textContent).toBe('3 suggested doc notes');

    // Nor are the personal and project counts of this conversation.
    act(() => {
      mocks.emitGlobal({ type: 'memory_suggestions_changed', conversation_id: id, pending_count: 5, total_pending: 5 });
      mocks.emitGlobal({ type: 'project_memory_suggestions_changed', conversation_id: id, pending_count: 6, total_pending: 6 });
    });
    expect(bubbleTexts()).toEqual([
      '5 suggested memories',
      '6 suggested project memories',
      '3 suggested doc notes',
    ]);

    // The last one resolved: the doc bubble goes, the others stay.
    docNoteEvent(id, 0);
    expect(bubble()).toBeNull();
    expect(document.querySelector('.memory-suggestions--doc')).toBeNull();
    expect(bubbleTexts()).toEqual(['5 suggested memories', '6 suggested project memories']);
    // Closed throughout: nothing was listed.
    expect(mocks.fetchConversationDocNoteSuggestions).not.toHaveBeenCalled();
  });

  it('stays hidden without pending rows and appears with the first one', async () => {
    const id = await mountBar();
    expect(bubble()).toBeNull();
    expect(bar()).toBeNull();

    docNoteEvent('another-conversation', 2);
    expect(bubble()).toBeNull();

    docNoteEvent(id, 1);
    expect(bubble()?.textContent).toBe('1 suggested doc note');
  });
});

const docNote = (id: string): DocNoteSuggestion => ({
  id,
  user_id: 1,
  doc_id: `doc-${id}`,
  conversation_id: 'c',
  note_kind: 'memory',
  content: `Note ${id}`,
  reasoning: null,
  base_instructions_version: null,
  replaces_memory_id: null,
  status: 'pending',
  memory_id: null,
  created_at: '2026-10-30T00:00:00Z',
  resolved_at: null,
  doc_title: `Doc ${id}`,
  replaces_content: null,
  current_instructions: null,
  current_instructions_version: null,
  instructions_moved: false,
});

describe('suggestions bar focus', () => {
  beforeEach(() => {
    mocks.fetchConversation.mockReset();
    mocks.fetchConversationDocNoteSuggestions.mockReset();
    mocks.dismissDocNoteSuggestion.mockReset().mockResolvedValue({});
    mocks.globals.clear();
  });

  afterEach(cleanup);

  const composer = () => screen.getByLabelText('Message');
  const personalBubble = () => screen.getByRole('button', { name: /suggested memory/ });

  function focus(el: HTMLElement) {
    act(() => el.focus());
  }

  /** A pointer press (jsdom has no PointerEvent, so the type is set by hand). */
  function pointerDown(el: Element, pointerType: 'mouse' | 'touch') {
    const event = new Event('pointerdown', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'pointerType', { value: pointerType });
    act(() => {
      el.dispatchEvent(event);
    });
  }

  it('focuses the composer field when the last bubble goes with focus inside it', async () => {
    const id = await mountBar({ pending_doc_note_suggestions: 1 });
    focus(bubble()!);
    docNoteEvent(id, 0);
    expect(bar()).toBeNull();
    expect(document.activeElement).toBe(composer());
  });

  it('focuses the bubble left in the bar', async () => {
    const id = await mountBar({ pending_memory_suggestions: 1, pending_doc_note_suggestions: 1 });
    focus(bubble()!);
    docNoteEvent(id, 0);
    expect(bubble()).toBeNull();
    expect(document.activeElement).toBe(personalBubble());
  });

  it('skips a disabled composer field', async () => {
    const id = await mountBar({ pending_doc_note_suggestions: 1 }, true);
    focus(bubble()!);
    docNoteEvent(id, 0);
    expect(bar()).toBeNull();
    expect(document.activeElement).toBe(document.body);
  });

  it('skips the composer field after a tap, but not another bubble', async () => {
    const id = await mountBar({ pending_doc_note_suggestions: 1 });
    pointerDown(bubble()!, 'touch');
    focus(bubble()!);
    docNoteEvent(id, 0);
    expect(bar()).toBeNull();
    expect(document.activeElement).toBe(document.body);
    cleanup();

    const id2 = await mountBar({ pending_memory_suggestions: 1, pending_doc_note_suggestions: 1 });
    pointerDown(bubble()!, 'touch');
    focus(bubble()!);
    docNoteEvent(id2, 0);
    expect(document.activeElement).toBe(personalBubble());
  });

  it('leaves focus alone after a press outside the bar', async () => {
    const id = await mountBar({ pending_doc_note_suggestions: 1 });
    focus(bubble()!);
    // Clicking (or selecting) message text moves focus to <body>, no focusin.
    pointerDown(screen.getByTestId('harness'), 'mouse');
    act(() => (document.activeElement as HTMLElement).blur());
    docNoteEvent(id, 0);
    expect(bar()).toBeNull();
    expect(document.activeElement).toBe(document.body);
  });

  it('leaves focus that moved out of the bar alone', async () => {
    const id = await mountBar({ pending_memory_suggestions: 1, pending_doc_note_suggestions: 1 });
    focus(bubble()!);
    focus(composer());
    docNoteEvent(id, 0);
    expect(document.activeElement).toBe(composer());
  });

  it('hands focus on when the open panel closes itself at zero', async () => {
    for (const others of [1, 0]) {
      mocks.fetchConversationDocNoteSuggestions.mockResolvedValue({
        suggestions: [docNote('d1')],
        pending_count: 1,
      });
      const id = await mountBar({
        pending_memory_suggestions: others,
        pending_doc_note_suggestions: 1,
      });
      const docBubble = bubble()!;
      focus(docBubble);
      fireEvent.click(docBubble);
      await screen.findByDisplayValue('Note d1');
      const dismiss = screen.getByRole('button', { name: 'Dismiss' });
      focus(dismiss);
      fireEvent.click(dismiss);
      await waitFor(() => expect(screen.queryByDisplayValue('Note d1')).toBeNull());
      // The panel stays open until the count drops; focus waits on its bubble.
      expect(document.activeElement).toBe(docBubble);

      docNoteEvent(id, 0);
      await waitFor(() => expect(bubble()).toBeNull());
      expect(document.activeElement).toBe(others ? personalBubble() : composer());
      cleanup();
    }
  });
});
