// The inbox's Other tab: open notifications before dismissed ones, the
// body's line breaks kept as text, Dismiss moving a card down (and staying
// down whatever a stale read says), a failed dismiss leaving the card open
// with the error, the list re-read on notifications_changed, and the
// guides_converted link opening Settings > Skills.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { UserNotification } from '../api/types';
import { NotificationsInbox } from './NotificationsInbox';

const mocks = vi.hoisted(() => ({
  fetchNotifications: vi.fn(),
  dismissNotification: vi.fn(),
  setSettingsOpen: vi.fn(),
  setSettingsInitialSection: vi.fn(),
  globalHandlers: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('../api/client', () => ({
  fetchNotifications: mocks.fetchNotifications,
  dismissNotification: mocks.dismissNotification,
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
    setSettingsOpen: mocks.setSettingsOpen,
    setSettingsInitialSection: mocks.setSettingsInitialSection,
  }),
}));

function emit(event: Record<string, unknown>) {
  act(() => {
    mocks.globalHandlers.forEach((handler) => handler(event));
  });
}

function notification(overrides: Partial<UserNotification> = {}): UserNotification {
  return {
    id: 'n1',
    kind: 'guides_converted',
    title: 'Your guides were converted to skills',
    body: 'Your 2 guides are now private skills:\n\n- Analyst\n- Writer',
    data: { skills: [], routines: [] },
    created_at: '2026-11-06T10:00:00',
    dismissed_at: null,
    ...overrides,
  };
}

function list(...rows: UserNotification[]) {
  return { notifications: rows, open_count: rows.filter((r) => !r.dismissed_at).length };
}

function openSection(): HTMLElement {
  return screen.getByRole('region', { name: /^Notifications/ });
}

beforeEach(() => {
  mocks.fetchNotifications.mockReset();
  mocks.dismissNotification.mockReset();
  mocks.setSettingsOpen.mockReset();
  mocks.setSettingsInitialSection.mockReset();
  mocks.globalHandlers.clear();
});

afterEach(cleanup);

describe('NotificationsInbox', () => {
  it('lists open notifications, then the dismissed ones', async () => {
    mocks.fetchNotifications.mockResolvedValue(list(
      notification(),
      notification({ id: 'n0', title: 'An older one', body: '', dismissed_at: '2026-11-01T09:00:00' }),
    ));
    render(<NotificationsInbox />);

    const card = await screen.findByRole('article', { name: 'Your guides were converted to skills' });
    expect(openSection().contains(card)).toBe(true);
    expect(openSection().querySelector('.memory-inbox-section-count')?.textContent).toBe('1');
    // The body is plain text: line breaks and list dashes as sent.
    expect(card.querySelector('.notification-card-body')?.textContent)
      .toBe('Your 2 guides are now private skills:\n\n- Analyst\n- Writer');
    expect(within(card).getByRole('button', { name: 'Dismiss' })).not.toBeNull();

    const dismissedSection = screen.getByRole('region', { name: 'Dismissed' });
    const older = within(dismissedSection).getByRole('article', { name: 'An older one' });
    expect(within(older).queryByRole('button', { name: 'Dismiss' })).toBeNull();
    expect(older.querySelector('.notification-card-body')).toBeNull();
  });

  it('shows the empty state and no Dismissed section without notifications', async () => {
    mocks.fetchNotifications.mockResolvedValue(list());
    render(<NotificationsInbox />);
    expect(await screen.findByText('No new notifications.')).not.toBeNull();
    expect(screen.queryByRole('region', { name: 'Dismissed' })).toBeNull();
  });

  it('moves a dismissed card down and keeps it there over a stale read', async () => {
    mocks.fetchNotifications.mockResolvedValue(list(notification()));
    mocks.dismissNotification.mockResolvedValue({
      notification: notification({ dismissed_at: '2026-11-06T11:00:00' }),
      open_count: 0,
    });
    render(<NotificationsInbox />);
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }));

    await waitFor(() => expect(screen.getByRole('region', { name: 'Dismissed' })).not.toBeNull());
    expect(mocks.dismissNotification).toHaveBeenCalledWith('n1');
    expect(screen.getByText('No new notifications.')).not.toBeNull();
    expect(openSection().querySelector('.memory-inbox-section-count')?.textContent).toBe('0');

    // The server's event re-reads the list; a read that still says "open"
    // (started before the dismissal landed) does not bring the card back.
    emit({ type: 'notifications_changed', open_count: 0 });
    await waitFor(() => expect(mocks.fetchNotifications).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText('No new notifications.')).not.toBeNull());
    expect(within(screen.getByRole('region', { name: 'Dismissed' })).getAllByRole('article')).toHaveLength(1);
  });

  it('keeps the card open and shows the error when a dismiss fails', async () => {
    mocks.fetchNotifications.mockResolvedValue(list(notification()));
    let reject!: (err: Error) => void;
    mocks.dismissNotification.mockReturnValue(new Promise((_resolve, rej) => {
      reject = rej;
    }));
    render(<NotificationsInbox />);
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }));
    const busy = screen.getByRole('button', { name: 'Dismissing...' }) as HTMLButtonElement;
    expect(busy.disabled).toBe(true);

    await act(async () => {
      reject(new Error('Server said no'));
    });
    expect((await screen.findByRole('alert')).textContent).toBe('Server said no');
    expect(screen.getByRole('button', { name: 'Dismiss' })).not.toBeNull();
    expect(screen.queryByRole('region', { name: 'Dismissed' })).toBeNull();
  });

  it('re-reads the list when another tab changes the notifications', async () => {
    mocks.fetchNotifications
      .mockResolvedValueOnce(list(notification()))
      .mockResolvedValueOnce(list(notification({ dismissed_at: '2026-11-06T11:00:00' })));
    render(<NotificationsInbox />);
    await screen.findByRole('button', { name: 'Dismiss' });

    emit({ type: 'memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    expect(mocks.fetchNotifications).toHaveBeenCalledTimes(1);
    emit({ type: 'notifications_changed', open_count: 0 });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull());
    expect(screen.getByRole('region', { name: 'Dismissed' })).not.toBeNull();
  });

  it('reports a failed load', async () => {
    mocks.fetchNotifications.mockRejectedValue(new Error('Network down'));
    render(<NotificationsInbox />);
    expect((await screen.findByRole('alert')).textContent).toBe('Network down');
  });

  it('opens Settings > Skills from a guides_converted notification', async () => {
    mocks.fetchNotifications.mockResolvedValue(list(
      notification(),
      notification({ id: 'n2', kind: 'something_newer', title: 'Unknown kind' }),
    ));
    render(<NotificationsInbox />);
    const card = await screen.findByRole('article', { name: 'Your guides were converted to skills' });

    fireEvent.click(within(card).getByRole('button', { name: 'Open Skills' }));
    expect(mocks.setSettingsInitialSection).toHaveBeenCalledWith('skills');
    expect(mocks.setSettingsOpen).toHaveBeenCalledWith(true);

    // A kind this build does not know still shows, with Dismiss only.
    const unknown = screen.getByRole('article', { name: 'Unknown kind' });
    expect(within(unknown).queryByRole('button', { name: 'Open Skills' })).toBeNull();
    expect(within(unknown).getByRole('button', { name: 'Dismiss' })).not.toBeNull();
  });
});
