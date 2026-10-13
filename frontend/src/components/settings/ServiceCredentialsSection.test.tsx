// ServiceCredentialsSection regression after the SchemaForm extraction: the
// admin form still renders the stored values, keeps the secret "leave blank"
// semantics, honours visible_if / required_if, and saves the same flat
// {field key: value} payload as before.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ServiceCredentialsSection } from './ServiceCredentialsSection';
import type { ServiceCredentialDetail } from '../../api/types';

const mocks = vi.hoisted(() => ({
  fetchServiceCredentials: vi.fn(),
  updateServiceCredentials: vi.fn(),
}));

vi.mock('../../api/client', () => ({
  fetchServiceCredentials: mocks.fetchServiceCredentials,
  updateServiceCredentials: mocks.updateServiceCredentials,
}));

const SMTP: ServiceCredentialDetail = {
  service: 'smtp',
  label: 'SMTP',
  configured: true,
  source: 'store',
  fields: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'smtp.example.com', required: true, required_if: null, visible_if: null },
    { key: 'password', label: 'Password', type: 'secret', placeholder: 'SMTP password', required: false, required_if: null, visible_if: null },
    { key: 'use_relay', label: 'Use relay', type: 'bool', placeholder: '', required: false, required_if: null, visible_if: null },
    { key: 'relay', label: 'Relay', type: 'text', placeholder: '', required: false, required_if: 'use_relay', visible_if: 'use_relay' },
    { key: 'notes', label: 'Notes', type: 'textarea', placeholder: '', required: false, required_if: null, visible_if: null },
  ],
  credentials: { host: 'smtp.a.com', password_set: true, use_relay: false, relay: '', notes: 'n' },
};

const COINGECKO: ServiceCredentialDetail = {
  service: 'coingecko',
  label: 'CoinGecko',
  configured: false,
  source: null,
  fields: [
    { key: 'api_key', label: 'API key', type: 'secret', placeholder: 'CoinGecko Pro API key', required: true, required_if: null, visible_if: null },
  ],
  credentials: { api_key_set: false },
};

const IMAP: ServiceCredentialDetail = {
  service: 'imap',
  label: 'IMAP mailbox',
  configured: true,
  source: 'store',
  fields: [
    {
      key: 'allowed_hosts', label: 'IMAP servers', type: 'list', placeholder: '', required: true,
      required_if: null, visible_if: null, item_label: 'server', max_items: 0,
      item_fields: [
        { key: 'host', label: 'Host', type: 'text', placeholder: 'imap.example.com', required: true },
        { key: 'security', label: 'Security', type: 'select', placeholder: '', required: true, options: ['ssl', 'starttls'], default: 'ssl' },
        { key: 'port', label: 'Port', type: 'text', placeholder: 'Default', required: false },
      ],
    },
  ],
  credentials: {
    allowed_hosts: [
      { host: 'imap.a.com', security: 'ssl', port: '993' },
      { host: 'imap.b.com', security: 'starttls', port: '143' },
    ],
  },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ServiceCredentialsSection', () => {
  it('edits a configured service and saves the flat payload', async () => {
    mocks.fetchServiceCredentials.mockResolvedValue({ services: [SMTP, COINGECKO] });
    mocks.updateServiceCredentials.mockImplementation(async () => ({
      ...SMTP,
      credentials: { ...SMTP.credentials, host: 'smtp.b.com', use_relay: true, relay: 'r.example' },
    }));
    render(<ServiceCredentialsSection />);

    // Summary card: secret masked, text listed.
    expect(await screen.findByText('smtp.a.com')).toBeTruthy();
    expect(screen.getByLabelText('Set')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const host = screen.getByLabelText('Host') as HTMLInputElement;
    expect(host.id).toBe('svc-cred-smtp-host');
    expect(host.value).toBe('smtp.a.com');
    const password = screen.getByLabelText(/Password/) as HTMLInputElement;
    expect(password.value).toBe('');
    expect(password.placeholder).toBe('Leave blank to keep the stored value');
    expect((screen.getByLabelText(/Notes/) as HTMLTextAreaElement).value).toBe('n');
    expect(screen.queryByLabelText(/Relay/)).toBeNull();

    fireEvent.change(host, { target: { value: 'smtp.b.com' } });
    fireEvent.click(screen.getByLabelText('Use relay'));
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    // required_if: Relay is now required and empty, so Save is disabled.
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Relay'), { target: { value: 'r.example' } });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);

    await waitFor(() => expect(mocks.updateServiceCredentials).toHaveBeenCalledTimes(1));
    expect(mocks.updateServiceCredentials).toHaveBeenCalledWith('smtp', {
      host: 'smtp.b.com', password: '', use_relay: true, relay: 'r.example', notes: 'n',
    });
    // Back to the summary with the saved values.
    expect(await screen.findByText('smtp.b.com')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
  });

  it('configures an unconfigured service from the add grid', async () => {
    mocks.fetchServiceCredentials.mockResolvedValue({ services: [SMTP, COINGECKO] });
    mocks.updateServiceCredentials.mockResolvedValue({ ...COINGECKO, configured: true, source: 'store', credentials: { api_key_set: true } });
    render(<ServiceCredentialsSection />);
    fireEvent.click(await screen.findByTitle('Configure CoinGecko'));
    const key = screen.getByLabelText('API key') as HTMLInputElement;
    expect(key.placeholder).toBe('CoinGecko Pro API key');
    // Required secrets are left to the server: Save stays enabled.
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.change(key, { target: { value: 'cg-123' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(mocks.updateServiceCredentials).toHaveBeenCalledWith('coingecko', { api_key: 'cg-123' }),
    );
  });

  it('shows the server error and keeps the form open on a failed save', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    mocks.fetchServiceCredentials.mockResolvedValue({ services: [SMTP] });
    mocks.updateServiceCredentials.mockRejectedValue(new Error('Host is invalid.'));
    render(<ServiceCredentialsSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Host is invalid.')).toBeTruthy();
    expect(screen.getByLabelText('Host')).toBeTruthy();
  });
});

describe('ServiceCredentialsSection list fields', () => {
  it('summarises the entries and saves the edited entry array', async () => {
    mocks.fetchServiceCredentials.mockResolvedValue({ services: [IMAP] });
    mocks.updateServiceCredentials.mockImplementation(async (_service: string, values: unknown) => ({
      ...IMAP,
      credentials: values,
    }));
    render(<ServiceCredentialsSection />);

    // Summary card: one line per entry with its details.
    expect(await screen.findByText('imap.a.com')).toBeTruthy();
    expect(screen.getByText('Security: starttls')).toBeTruthy();
    expect(screen.getByText('Port: 143')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('button', { name: 'Edit imap.a.com' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Remove imap.a.com' }));
    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }));
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'imap.c.com' } });
    fireEvent.change(screen.getByLabelText('Security'), { target: { value: 'starttls' } });
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(mocks.updateServiceCredentials).toHaveBeenCalledTimes(1));
    expect(mocks.updateServiceCredentials).toHaveBeenCalledWith('imap', {
      allowed_hosts: [
        { host: 'imap.b.com', security: 'starttls', port: '143' },
        { host: 'imap.c.com', security: 'starttls', port: '' },
      ],
    });
    // Back on the summary with the saved entries.
    expect(await screen.findByText('imap.c.com')).toBeTruthy();
    expect(screen.queryByText('imap.a.com')).toBeNull();
  });

  it('disables Save while a required list is empty', async () => {
    mocks.fetchServiceCredentials.mockResolvedValue({
      services: [{ ...IMAP, configured: false, source: null, credentials: { allowed_hosts: [] } }],
    });
    render(<ServiceCredentialsSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'IMAP mailbox' }));
    expect(screen.getByText('No servers yet.')).toBeTruthy();
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }));
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'imap.c.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(save.disabled).toBe(false);
  });
});
