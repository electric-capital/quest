// Suggested-memories bubble + panel: hidden at zero, opens on click and
// lists the conversation's pending suggestions, accept sends the edited
// text only when it changed, dismiss removes the row, a 409 from another
// tab drops the row silently, and a count change while open re-fetches.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { MemorySuggestion } from '../api/types';
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
}));

vi.mock('../api/client', () => ({
  fetchMemorySuggestions: mocks.fetchMemorySuggestions,
  acceptMemorySuggestion: mocks.acceptMemorySuggestion,
  dismissMemorySuggestion: mocks.dismissMemorySuggestion,
  fetchProjectMemorySuggestions: mocks.fetchProjectMemorySuggestions,
  acceptProjectMemorySuggestion: mocks.acceptProjectMemorySuggestion,
  dismissProjectMemorySuggestion: mocks.dismissProjectMemorySuggestion,
}));

const suggestion = (id: string, content: string, extra: Partial<MemorySuggestion> = {}): MemorySuggestion => ({
  id,
  user_id: 1,
  conversation_id: 'c1',
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

  it('reconciles the store count from the list and ignores a stale in-flight fetch', async () => {
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
