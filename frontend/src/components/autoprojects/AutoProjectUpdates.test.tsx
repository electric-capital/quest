// The Updates card: the sort control (only with two or more updates), the
// display order it selects, "Show earlier updates" at the old end in both
// orders, the optimistic entry of a run being started, and the empty,
// loading and failed states.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { AutoProjectRunsView } from '../../hooks/useAutoProjectRuns';
import { ApiClientError } from '../../api/request';
import { AutoProjectUpdates, type PendingStart } from './AutoProjectUpdates';
import type { AutoProjectUpdatesOrder } from './autoprojectUpdatesOrder';
import { makeRun, makeRunsView } from './autoprojectTestFixtures';

function renderUpdates(
  runs: AutoProjectRunsView,
  {
    order = 'recent',
    onOrderChange = vi.fn(),
    pendingStart = null,
    archived = false,
  }: {
    order?: AutoProjectUpdatesOrder;
    onOrderChange?: (order: AutoProjectUpdatesOrder) => void;
    pendingStart?: PendingStart | null;
    archived?: boolean;
  } = {},
) {
  const props = {
    autoprojectId: 'ap1',
    archived,
    runs,
    pendingStart,
    keptCards: {},
    activeRunId: null,
    activity: null,
    answeringHandleId: null,
    openRunConversationId: null,
    count: runs.runs.length,
    order,
    onOrderChange,
    onLoadOlder: runs.loadOlder,
    onStop: vi.fn(),
    onQuestionAnswered: vi.fn(),
    onOpenFile: vi.fn(),
    onDelete: vi.fn(),
    onView: vi.fn(),
    claimArrival: () => false,
  };
  const utils = render(<AutoProjectUpdates {...props} />);
  return {
    ...utils,
    rerenderWith: (next: Partial<typeof props>) => utils.rerender(<AutoProjectUpdates {...props} {...next} />),
  };
}

/** Article labels in display order. */
function displayed(): string[] {
  return screen.getAllByRole('article').map((el) => el.getAttribute('aria-label') ?? '');
}

/** The card body's children in display order: 'earlier' for the button, else the list. */
function bodyOrder(): string[] {
  const body = document.querySelector('.autoproject-card-body')!;
  return Array.from(body.children).map((el) =>
    el.classList.contains('autoproject-updates-earlier') ? 'earlier' : 'list',
  );
}

afterEach(cleanup);

describe('AutoProjectUpdates', () => {
  it('offers the sort control only once there are two updates', () => {
    const { rerenderWith } = renderUpdates(makeRunsView([makeRun(1)]));
    expect(screen.queryByRole('group', { name: 'Sort updates' })).toBeNull();

    rerenderWith({ runs: makeRunsView([makeRun(1), makeRun(2)]) });
    expect(screen.getByRole('group', { name: 'Sort updates' })).toBeTruthy();
  });

  it('counts a run being started toward the two updates', () => {
    renderUpdates(makeRunsView([makeRun(1)]), {
      pendingStart: { key: 1, prompt: 'Next step', startedAt: '2026-11-10T11:00:00', conversationId: null },
    });
    expect(screen.getByRole('group', { name: 'Sort updates' })).toBeTruthy();
  });

  it('shows the updates newest first by default and oldest first on request', () => {
    const onOrderChange = vi.fn();
    const runs = makeRunsView([makeRun(1), makeRun(2), makeRun(3)]);
    const { rerenderWith } = renderUpdates(runs, { onOrderChange });

    expect(displayed()).toEqual(['Update 3', 'Update 2', 'Update 1']);
    expect(screen.getByRole('button', { name: 'Recent first' }).getAttribute('aria-pressed')).toBe('true');

    fireEvent.click(screen.getByRole('button', { name: 'Oldest first' }));
    expect(onOrderChange).toHaveBeenCalledWith('oldest');

    rerenderWith({ order: 'oldest' });
    expect(displayed()).toEqual(['Update 1', 'Update 2', 'Update 3']);
    expect(screen.getByRole('button', { name: 'Oldest first' }).getAttribute('aria-pressed')).toBe('true');
  });

  it('puts the optimistic entry at the newest end in either order', () => {
    const pendingStart = { key: 1, prompt: 'Next step', startedAt: '2026-11-10T11:00:00', conversationId: null };
    const runs = makeRunsView([makeRun(1), makeRun(2)]);
    const { rerenderWith } = renderUpdates(runs, { pendingStart });
    expect(displayed()).toEqual(['New update', 'Update 2', 'Update 1']);
    expect(screen.getByText('Next step')).toBeTruthy();
    expect(screen.getByText('Starting')).toBeTruthy();

    rerenderWith({ order: 'oldest' });
    expect(displayed()).toEqual(['Update 1', 'Update 2', 'New update']);
  });

  it.each([
    ['recent', ['list', 'earlier']],
    ['oldest', ['earlier', 'list']],
  ] as const)('puts "Show earlier updates" at the old end (%s first)', (order, expected) => {
    const runs = makeRunsView([makeRun(5), makeRun(6)], { hasOlder: true });
    renderUpdates(runs, { order });
    expect(bodyOrder()).toEqual(expected);
    fireEvent.click(screen.getByRole('button', { name: 'Show earlier updates' }));
    expect(runs.loadOlder).toHaveBeenCalledTimes(1);
  });

  it('disables "Show earlier updates" while a page loads and hides it at the end', () => {
    const { rerenderWith } = renderUpdates(makeRunsView([makeRun(5)], { hasOlder: true, loadingOlder: true }));
    const button = screen.getByRole('button', { name: 'Loading earlier updates...' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    rerenderWith({ runs: makeRunsView([makeRun(5)], { hasOlder: false }) });
    expect(screen.queryByRole('button', { name: /earlier updates/ })).toBeNull();
  });

  it('shows the empty, loading and failed states', () => {
    const { rerenderWith } = renderUpdates(makeRunsView([]));
    expect(screen.getByText('No updates yet')).toBeTruthy();

    rerenderWith({ runs: makeRunsView([], { loading: true }) });
    expect(screen.getByText('Loading updates...')).toBeTruthy();

    const failed = makeRunsView([], { error: new ApiClientError('Server unavailable', 503) });
    rerenderWith({ runs: failed });
    expect(screen.getByRole('alert').textContent).toContain('Server unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(failed.refresh).toHaveBeenCalledTimes(1);
  });
});
