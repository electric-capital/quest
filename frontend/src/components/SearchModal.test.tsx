// The search modal's routine filter: searches leave routine runs out by
// default, the "Include routine runs" checkbox re-runs the current query
// with them, hits from routine runs carry a badge, and reopening the modal
// resets the checkbox.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { SearchResult } from '../api/types';
import { SearchModal } from './SearchModal';

const mocks = vi.hoisted(() => ({
  searchConversations: vi.fn(),
}));

vi.mock('../api/client', () => ({
  searchConversations: mocks.searchConversations,
}));

function hit(overrides: Partial<SearchResult> = {}): SearchResult {
  return {
    conversation_id: 'c1',
    conversation_title: 'Quarterly report',
    project_id: null,
    message_index: 1,
    message_role: 'assistant',
    snippet: 'the <mark>quarterly</mark> report',
    timestamp: '2026-08-01T12:00:00Z',
    ...overrides,
  };
}

function respond(results: SearchResult[]) {
  return { results, total_matches: results.length, query: 'quarterly' };
}

async function typeQuery(value: string) {
  fireEvent.change(screen.getByPlaceholderText('Search conversations...'), {
    target: { value },
  });
  await act(async () => {
    vi.advanceTimersByTime(300);
  });
}

describe('SearchModal routine filter', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.searchConversations.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('searches without routine runs by default and re-runs with them when toggled', async () => {
    mocks.searchConversations
      .mockResolvedValueOnce(respond([hit()]))
      .mockResolvedValueOnce(respond([hit(), hit({ conversation_id: 'c2', routine_id: 'rtn' })]));

    render(<SearchModal isOpen onClose={() => {}} onNavigate={() => {}} />);
    const checkbox = screen.getByLabelText('Include routine runs') as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    await typeQuery('quarterly');
    expect(mocks.searchConversations).toHaveBeenCalledTimes(1);
    expect(mocks.searchConversations).toHaveBeenLastCalledWith('quarterly', { includeRoutines: false });
    expect(screen.getByText('1 match found')).toBeTruthy();
    expect(screen.queryByText('routine')).toBeNull();

    await act(async () => {
      fireEvent.click(checkbox);
    });
    expect(checkbox.checked).toBe(true);
    expect(mocks.searchConversations).toHaveBeenCalledTimes(2);
    expect(mocks.searchConversations).toHaveBeenLastCalledWith('quarterly', { includeRoutines: true });
    expect(screen.getByText('2 matches found')).toBeTruthy();
    expect(screen.getByText('routine')).toBeTruthy();
  });

  it('does not search on toggle while the query is too short', async () => {
    render(<SearchModal isOpen onClose={() => {}} onNavigate={() => {}} />);
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Include routine runs'));
    });
    expect(mocks.searchConversations).not.toHaveBeenCalled();
  });

  it('resets the checkbox when the modal reopens', async () => {
    const { rerender } = render(<SearchModal isOpen onClose={() => {}} onNavigate={() => {}} />);
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Include routine runs'));
    });
    expect((screen.getByLabelText('Include routine runs') as HTMLInputElement).checked).toBe(true);

    rerender(<SearchModal isOpen={false} onClose={() => {}} onNavigate={() => {}} />);
    rerender(<SearchModal isOpen onClose={() => {}} onNavigate={() => {}} />);
    expect((screen.getByLabelText('Include routine runs') as HTMLInputElement).checked).toBe(false);
  });
});
