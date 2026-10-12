// DataConnectionsSection: the multi-connection cards and add / re-entry form
// (issue #84), plus regressions for the unchanged single-connection rows.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { DataConnectionsSection } from './DataConnectionsSection';
import type { ConnectionRow, ConnectorRow } from '../../api/types';

const mocks = vi.hoisted(() => ({
  fetchConnectors: vi.fn(),
  saveConnectorKey: vi.fn(),
  disconnectConnector: vi.fn(),
  createConnection: vi.fn(),
  updateConnection: vi.fn(),
  deleteConnection: vi.fn(),
  openOAuthPopup: vi.fn(),
}));

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return {
    ConnectionApiError: actual.ConnectionApiError,
    fetchConnectors: mocks.fetchConnectors,
    saveConnectorKey: mocks.saveConnectorKey,
    disconnectConnector: mocks.disconnectConnector,
    createConnection: mocks.createConnection,
    updateConnection: mocks.updateConnection,
    deleteConnection: mocks.deleteConnection,
  };
});

vi.mock('../../utils/oauthPopup', () => ({ openOAuthPopup: mocks.openOAuthPopup }));

// Imported after vi.mock so it is the real class the component checks against.
const { ConnectionApiError } = await import('../../api/client');

const GOOGLE: ConnectorRow = {
  service: 'google', label: 'Google', description: 'Gmail, Calendar, Drive', kind: 'oauth',
  connect_url: '/auth/google?popup=1', connected: true, needs_reauth: false,
};
const SLACK: ConnectorRow = {
  service: 'slack', label: 'Slack', kind: 'oauth', connect_url: '/auth/slack?popup=1', connected: false,
};
const AIRTABLE: ConnectorRow = {
  service: 'airtable', label: 'Airtable', kind: 'api_key', connected: true, key_hint: 'abcd',
  key_url: '/auth/airtable-key', key_field: 'token', disconnect_url: '/auth/airtable-key/remove',
};
const TAILSCALE: ConnectorRow = {
  service: 'tailscale', label: 'Tailscale', kind: 'api_key', connected: false,
  key_url: '/auth/service-key/tailscale', key_field: 'api_key', key_placeholder: 'tskey-...',
  key_help: 'Create a read-only OAuth client.',
  key_help_links: [{ label: 'OAuth clients', url: 'https://login.tailscale.com/admin/settings/oauth' }],
};
const HIDDEN: ConnectorRow = {
  service: 'ramp', label: 'Ramp', kind: 'oauth', connect_url: '/auth/ramp?popup=1', connected: false, available: false,
};

const WORK: ConnectionRow = {
  id: 1, account: 'work@example.com', description: 'main work mailbox', is_default: true,
  needs_reauth: false, warning: null, created_at: '2026-10-01T00:00:00',
};
const HOME: ConnectionRow = {
  id: 2, account: 'me@home.example', description: null, is_default: false,
  needs_reauth: true, warning: 'imap.old.com is no longer allowed by your admin.', created_at: '2026-10-02T00:00:00',
};

function imap(connections: ConnectionRow[] = [WORK, HOME], limit = 10): ConnectorRow {
  return {
    service: 'imap', label: 'IMAP mailbox', kind: 'form', available: true,
    connected: connections.length > 0, multi_connection: true, connection_limit: limit,
    connections, connections_url: '/auth/connections/imap',
    form_fields: [
      { key: 'host', label: 'IMAP server', type: 'select', placeholder: '', required: true, required_if: null, visible_if: null, options: ['imap.a.com', 'imap.b.com'] },
      { key: 'username', label: 'Username', type: 'text', placeholder: 'you@example.com', required: true, required_if: null, visible_if: null, options: [] },
      { key: 'password', label: 'Password', type: 'secret', placeholder: 'App password', required: true, required_if: null, visible_if: null, options: [] },
    ],
  };
}

function serve(...connectors: ConnectorRow[]) {
  mocks.fetchConnectors.mockResolvedValue({ connectors });
}

const refreshConnectionStatus = vi.fn();

async function renderSection() {
  render(<DataConnectionsSection refreshConnectionStatus={refreshConnectionStatus} />);
  await screen.findByRole('heading', { name: 'Data Connections' });
  await waitFor(() => expect(screen.queryByText('Loading connections...')).toBeNull());
}

function card(id: number): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-connection-id="${id}"]`);
  if (!el) throw new Error(`no card ${id}`);
  return el;
}

async function openForm(label = 'IMAP mailbox') {
  fireEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));
  fireEvent.click(screen.getByRole('button', { name: new RegExp(label) }));
  await screen.findByText(`Connect ${label}`);
}

function fillImapForm() {
  fireEvent.change(screen.getByLabelText('IMAP server'), { target: { value: 'imap.b.com' } });
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'new@example.com' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'hunter2' } });
}

beforeEach(() => {
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe('DataConnectionsSection: single-connection rows (unchanged)', () => {
  it('lists connected rows with their actions and hides unavailable ones', async () => {
    serve(GOOGLE, SLACK, AIRTABLE, TAILSCALE, HIDDEN);
    await renderSection();
    const list = document.querySelector('.connectors-list')!;
    const rows = list.querySelectorAll('.connector-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByText('Google')).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText('Gmail, Calendar, Drive')).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText('Connected')).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText('...abcd')).toBeTruthy();
    expect(screen.queryByText('Ramp')).toBeNull();
  });

  it('reconnects an oauth row through the popup', async () => {
    serve(GOOGLE);
    mocks.openOAuthPopup.mockReturnValue({ closed: false, close: vi.fn() });
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
    expect(mocks.openOAuthPopup).toHaveBeenCalledWith('/auth/google?popup=1');
  });

  it('disconnects an api_key row after confirming', async () => {
    serve(AIRTABLE);
    mocks.disconnectConnector.mockResolvedValue(undefined);
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(window.confirm).toHaveBeenCalledWith('Remove Airtable connection?');
    await waitFor(() => expect(mocks.disconnectConnector).toHaveBeenCalledWith('/auth/airtable-key/remove'));
    await waitFor(() => expect(refreshConnectionStatus).toHaveBeenCalled());
  });

  it('shows not-connected services as picker tiles', async () => {
    serve(GOOGLE, SLACK, TAILSCALE);
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));
    const tiles = document.querySelectorAll('.add-connection-tile');
    expect([...tiles].map((t) => t.textContent)).toEqual(['SlackSign in', 'TailscaleAPI key']);
  });

  it('saves a key in the key step', async () => {
    serve(TAILSCALE);
    mocks.saveConnectorKey.mockResolvedValue(undefined);
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));
    fireEvent.click(screen.getByRole('button', { name: /Tailscale/ }));
    expect(screen.getByText('Connect Tailscale')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'OAuth clients' })).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText('tskey-...'), { target: { value: ' tskey-api-1 ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(mocks.saveConnectorKey).toHaveBeenCalledWith('/auth/service-key/tailscale', 'api_key', 'tskey-api-1'),
    );
    await waitFor(() => expect(screen.queryByText('Connect Tailscale')).toBeNull());
  });
});

describe('DataConnectionsSection: multi-connection cards', () => {
  it('renders one card per connection, default first', async () => {
    serve(imap());
    await renderSection();
    const group = screen.getByRole('group', { name: 'IMAP mailbox connections' });
    const cards = group.querySelectorAll('[data-connection-id]');
    expect([...cards].map((c) => c.getAttribute('data-connection-id'))).toEqual(['1', '2']);

    const work = within(card(1));
    expect(work.getByText('work@example.com')).toBeTruthy();
    expect(work.getByText('IMAP mailbox')).toBeTruthy();
    expect((work.getByLabelText('Description for work@example.com') as HTMLInputElement).value).toBe('main work mailbox');
    expect(work.getByText('Default')).toBeTruthy();
    expect(work.queryByRole('button', { name: 'Make default' })).toBeNull();
    expect(work.queryByText('Needs re-entry')).toBeNull();

    const home = within(card(2));
    expect(home.queryByText('Default')).toBeNull();
    expect(home.getByRole('button', { name: 'Make default' })).toBeTruthy();
    expect(home.getByText('Needs re-entry')).toBeTruthy();
    expect(home.getByText('imap.old.com is no longer allowed by your admin.')).toBeTruthy();
  });

  it('labels a legacy connection without an account', async () => {
    serve(imap([{ ...WORK, account: '' }]));
    await renderSection();
    expect(within(card(1)).getByText('(unnamed connection)')).toBeTruthy();
  });

  it('offers no service-wide Disconnect, only per-card ones', async () => {
    serve(imap());
    await renderSection();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Disconnect work@example.com' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Disconnect me@home.example' })).toBeTruthy();
  });

  it('saves the description on blur and skips an unchanged one', async () => {
    serve(imap());
    mocks.updateConnection.mockResolvedValue({ ...WORK, description: 'renamed' });
    await renderSection();
    const input = screen.getByLabelText('Description for work@example.com') as HTMLInputElement;

    fireEvent.focus(input);
    fireEvent.blur(input);
    expect(mocks.updateConnection).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: '  renamed  ' } });
    fireEvent.blur(input);
    await waitFor(() =>
      expect(mocks.updateConnection).toHaveBeenCalledWith('/auth/connections/imap', 1, { description: 'renamed' }),
    );
    await waitFor(() => expect(mocks.fetchConnectors).toHaveBeenCalledTimes(2));
    expect(refreshConnectionStatus).toHaveBeenCalled();
  });

  it('saves on Enter and clears an emptied description to null', async () => {
    serve(imap());
    mocks.updateConnection.mockResolvedValue({ ...WORK, description: null });
    await renderSection();
    const input = screen.getByLabelText('Description for work@example.com') as HTMLInputElement;
    input.focus();
    fireEvent.change(input, { target: { value: '   ' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() =>
      expect(mocks.updateConnection).toHaveBeenCalledWith('/auth/connections/imap', 1, { description: null }),
    );
  });

  it('reverts on Escape without saving', async () => {
    serve(imap());
    await renderSection();
    const input = screen.getByLabelText('Description for work@example.com') as HTMLInputElement;
    input.focus();
    fireEvent.change(input, { target: { value: 'something else' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(input.value).toBe('main work mailbox');
    fireEvent.blur(input);
    expect(mocks.updateConnection).not.toHaveBeenCalled();
  });

  it('shows a description save error', async () => {
    serve(imap());
    mocks.updateConnection.mockRejectedValue(
      new ConnectionApiError('Invalid description: too long.', 400, { code: 'invalid_description' }),
    );
    await renderSection();
    const input = screen.getByLabelText('Description for work@example.com');
    fireEvent.change(input, { target: { value: 'x' } });
    fireEvent.blur(input);
    expect(await within(card(1)).findByText('Invalid description: too long.')).toBeTruthy();
  });

  it('makes a connection the default', async () => {
    serve(imap());
    mocks.updateConnection.mockResolvedValue({ ...HOME, is_default: true });
    await renderSection();
    fireEvent.click(within(card(2)).getByRole('button', { name: 'Make default' }));
    await waitFor(() =>
      expect(mocks.updateConnection).toHaveBeenCalledWith('/auth/connections/imap', 2, { is_default: true }),
    );
    await waitFor(() => expect(mocks.fetchConnectors).toHaveBeenCalledTimes(2));
  });

  it('deletes a connection after confirming', async () => {
    serve(imap());
    mocks.deleteConnection.mockResolvedValue(undefined);
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect me@home.example' }));
    expect(window.confirm).toHaveBeenCalledWith('Remove the IMAP mailbox connection me@home.example?');
    await waitFor(() => expect(mocks.deleteConnection).toHaveBeenCalledWith('/auth/connections/imap', 2));
    await waitFor(() => expect(refreshConnectionStatus).toHaveBeenCalled());
  });

  it('does nothing when the delete is not confirmed', async () => {
    serve(imap());
    vi.mocked(window.confirm).mockReturnValue(false);
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect me@home.example' }));
    expect(mocks.deleteConnection).not.toHaveBeenCalled();
  });

  it('re-enters the password of a connection with PUT {values}', async () => {
    serve(imap());
    mocks.updateConnection.mockResolvedValue({ ...HOME, needs_reauth: false });
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect me@home.example' }));
    const home = within(card(2));
    // Only the secret field: the account-determining fields stay as stored.
    expect(home.queryByLabelText('IMAP server')).toBeNull();
    expect(home.queryByLabelText('Username')).toBeNull();
    const save = home.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(home.getByLabelText('Password'), { target: { value: 'new-pass' } });
    fireEvent.click(save);
    await waitFor(() =>
      expect(mocks.updateConnection).toHaveBeenCalledWith('/auth/connections/imap', 2, {
        values: { password: 'new-pass' },
      }),
    );
    await waitFor(() => expect(home.queryByLabelText('Password')).toBeNull());
  });

  it('shows an account_mismatch from the re-entry inline', async () => {
    serve(imap());
    mocks.updateConnection.mockRejectedValue(
      new ConnectionApiError('These settings belong to x@y.com, not me@home.example.', 400, { code: 'account_mismatch' }),
    );
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect me@home.example' }));
    const home = within(card(2));
    fireEvent.change(home.getByLabelText('Password'), { target: { value: 'p' } });
    fireEvent.click(home.getByRole('button', { name: 'Save' }));
    expect((await home.findByRole('alert')).textContent).toContain('belong to x@y.com');
  });

  it('opens the popup to reconnect an oauth-kind connection', async () => {
    serve({ ...imap(), service: 'm365', label: 'Outlook', kind: 'oauth', connect_url: '/auth/m365?popup=1', form_fields: undefined });
    mocks.openOAuthPopup.mockReturnValue({ closed: false, close: vi.fn() });
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect work@example.com' }));
    expect(mocks.openOAuthPopup).toHaveBeenCalledWith('/auth/m365?popup=1');
    expect(screen.getAllByText('Update Available')).toHaveLength(1);
  });
});

describe('DataConnectionsSection: multi-connection add flow', () => {
  it('keeps the service in the picker while under its limit', async () => {
    serve(imap([WORK, HOME], 3));
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));
    expect(screen.getByRole('button', { name: /IMAP mailbox/ }).textContent).toContain('Account details');
  });

  it('drops it from the picker at the limit', async () => {
    serve(imap([WORK, HOME], 2), SLACK);
    await renderSection();
    fireEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));
    expect(screen.queryByRole('button', { name: /IMAP mailbox/ })).toBeNull();
    expect(screen.getByRole('button', { name: /Slack/ })).toBeTruthy();
  });

  it('renders the form fields and an optional Description', async () => {
    serve(imap([]));
    await renderSection();
    expect(screen.getByText(/No connections yet/)).toBeTruthy();
    await openForm();
    const host = screen.getByLabelText('IMAP server') as HTMLSelectElement;
    expect([...host.options].map((o) => o.value)).toEqual(['', 'imap.a.com', 'imap.b.com']);
    expect(screen.getByLabelText(/Description/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('posts the values and description, then closes', async () => {
    serve(imap([]));
    mocks.createConnection.mockResolvedValue({ ...WORK, id: 9 });
    await renderSection();
    await openForm();
    fillImapForm();
    fireEvent.change(screen.getByLabelText(/Description/), { target: { value: ' personal ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() =>
      expect(mocks.createConnection).toHaveBeenCalledWith('/auth/connections/imap', {
        values: { host: 'imap.b.com', username: 'new@example.com', password: 'hunter2' },
        description: 'personal',
      }),
    );
    await waitFor(() => expect(screen.queryByText('Connect IMAP mailbox')).toBeNull());
    expect(refreshConnectionStatus).toHaveBeenCalled();
  });

  it('shows a verify_failed message inline and keeps the form', async () => {
    serve(imap([]));
    mocks.createConnection.mockRejectedValue(
      new ConnectionApiError('Login failed: authentication failed.', 400, { code: 'verify_failed' }),
    );
    await renderSection();
    await openForm();
    fillImapForm();
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect((await screen.findByRole('alert')).textContent).toBe('Login failed: authentication failed.');
    expect((screen.getByLabelText('Username') as HTMLInputElement).value).toBe('new@example.com');
  });

  it('maps invalid_values onto the fields', async () => {
    serve(imap([]));
    mocks.createConnection.mockRejectedValue(
      new ConnectionApiError('Some fields are invalid.', 400, {
        code: 'invalid_values',
        fields: { host: 'Not one of the allowed choices.' },
      }),
    );
    await renderSection();
    await openForm();
    fillImapForm();
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect((await screen.findByRole('alert')).textContent).toBe('Some fields need attention.');
    const host = screen.getByLabelText('IMAP server');
    expect(host.getAttribute('aria-invalid')).toBe('true');
    expect(document.getElementById(host.getAttribute('aria-describedby')!)?.textContent).toBe(
      'Not one of the allowed choices.',
    );
  });

  it('points at the existing card on connection_exists and keeps the values', async () => {
    serve(imap([WORK]));
    mocks.createConnection.mockRejectedValue(
      new ConnectionApiError('work@example.com is already connected.', 409, {
        code: 'connection_exists', account: 'work@example.com', connectionId: 1,
      }),
    );
    const scroll = vi.fn();
    await renderSection();
    card(1).scrollIntoView = scroll;
    await openForm();
    fillImapForm();
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('This account is already connected.');
    vi.useFakeTimers();
    try {
      fireEvent.click(within(alert).getByRole('button', { name: 'Show it' }));
      expect(scroll).toHaveBeenCalled();
      expect(card(1).className).toContain('connection-card-highlight');
      expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('hunter2');
      act(() => {
        vi.advanceTimersByTime(3000);
      });
      expect(card(1).className).not.toContain('connection-card-highlight');
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows the 429 retry delay', async () => {
    serve(imap([]));
    mocks.createConnection.mockRejectedValue(
      new ConnectionApiError('Too many connection attempts.', 429, { code: 'rate_limited', retryAfter: 42 }),
    );
    await renderSection();
    await openForm();
    fillImapForm();
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect((await screen.findByRole('alert')).textContent).toBe('Too many attempts; try again in 42 seconds.');
  });

  it('shows the connection limit and network errors', async () => {
    serve(imap([]));
    mocks.createConnection
      .mockRejectedValueOnce(new ConnectionApiError('At most 10.', 400, { code: 'connection_limit', limit: 10 }))
      .mockRejectedValueOnce(new ConnectionApiError('Failed to fetch', 0, { code: 'network_error' }));
    await renderSection();
    await openForm();
    fillImapForm();
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect((await screen.findByRole('alert')).textContent).toBe('You can connect at most 10 IMAP mailbox accounts.');
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Could not reach the server/));
  });

  it('adds a multi-connection api_key service with POST {api_key}', async () => {
    const keyed: ConnectorRow = {
      service: 'acme', label: 'Acme', kind: 'api_key', connected: false, multi_connection: true,
      connection_limit: 5, connections: [], connections_url: '/auth/connections/acme',
    };
    serve(keyed);
    mocks.createConnection.mockResolvedValue({ ...WORK, id: 4 });
    await renderSection();
    await openForm('Acme');
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: ' k-123 ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() =>
      expect(mocks.createConnection).toHaveBeenCalledWith('/auth/connections/acme', { api_key: 'k-123' }),
    );
  });

  it('falls back to the picker when the service reaches its limit meanwhile', async () => {
    serve(imap([WORK], 2), SLACK);
    await renderSection();
    await openForm();
    mocks.fetchConnectors.mockResolvedValue({ connectors: [imap([WORK, HOME], 2), SLACK] });
    // Any refresh (here: another card's write) re-reads the list.
    mocks.updateConnection.mockResolvedValue({ ...WORK });
    const input = screen.getByLabelText('Description for work@example.com');
    fireEvent.change(input, { target: { value: 'changed' } });
    fireEvent.blur(input);
    await screen.findByText('Add a connection');
    expect(screen.queryByText('Connect IMAP mailbox')).toBeNull();
  });
});
