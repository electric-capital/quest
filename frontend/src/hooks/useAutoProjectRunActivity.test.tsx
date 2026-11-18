// useAutoProjectRunActivity: the activity line's label for the active run,
// derived from the run conversation's rows and live events. Each conversation
// id takes exactly one hold on the socket channel and releases exactly one
// (another holder, e.g. the run panel's ChatPanel, keeps its own).
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AUTOPROJECT_ACTIVITY_RESUMING,
  AUTOPROJECT_ACTIVITY_STARTING,
  AUTOPROJECT_ACTIVITY_THINKING,
  AUTOPROJECT_ACTIVITY_WAITING,
  AUTOPROJECT_ACTIVITY_WRITING,
  rowActivityLabel,
  useAutoProjectRunActivity,
} from './useAutoProjectRunActivity';

type Handler = (event: Record<string, unknown>) => void;
type Tail = { messages: Array<Record<string, unknown>>; last_message_seq: number };

const mocks = vi.hoisted(() => ({
  fetchConversationTail: vi.fn(),
  subscribe: vi.fn(),
  unsubscribe: vi.fn(),
  resubscribe: vi.fn(),
  handlers: new Map<string, Set<(event: Record<string, unknown>) => void>>(),
  log: [] as string[],
}));

vi.mock('../api/client', () => ({
  fetchConversationTail: mocks.fetchConversationTail,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onConversationEvent: (id: string, handler: Handler) => {
      mocks.log.push(`attach:${id}`);
      let set = mocks.handlers.get(id);
      if (!set) {
        set = new Set();
        mocks.handlers.set(id, set);
      }
      set.add(handler);
      return () => {
        mocks.log.push(`detach:${id}`);
        mocks.handlers.get(id)?.delete(handler);
      };
    },
    subscribe: (id: string) => {
      mocks.log.push(`subscribe:${id}`);
      mocks.subscribe(id);
    },
    unsubscribe: (id: string) => {
      mocks.log.push(`unsubscribe:${id}`);
      mocks.unsubscribe(id);
    },
    resubscribe: mocks.resubscribe,
  },
}));

function emit(id: string, event: Record<string, unknown>) {
  act(() => {
    for (const handler of [...(mocks.handlers.get(id) ?? [])]) handler({ conversation_id: id, ...event });
  });
}

beforeEach(() => {
  mocks.fetchConversationTail.mockReset().mockResolvedValue({ messages: [], last_message_seq: 0 });
  mocks.subscribe.mockReset();
  mocks.unsubscribe.mockReset();
  mocks.resubscribe.mockReset();
  mocks.handlers.clear();
  mocks.log = [];
});

afterEach(cleanup);

describe('rowActivityLabel', () => {
  it.each([
    [{ type: 'tool_use', tool_name: 'web_search', intent_message: 'Searching for flights' }, 'Searching for flights'],
    [{ type: 'tool_use', tool_name: 'web_search' }, 'Running web search'],
    [{ type: 'tool_use', tool_name: 'tool_call', tool_input: { tool_name: 'list_docs' } }, 'Running list docs'],
    [{ type: 'tool_use', tool_name: 'complete_autoproject_run' }, 'Writing the result and handoff'],
    [{ type: 'tool_use', tool_name: 'ask_autoproject_question' }, 'Asking you a question'],
    [{ type: 'tool_use' }, AUTOPROJECT_ACTIVITY_THINKING],
    [{ type: 'tool_result' }, AUTOPROJECT_ACTIVITY_THINKING],
    [{ type: 'text', role: 'user', content: 'Do it' }, AUTOPROJECT_ACTIVITY_THINKING],
    [{ role: 'assistant', content: 'Done' }, AUTOPROJECT_ACTIVITY_THINKING],
    [{ type: 'autoproject_question', status: 'pending' }, AUTOPROJECT_ACTIVITY_WAITING],
    [{ type: 'autoproject_question', status: 'answered' }, AUTOPROJECT_ACTIVITY_THINKING],
    [{ type: 'action_request', status: 'open' }, AUTOPROJECT_ACTIVITY_WAITING],
    [{ type: 'connect_request', status: 'pending' }, AUTOPROJECT_ACTIVITY_WAITING],
    [{ type: 'action_request', status: 'executed' }, AUTOPROJECT_ACTIVITY_THINKING],
    [{ type: 'autoproject_nudge' }, null],
    [{ type: 'stats' }, null],
  ])('%o -> %s', (row, label) => {
    expect(rowActivityLabel(row)).toBe(label);
  });
});

describe('useAutoProjectRunActivity', () => {
  it('attaches its handler before taking one hold, and releases exactly one on unmount', () => {
    const { result, unmount } = renderHook(() => useAutoProjectRunActivity('c1'));
    expect(result.current.label).toBe(AUTOPROJECT_ACTIVITY_STARTING);
    expect(mocks.log).toEqual(['attach:c1', 'subscribe:c1']);

    unmount();
    expect(mocks.log).toEqual(['attach:c1', 'subscribe:c1', 'detach:c1', 'unsubscribe:c1']);
    expect(mocks.subscribe).toHaveBeenCalledTimes(1);
    expect(mocks.unsubscribe).toHaveBeenCalledTimes(1);
  });

  it('pairs one subscribe with one unsubscribe per conversation across id changes', () => {
    const { rerender, unmount } = renderHook(({ id }) => useAutoProjectRunActivity(id), {
      initialProps: { id: 'c1' as string | null },
    });
    rerender({ id: 'c2' });
    rerender({ id: null });
    unmount();
    expect(mocks.subscribe.mock.calls).toEqual([['c1'], ['c2']]);
    expect(mocks.unsubscribe.mock.calls).toEqual([['c1'], ['c2']]);
  });

  it('subscribes to nothing without a conversation id', () => {
    const { result } = renderHook(() => useAutoProjectRunActivity(null));
    expect(result.current.label).toBe(AUTOPROJECT_ACTIVITY_STARTING);
    expect(mocks.subscribe).not.toHaveBeenCalled();
  });

  it('labels live events: writing, sub-agent steps and resuming', () => {
    const { result } = renderHook(() => useAutoProjectRunActivity('c1'));
    emit('c1', { type: 'text_delta', content: 'Hel' });
    expect(result.current.label).toBe(AUTOPROJECT_ACTIVITY_WRITING);
    emit('c1', { type: 'sub_agent_tool_use', tool_name: 'web_fetch' });
    expect(result.current.label).toBe('Sub-agent: web fetch');
    emit('c1', { type: 'sub_agent_tool_use', tool_name: 'web_fetch', intent_message: 'Reading the filing' });
    expect(result.current.label).toBe('Sub-agent: Reading the filing');
    emit('c1', { type: 'resume_started' });
    expect(result.current.label).toBe(AUTOPROJECT_ACTIVITY_RESUMING);
    // The finish keeps the last label; another conversation's events are not seen.
    emit('c1', { type: 'send_message_finished' });
    emit('c2', { type: 'text_delta', content: 'x' });
    expect(result.current.label).toBe(AUTOPROJECT_ACTIVITY_RESUMING);
  });

  it('reads the last rows after the first subscribed ack and the tail after each message_appended', async () => {
    mocks.fetchConversationTail.mockResolvedValueOnce({
      messages: [
        { seq: 7, type: 'text', role: 'user', content: 'Go' },
        { seq: 8, type: 'tool_use', tool_name: 'web_search', intent_message: 'Searching the web' },
      ],
      last_message_seq: 8,
    } satisfies Tail);
    const { result } = renderHook(() => useAutoProjectRunActivity('c1'));
    emit('c1', { type: 'subscribed', mode: 'up_to_date', current_seq: 8 });
    await waitFor(() => expect(result.current.label).toBe('Searching the web'));
    expect(mocks.fetchConversationTail).toHaveBeenCalledWith('c1', 4);

    mocks.fetchConversationTail.mockResolvedValueOnce({
      messages: [{ seq: 9, type: 'autoproject_question', status: 'pending' }],
      last_message_seq: 9,
    } satisfies Tail);
    emit('c1', { type: 'message_appended', seq: 9 });
    await waitFor(() => expect(result.current.label).toBe(AUTOPROJECT_ACTIVITY_WAITING));
    expect(mocks.fetchConversationTail).toHaveBeenLastCalledWith('c1', 8);
  });

  it('applies the rows of a catchup ack directly', () => {
    const { result } = renderHook(() => useAutoProjectRunActivity('c1'));
    emit('c1', {
      type: 'subscribed',
      mode: 'catchup',
      current_seq: 3,
      messages: [{ seq: 3, type: 'tool_use', tool_name: 'read_file' }],
    });
    expect(result.current.label).toBe('Running read file');
  });

  it('never lets a tail fetch that lands late put an older step over a newer event', async () => {
    let release: (tail: Tail) => void = () => {};
    mocks.fetchConversationTail.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    const { result } = renderHook(() => useAutoProjectRunActivity('c1'));
    emit('c1', { type: 'subscribed', mode: 'up_to_date', current_seq: 0 });
    emit('c1', { type: 'message_appended', seq: 1 });
    emit('c1', { type: 'text_delta', content: 'Hi' });
    expect(result.current.label).toBe(AUTOPROJECT_ACTIVITY_WRITING);

    await act(async () => release({ messages: [{ seq: 1, type: 'tool_use', tool_name: 'web_search' }], last_message_seq: 1 }));
    expect(result.current.label).toBe(AUTOPROJECT_ACTIVITY_WRITING);
  });

  it('re-sends the subscribe on subscription_expired without taking another hold', () => {
    renderHook(() => useAutoProjectRunActivity('c1'));
    emit('c1', { type: 'subscription_expired' });
    expect(mocks.resubscribe).toHaveBeenCalledWith('c1');
    expect(mocks.subscribe).toHaveBeenCalledTimes(1);
  });
});
