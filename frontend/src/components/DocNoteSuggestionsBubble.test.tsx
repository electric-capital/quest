/**
 * The composer's doc note bubble following the conversation's count: a
 * harness renders ``MemorySuggestions kind="doc"`` with
 * ``useConversation(id).pendingDocNoteSuggestions``, as ChatPanel's
 * suggestions bar does. The count is seeded from the GET payload's
 * ``pending_doc_note_suggestions`` and replaced by
 * ``doc_note_suggestions_changed`` globals for this conversation; another
 * conversation's event and the other kinds' events leave it alone, and the
 * bubble hides at 0. The API client and the realtime singletons are mocked
 * as in useConversation.test.tsx; the store is real.
 */
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import type { ConversationDetail } from '../api/types';
import { useConversation } from '../hooks/useConversation';
import { MemorySuggestions } from './MemorySuggestions';

type Envelope = Record<string, unknown>;
type Handler = (event: Envelope) => void;

const mocks = vi.hoisted(() => {
  const globals = new Set<Handler>();
  return {
    globals,
    fetchConversation: vi.fn<(id: string) => Promise<ConversationDetail>>(),
    fetchConversationDocNoteSuggestions: vi.fn(),
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

// The bubble lists nothing until it is opened; fake the network call anyway.
vi.mock('../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/docsApi')>()),
  fetchConversationDocNoteSuggestions: mocks.fetchConversationDocNoteSuggestions,
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

/** ChatPanel's wiring of the doc kind, without the rest of the panel. */
function Harness({ conversationId }: { conversationId: string }) {
  const { isLoaded, pendingDocNoteSuggestions } = useConversation(conversationId);
  return (
    <div data-testid="harness" data-loaded={String(isLoaded)}>
      <MemorySuggestions
        kind="doc"
        conversationId={conversationId}
        pendingCount={pendingDocNoteSuggestions}
      />
    </div>
  );
}

let counter = 0;

/** Mount the harness on a fresh conversation whose GET carries `pending`. */
async function mountBubble(pending?: number) {
  counter += 1;
  const id = `bubble-conv-${counter}`;
  mocks.fetchConversation.mockResolvedValueOnce({
    id,
    user_id: 1,
    created_at: '2026-10-30T00:00:00Z',
    messages: [],
    last_message_seq: 0,
    ...(pending === undefined ? {} : { pending_doc_note_suggestions: pending }),
  });
  render(
    <MemoryRouter>
      <Harness conversationId={id} />
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

describe('doc note suggestions bubble', () => {
  beforeEach(() => {
    mocks.fetchConversation.mockReset();
    mocks.fetchConversationDocNoteSuggestions.mockReset();
    mocks.globals.clear();
  });

  afterEach(cleanup);

  it('is seeded from pending_doc_note_suggestions and follows this conversation\'s events', async () => {
    const id = await mountBubble(1);
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
    expect(bubble()?.textContent).toBe('3 suggested doc notes');
    expect(screen.getAllByRole('button')).toHaveLength(1);

    // The last one resolved: the bubble goes.
    docNoteEvent(id, 0);
    expect(bubble()).toBeNull();
    expect(document.querySelector('.memory-suggestions--doc')).toBeNull();
    // Closed throughout: nothing was listed.
    expect(mocks.fetchConversationDocNoteSuggestions).not.toHaveBeenCalled();
  });

  it('stays hidden without pending rows and appears with the first one', async () => {
    const id = await mountBubble();
    expect(bubble()).toBeNull();

    docNoteEvent('another-conversation', 2);
    expect(bubble()).toBeNull();

    docNoteEvent(id, 1);
    expect(bubble()?.textContent).toBe('1 suggested doc note');
  });
});
