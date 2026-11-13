// The inbox view's Memories tab label counts pending suggestions of all
// three kinds (usePendingSuggestionCounts: REST seed, then each per-user
// global's total_pending, a re-fetch for an event without one), and a doc
// link in the Memories tab closes the inbox before it opens the doc. The
// Other tab label counts open notifications (useOpenNotificationCount) and
// its path shows the NotificationsInbox.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { DocNoteInboxSuggestion } from '../api/types';
import { RequestsView } from './RequestsView';

const mocks = vi.hoisted(() => ({
  fetchActionRequestsEnriched: vi.fn(),
  fetchActionRequestCounts: vi.fn(),
  fetchMemoryInboxCounts: vi.fn(),
  fetchMemoryInboxPending: vi.fn(),
  fetchMemoryInboxResolved: vi.fn(),
  fetchNotifications: vi.fn(),
  fetchNotificationsCount: vi.fn(),
  setShowRequestsView: vi.fn(),
  globalHandlers: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  fetchActionRequestsEnriched: mocks.fetchActionRequestsEnriched,
  fetchActionRequestCounts: mocks.fetchActionRequestCounts,
  fetchMemoryInboxCounts: mocks.fetchMemoryInboxCounts,
  fetchMemoryInboxPending: mocks.fetchMemoryInboxPending,
  fetchMemoryInboxResolved: mocks.fetchMemoryInboxResolved,
  fetchNotifications: mocks.fetchNotifications,
  fetchNotificationsCount: mocks.fetchNotificationsCount,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (handler: (event: Record<string, unknown>) => void) => {
      mocks.globalHandlers.add(handler);
      return () => mocks.globalHandlers.delete(handler);
    },
  },
}));

vi.mock('../contexts/NavigationContext', () => ({
  useNavigationState: () => ({
    setShowRequestsView: mocks.setShowRequestsView,
    setSettingsOpen: vi.fn(),
    setSettingsInitialSection: vi.fn(),
  }),
}));

function emit(event: Record<string, unknown>) {
  act(() => {
    mocks.globalHandlers.forEach((handler) => handler(event));
  });
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderView(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <RequestsView />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function memoriesTabCount(): string | null {
  const tab = screen.getByRole('tab', { name: /Memories/ });
  return tab.querySelector('.requests-view-tab-count')?.textContent ?? null;
}

function otherTabCount(): string | null {
  const tab = screen.getByRole('tab', { name: /Other/ });
  return tab.querySelector('.requests-view-tab-count')?.textContent ?? null;
}

const docRow: DocNoteInboxSuggestion = {
  id: 'd1',
  user_id: 1,
  doc_id: 'doc-d1',
  conversation_id: 'conv-d1',
  note_kind: 'memory',
  content: 'Uses metric units',
  reasoning: null,
  base_instructions_version: null,
  replaces_memory_id: null,
  status: 'pending',
  memory_id: null,
  created_at: '2026-10-29T00:00:00',
  resolved_at: null,
  doc_title: 'Budget 2027',
  replaces_content: null,
  current_instructions: null,
  current_instructions_version: null,
  instructions_moved: false,
  kind: 'doc',
  project_id: null,
  project_name: null,
  conversation_title: 'Planning chat',
};

beforeEach(() => {
  mocks.fetchActionRequestsEnriched.mockReset().mockResolvedValue({ action_requests: [] });
  mocks.fetchActionRequestCounts.mockReset().mockResolvedValue({
    counts: { open: 0, executed: 0, denied: 0, stopped: 0, all: 0 },
  });
  mocks.fetchMemoryInboxCounts.mockReset().mockResolvedValue({ user: 0, project: 0, doc: 0, pending_count: 0 });
  mocks.fetchNotifications.mockReset().mockResolvedValue({ notifications: [], open_count: 0 });
  mocks.fetchNotificationsCount.mockReset().mockResolvedValue({ open_count: 0 });
  mocks.fetchMemoryInboxPending.mockReset().mockResolvedValue({ suggestions: [], pending_count: 0 });
  mocks.fetchMemoryInboxResolved.mockReset().mockResolvedValue({ suggestions: [], next_cursor: null });
  mocks.setShowRequestsView.mockReset();
  mocks.globalHandlers.clear();
});

afterEach(cleanup);

describe('RequestsView', () => {
  it('counts pending suggestions of all three kinds on the Memories tab', async () => {
    mocks.fetchMemoryInboxCounts
      .mockResolvedValueOnce({ user: 1, project: 2, doc: 3, pending_count: 6 })
      .mockResolvedValueOnce({ user: 1, project: 0, doc: 0, pending_count: 1 });
    renderView('/inbox');
    await waitFor(() => expect(memoriesTabCount()).toBe('6'));

    // Each kind's total_pending replaces its own part.
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 1 });
    expect(memoriesTabCount()).toBe('4');
    emit({ type: 'project_memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    expect(memoriesTabCount()).toBe('2');
    // No numeric total: the counts are fetched again, as for the badge.
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 0 });
    await waitFor(() => expect(mocks.fetchMemoryInboxCounts).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(memoriesTabCount()).toBe('1'));
    emit({ type: 'memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    expect(memoriesTabCount()).toBeNull();
  });

  it('closes the inbox when a doc link in the Memories tab opens the doc', async () => {
    mocks.fetchMemoryInboxCounts.mockResolvedValue({ user: 0, project: 0, doc: 1, pending_count: 1 });
    mocks.fetchMemoryInboxPending.mockResolvedValue({ suggestions: [docRow], pending_count: 1 });
    renderView('/inbox/memories');
    const link = await screen.findByRole('link', { name: 'Budget 2027' });
    expect(mocks.setShowRequestsView).not.toHaveBeenCalled();

    fireEvent.click(link);
    expect(mocks.setShowRequestsView).toHaveBeenCalledWith(false);
    expect(screen.getByTestId('location').textContent).toBe('/docs/doc-d1');
  });

  it('counts open notifications on the Other tab and shows them at /inbox/other', async () => {
    mocks.fetchNotificationsCount.mockResolvedValue({ open_count: 2 });
    mocks.fetchNotifications.mockResolvedValue({
      notifications: [{
        id: 'n1',
        kind: 'guides_converted',
        title: 'Your guides were converted to skills',
        body: 'Details',
        data: null,
        created_at: '2026-11-06T10:00:00',
        dismissed_at: null,
      }],
      open_count: 1,
    });
    renderView('/inbox');
    await waitFor(() => expect(otherTabCount()).toBe('2'));
    expect(mocks.fetchNotifications).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: /Other/ }));
    expect(screen.getByTestId('location').textContent).toBe('/inbox/other');
    expect(screen.getByRole('tab', { name: /Other/ }).getAttribute('aria-selected')).toBe('true');
    expect(await screen.findByRole('article', { name: 'Your guides were converted to skills' })).not.toBeNull();
    // The Requests tab's status filters belong to that tab only.
    expect(screen.queryByText('Executed')).toBeNull();

    emit({ type: 'notifications_changed', open_count: 0 });
    expect(otherTabCount()).toBeNull();
  });
});
