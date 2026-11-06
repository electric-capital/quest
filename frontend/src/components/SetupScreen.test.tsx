// SetupScreen: the first-run setup flow. The unlock step posts the
// encryption password; the steps collect the answers; Finish posts them all
// at once, and a validation error jumps back to the step owning the field.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SetupScreen, reloadHooks } from './SetupScreen';
import { ApiClientError } from '../api/request';

const mocks = vi.hoisted(() => ({
  unlockSetup: vi.fn(),
  completeSetup: vi.fn(),
}));

vi.mock('../api/client', () => ({
  unlockSetup: mocks.unlockSetup,
  completeSetup: mocks.completeSetup,
}));

vi.mock('../contexts/AppConfigContext', () => ({
  useAppConfig: () => ({ appName: 'Quest' }),
}));

const DEFAULTS = {
  login_method: 'password',
  login_method_configured: false,
  admin_emails: [],
  allowed_login_domain: '',
  allowed_login_emails: [],
  app_base_url: 'https://quest.example.com',
  app_base_url_configured: false,
  google_oauth_configured: false,
  google_client_id: '',
  google_project_id: '',
  redirect_paths: ['/auth/callback', '/auth/google-services/callback'],
  llm_configured: false,
  vertex_project_id: '',
  vertex_key_present: false,
  public_email_domains: ['gmail.com'],
};

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function next() {
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
}

async function unlock() {
  mocks.unlockSetup.mockResolvedValue({ token: 'tok', defaults: DEFAULTS });
  render(<SetupScreen />);
  type('Encryption password', 'hunter2');
  fireEvent.click(screen.getByRole('button', { name: 'Unlock setup' }));
  await screen.findByText('How will people sign in?');
}

describe('SetupScreen', () => {
  beforeEach(() => {
    vi.spyOn(reloadHooks, 'reloadAfterSetup').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.restoreAllMocks();
  });

  it('shows the unlock error for a wrong encryption password', async () => {
    mocks.unlockSetup.mockRejectedValue(new ApiClientError('That is not the encryption password', 401, 'wrong_password'));
    render(<SetupScreen />);
    type('Encryption password', 'nope');
    fireEvent.click(screen.getByRole('button', { name: 'Unlock setup' }));
    expect((await screen.findByRole('alert')).textContent).toContain('That is not the encryption password');
    expect(mocks.unlockSetup).toHaveBeenCalledWith('nope');
  });

  it('walks the password flow and submits every answer at once', async () => {
    await unlock();
    next(); // email + password is the default method

    expect(screen.getByText('Administrators and access')).toBeTruthy();
    type('Your email address', 'Me@OtherCo.com');
    // The domain suggestion comes from the admin's address.
    fireEvent.click(screen.getByRole('button', { name: 'otherco.com' }));
    expect((screen.getByLabelText('Email domain allowed to sign in') as HTMLInputElement).value).toBe('otherco.com');
    type('Other administrators', 'ops@otherco.com');
    type('Other email addresses allowed to sign in', 'kid@gmail.com');
    next();

    expect(screen.getByText('Public address')).toBeTruthy();
    expect((screen.getByLabelText('Public URL') as HTMLInputElement).value).toBe('https://quest.example.com');
    next();

    expect(screen.getByText('Your password')).toBeTruthy();
    type('Password', 'short');
    type('Repeat the password', 'short');
    next();
    expect(screen.getByRole('alert').textContent).toContain('at least 8 characters');
    type('Password', 'correct horse battery');
    type('Repeat the password', 'correct horse');
    next();
    expect(screen.getByRole('alert').textContent).toContain('do not match');
    type('Repeat the password', 'correct horse battery');
    next();

    expect(screen.getByText('Models')).toBeTruthy();
    type('Service-account key JSON', '{"type": "service_account", "project_id": "p"}');
    next();

    expect(screen.getByText('Review')).toBeTruthy();
    expect(screen.getByText('Me@OtherCo.com, ops@otherco.com')).toBeTruthy();
    mocks.completeSetup.mockResolvedValue({ login_method: 'password', signed_in: true });
    fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }));

    await screen.findByText('Setup complete');
    expect(mocks.completeSetup).toHaveBeenCalledWith({
      token: 'tok',
      login_method: 'password',
      admin_emails: ['Me@OtherCo.com', 'ops@otherco.com'],
      allowed_login_domain: 'otherco.com',
      allowed_login_emails: ['kid@gmail.com'],
      app_base_url: 'https://quest.example.com',
      admin_password: 'correct horse battery',
      google_client_id: undefined,
      google_client_secret: undefined,
      google_project_id: undefined,
      vertex_service_account: { type: 'service_account', project_id: 'p' },
      vertex_project_id: '',
    });
    expect(screen.getByRole('status').textContent).toContain('You are signed in');
  });

  it('collects the OAuth client under Google sign-in and lists the redirect URIs', async () => {
    await unlock();
    fireEvent.click(screen.getByRole('radio', { name: /^Google sign-in/ }));
    next();
    type('Your email address', 'me@otherco.com');
    next();
    type('Public URL', 'quest.otherco.com');
    next();

    expect(screen.getByText('Google OAuth client')).toBeTruthy();
    expect(screen.getByText('quest.otherco.com/auth/callback')).toBeTruthy();
    expect(screen.getByText('quest.otherco.com/auth/google-services/callback')).toBeTruthy();
    next();
    expect(screen.getByRole('alert').textContent).toContain('client id and secret');
    type('Client id', 'cid.apps.googleusercontent.com');
    type('Client secret', 'sekrit');
    next();
    next(); // models skipped

    mocks.completeSetup.mockResolvedValue({ login_method: 'google', signed_in: false });
    fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }));
    await screen.findByText('Setup complete');
    expect(mocks.completeSetup).toHaveBeenCalledWith(expect.objectContaining({
      login_method: 'google',
      admin_emails: ['me@otherco.com'],
      allowed_login_domain: '',
      app_base_url: 'quest.otherco.com',
      admin_password: undefined,
      google_client_id: 'cid.apps.googleusercontent.com',
      google_client_secret: 'sekrit',
      vertex_service_account: null,
    }));
    expect(screen.getByRole('status').textContent).toContain('Sign in with Google');
  });

  it('jumps back to the step that owns a rejected field', async () => {
    await unlock();
    next();
    type('Your email address', 'me@gmail.com');
    type('Email domain allowed to sign in', 'gmail.com');
    next();
    next();
    type('Password', 'correct horse battery');
    type('Repeat the password', 'correct horse battery');
    next();
    next();
    mocks.completeSetup.mockRejectedValue(new ApiClientError(
      'gmail.com is a public mailbox provider', 400, 'invalid_params', undefined,
      { error: 'invalid_params', field: 'allowed_login_domain' },
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }));

    await waitFor(() => expect(screen.queryByText('Administrators and access')).toBeTruthy());
    expect(screen.getByRole('alert').textContent).toContain('public mailbox provider');
    expect(screen.getByLabelText('Email domain allowed to sign in').closest('.setup-field')?.className).toContain('invalid');
  });
});
