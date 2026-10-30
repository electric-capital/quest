// The sidebar inbox badge adds open action requests and pending suggestions
// of all three kinds (personal and project memories, doc notes), seeded by
// REST and then followed through the per-user globals.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { RequestsBadge } from './RequestsBadge';

const mocks = vi.hoisted(() => ({
  fetchActionRequestCounts: vi.fn(),
  fetchMemoryInboxCounts: vi.fn(),
  globalHandlers: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('../../api/client', () => ({
  fetchActionRequestCounts: mocks.fetchActionRequestCounts,
  fetchMemoryInboxCounts: mocks.fetchMemoryInboxCounts,
}));

vi.mock('../../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (handler: (event: Record<string, unknown>) => void) => {
      mocks.globalHandlers.add(handler);
      return () => mocks.globalHandlers.delete(handler);
    },
  },
}));

function emit(event: Record<string, unknown>) {
  act(() => {
    mocks.globalHandlers.forEach((handler) => handler(event));
  });
}

beforeEach(() => {
  mocks.fetchActionRequestCounts.mockReset();
  mocks.fetchMemoryInboxCounts.mockReset();
  mocks.globalHandlers.clear();
});

afterEach(cleanup);

describe('RequestsBadge', () => {
  it('sums open requests and pending suggestions, following the events', async () => {
    mocks.fetchActionRequestCounts.mockResolvedValue({ counts: { open: 2, executed: 0, denied: 0, stopped: 0, all: 2 } });
    mocks.fetchMemoryInboxCounts.mockResolvedValue({ user: 1, project: 1, doc: 0, pending_count: 2 });
    render(<RequestsBadge />);
    await waitFor(() => expect(screen.getByText('4')).not.toBeNull());

    emit({ type: 'request_count_changed', counts: { open: 0 } });
    expect(screen.getByText('2').textContent).toBe('2');
    emit({ type: 'memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    expect(screen.getByText('1')).not.toBeNull();
    emit({ type: 'project_memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    expect(screen.queryByText('0')).toBeNull();
    expect(document.querySelector('.requests-badge')).toBeNull();

    emit({ type: 'memory_suggestions_changed', conversation_id: 'c', pending_count: 3, total_pending: 12 });
    expect(screen.getByText('9+')).not.toBeNull();
  });

  it('adds the doc note count and follows its event', async () => {
    mocks.fetchActionRequestCounts.mockResolvedValue({ counts: { open: 1, executed: 0, denied: 0, stopped: 0, all: 1 } });
    mocks.fetchMemoryInboxCounts
      .mockResolvedValueOnce({ user: 1, project: 0, doc: 2, pending_count: 3 })
      .mockResolvedValueOnce({ user: 1, project: 0, doc: 6, pending_count: 7 });
    render(<RequestsBadge />);
    await waitFor(() => expect(screen.getByText('4')).not.toBeNull());

    // total_pending replaces the doc part only.
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 1, total_pending: 5 });
    expect(screen.getByText('7')).not.toBeNull();
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    expect(screen.getByText('2')).not.toBeNull();
    emit({ type: 'memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    expect(screen.getByText('1')).not.toBeNull();

    // An event without a numeric total re-fetches the counts.
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 1 });
    await waitFor(() => expect(mocks.fetchMemoryInboxCounts).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText('8')).not.toBeNull());
  });

  it('stays hidden while nothing is unanswered', async () => {
    mocks.fetchActionRequestCounts.mockResolvedValue({ counts: { open: 0, executed: 0, denied: 0, stopped: 0, all: 0 } });
    mocks.fetchMemoryInboxCounts.mockResolvedValue({ user: 0, project: 0, doc: 0, pending_count: 0 });
    render(<RequestsBadge />);
    await waitFor(() => expect(mocks.fetchMemoryInboxCounts).toHaveBeenCalled());
    expect(document.querySelector('.requests-badge')).toBeNull();
  });
});
