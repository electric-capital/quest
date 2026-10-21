// ConnectRequestMessage: the inline connect card. Open oauth cards offer
// Connect / Not now; Not now declines through the resolve endpoint and
// collapses the card; a check that comes back unconnected keeps the card
// open with a retry row; non-oauth kinds send the user to Settings; a
// wait_handle_resolved event for the card's handle flips it in place.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ConnectRequestMessage as ConnectRequestMessageType } from '../api/types';

const resolveConnectRequest = vi.fn();
const refreshConnectionStatus = vi.fn();
const setSettingsOpen = vi.fn();
const openOAuthPopup = vi.fn();
let globalListener: ((event: Record<string, unknown>) => void) | null = null;

vi.mock('../api/client', () => ({
  resolveConnectRequest: (...args: unknown[]) => resolveConnectRequest(...args),
}));
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ refreshConnectionStatus }),
}));
vi.mock('../contexts/NavigationContext', () => ({
  useNavigationState: () => ({ isSettingsOpen: false, setSettingsOpen }),
}));
vi.mock('../utils/oauthPopup', () => ({
  openOAuthPopup: (...args: unknown[]) => openOAuthPopup(...args),
}));
vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (listener: (event: Record<string, unknown>) => void) => {
      globalListener = listener;
      return () => { globalListener = null; };
    },
  },
}));

import { ConnectRequestMessage } from './ConnectRequestMessage';

function card(overrides: Partial<ConnectRequestMessageType> = {}): ConnectRequestMessageType {
  return {
    type: 'connect_request',
    wait_handle_id: 'wh-1',
    status: 'open',
    service: 'google_services',
    label: 'Google Services',
    description: 'Gmail, Calendar, Drive, Docs, Sheets',
    kind: 'oauth',
    connect_url: '/auth/google-services?popup=1',
    reason: 'To search your inbox for the Acme invoice',
    timestamp: '2026-10-21T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  resolveConnectRequest.mockReset();
  refreshConnectionStatus.mockReset();
  setSettingsOpen.mockReset();
  openOAuthPopup.mockReset();
  // The mount-time handle refresh: keep the card open.
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ status: 'pending' }),
  })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  globalListener = null;
});

describe('ConnectRequestMessage', () => {
  it('renders the reason with Connect and Not now for an open oauth card', () => {
    render(<ConnectRequestMessage message={card()} />);
    expect(screen.getByText('Connect Google Services')).toBeTruthy();
    expect(screen.getByText('To search your inbox for the Acme invoice')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Not now' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull();
  });

  it('Connect opens the popup at the card connect_url', () => {
    openOAuthPopup.mockReturnValue({ closed: true, close: vi.fn() });
    render(<ConnectRequestMessage message={card()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(openOAuthPopup).toHaveBeenCalledWith('/auth/google-services?popup=1');
  });

  it('Not now declines and collapses the card', async () => {
    resolveConnectRequest.mockResolvedValue({ resolved: true, status: 'declined', service: 'google_services' });
    render(<ConnectRequestMessage message={card()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
    await waitFor(() => expect(screen.getByText('Not now:')).toBeTruthy());
    expect(resolveConnectRequest).toHaveBeenCalledWith('wh-1', 'decline');
    expect(screen.queryByRole('button', { name: 'Connect' })).toBeNull();
  });

  it('a check that finds the service unconnected keeps the card open with a retry', async () => {
    resolveConnectRequest.mockResolvedValue({ resolved: false, status: 'not_connected', service: 'google_services' });
    // A popup that is already closed makes the poll call check() at once.
    openOAuthPopup.mockReturnValue({ closed: true, close: vi.fn() });
    vi.useFakeTimers();
    try {
      render(<ConnectRequestMessage message={card()} />);
      fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
      await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeTruthy());
    expect(resolveConnectRequest).toHaveBeenCalledWith('wh-1', 'check');
    expect(screen.getByText(/not connected yet/)).toBeTruthy();
    expect(refreshConnectionStatus).not.toHaveBeenCalled();
  });

  it('a successful check collapses to Connected and refreshes the app status', async () => {
    resolveConnectRequest.mockResolvedValue({ resolved: true, status: 'connected', service: 'google_services' });
    openOAuthPopup.mockReturnValue({ closed: true, close: vi.fn() });
    vi.useFakeTimers();
    try {
      render(<ConnectRequestMessage message={card()} />);
      fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
      await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() => expect(screen.getByText('Connected:')).toBeTruthy());
    expect(refreshConnectionStatus).toHaveBeenCalled();
  });

  it('a non-oauth kind sends the user to Settings instead of a popup', () => {
    render(<ConnectRequestMessage message={card({ kind: 'api_key', connect_url: undefined, service: 'airtable', label: 'Airtable' })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Open Settings' }));
    expect(setSettingsOpen).toHaveBeenCalledWith(true);
    expect(openOAuthPopup).not.toHaveBeenCalled();
  });

  it('flips on a wait_handle_resolved event for its handle only', async () => {
    render(<ConnectRequestMessage message={card()} />);
    await waitFor(() => expect(globalListener).not.toBeNull());
    act(() => {
      globalListener?.({ type: 'wait_handle_resolved', handle_id: 'other', status: 'cancelled' });
    });
    expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy();
    act(() => {
      globalListener?.({ type: 'wait_handle_resolved', handle_id: 'wh-1', status: 'cancelled' });
    });
    expect(screen.getByText('Cancelled:')).toBeTruthy();
  });

  it('renders a resolved card collapsed', () => {
    render(<ConnectRequestMessage message={card({ status: 'connected' })} />);
    expect(screen.getByText('Connected:')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
});
