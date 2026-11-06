// SetupModelsStep: the optional models step of first-run setup. Three
// choices: an API-key provider (connected live through the quick setup),
// Vertex AI (key, or project only when the server already has ADC), or skip.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { SetupModelsStep, describeModelsOutcome } from './SetupModelsStep';
import { ApiClientError } from '../api/request';
import type { SetupInferenceDefaults } from '../api/types';

const mocks = vi.hoisted(() => ({
  setupConnectProvider: vi.fn(),
  setupConnectVertex: vi.fn(),
}));

vi.mock('../api/client', () => ({
  setupConnectProvider: mocks.setupConnectProvider,
  setupConnectVertex: mocks.setupConnectVertex,
}));

const DEFAULTS: SetupInferenceDefaults = {
  llm_configured: false,
  kinds: [
    { kind: 'anthropic', label: 'Anthropic', endpoint: false, catalog: 'anthropic', hint: 'sk-ant-...', key_required: true },
    { kind: 'local', label: 'Self-hosted', endpoint: true, catalog: 'server', hint: 'optional', key_required: false },
  ],
  api_types: [
    { id: 'openai', label: 'OpenAI-compatible', description: 'llama.cpp, vLLM…' },
    { id: 'ollama', label: 'Ollama', description: "Ollama's native API" },
  ],
  vertex: {
    credentials_source: null, service_account_email: null, detected_project_id: null,
    problem: null, configured_project_id: '',
  },
};

function setup(defaults: SetupInferenceDefaults = DEFAULTS) {
  const onOutcome = vi.fn();
  const onSkip = vi.fn();
  render(<SetupModelsStep token="tok" defaults={defaults} outcome={null} onOutcome={onOutcome} onSkip={onSkip} />);
  return { onOutcome, onSkip };
}

describe('SetupModelsStep', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('offers the three ways out and skip hands off to the parent', () => {
    const { onSkip } = setup();
    expect(screen.getByRole('button', { name: /Connect a provider with an API key/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Use Google Vertex AI/ }).textContent).toContain('paste a service-account key');
    fireEvent.click(screen.getByRole('button', { name: /Skip for now/ }));
    expect(onSkip).toHaveBeenCalled();
  });

  it('connects an API-key provider and reports the live outcome', async () => {
    const { onOutcome } = setup();
    fireEvent.click(screen.getByRole('button', { name: /Connect a provider with an API key/ }));
    const connect = screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement;
    expect(connect.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'sk-ant-x' } });
    mocks.setupConnectProvider.mockResolvedValue({
      instance: { id: 'anthropic', kind: 'anthropic', label: 'Anthropic' },
      added: ['anthropic:claude-sonnet-5'],
      check: { model: 'anthropic:claude-sonnet-5', ok: true, error: null, checked_at: 'now' },
      catalog_error: null,
    });
    fireEvent.click(connect);
    await vi.waitFor(() => expect(onOutcome).toHaveBeenCalledWith({
      kind: 'provider', label: 'Anthropic', added: 1, ok: true, error: null,
    }));
    expect(mocks.setupConnectProvider).toHaveBeenCalledWith({ token: 'tok', kind: 'anthropic', api_key: 'sk-ant-x' });
  });

  it('treats a failed first-model check as a rejected key', async () => {
    const { onOutcome } = setup();
    fireEvent.click(screen.getByRole('button', { name: /Connect a provider with an API key/ }));
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'bad' } });
    mocks.setupConnectProvider.mockResolvedValue({
      instance: { id: 'anthropic', kind: 'anthropic', label: 'Anthropic' },
      added: [],
      check: { model: 'anthropic:claude-sonnet-5', ok: false, error: '401 invalid x-api-key', checked_at: 'now' },
      catalog_error: null,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await vi.waitFor(() => expect(onOutcome).toHaveBeenCalledWith({
      kind: 'provider', label: 'Anthropic', added: 0, ok: false, error: '401 invalid x-api-key',
    }));
  });

  it('sends a self-hosted server URL and API type', async () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: /Connect a provider with an API key/ }));
    fireEvent.click(screen.getByRole('radio', { name: 'Self-hosted' }));
    fireEvent.change(screen.getByLabelText('Server URL'), { target: { value: 'http://box:11434' } });
    fireEvent.change(screen.getByLabelText('API type'), { target: { value: 'ollama' } });
    mocks.setupConnectProvider.mockResolvedValue({
      instance: { id: 'local', kind: 'local', label: 'Self-hosted' }, added: [], check: null, catalog_error: null,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await vi.waitFor(() => expect(mocks.setupConnectProvider).toHaveBeenCalledWith({
      token: 'tok', kind: 'local', base_url: 'http://box:11434', api_type: 'ollama', api_key: undefined,
    }));
  });

  it('shows the server error from a rejected provider request', async () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: /Connect a provider with an API key/ }));
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'k' } });
    mocks.setupConnectProvider.mockRejectedValue(new ApiClientError('Unlock setup first.', 401, 'setup_locked'));
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Unlock setup first.');
  });

  it('says so when Vertex credentials are already on the server and needs only the project', async () => {
    const { onOutcome } = setup({
      ...DEFAULTS,
      vertex: {
        credentials_source: 'env', service_account_email: 'quest@proj.iam', detected_project_id: 'proj',
        problem: null, configured_project_id: '',
      },
    });
    expect(screen.getByRole('button', { name: /Use Google Vertex AI/ }).textContent)
      .toContain('Google credentials were found on this server (quest@proj.iam), project proj');
    fireEvent.click(screen.getByRole('button', { name: /Use Google Vertex AI/ }));
    expect(screen.getByRole('status').textContent).toContain('GOOGLE_APPLICATION_CREDENTIALS');
    expect((screen.getByLabelText('GCP project id for Vertex AI') as HTMLInputElement).value).toBe('proj');
    mocks.setupConnectVertex.mockResolvedValue({
      project_id: 'proj', credentials_source: 'env',
      check: { model: 'claude-sonnet-5', ok: true, error: null, checked_at: 'now' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Use Vertex AI' }));
    await vi.waitFor(() => expect(onOutcome).toHaveBeenCalledWith({ kind: 'vertex', projectId: 'proj', ok: true, error: null }));
    expect(mocks.setupConnectVertex).toHaveBeenCalledWith({ token: 'tok', service_account: null, project_id: 'proj' });
  });

  it('requires a key when the server has no Google credentials, and parses it', async () => {
    const { onOutcome } = setup();
    fireEvent.click(screen.getByRole('button', { name: /Use Google Vertex AI/ }));
    fireEvent.change(screen.getByLabelText('GCP project id for Vertex AI'), { target: { value: 'p' } });
    fireEvent.click(screen.getByRole('button', { name: 'Use Vertex AI' }));
    expect(screen.getByRole('alert').textContent).toContain('Paste a service-account key');
    fireEvent.change(screen.getByLabelText('Service-account key JSON'), { target: { value: '{not json' } });
    fireEvent.click(screen.getByRole('button', { name: 'Use Vertex AI' }));
    expect(screen.getByRole('alert').textContent).toContain('not valid JSON');
    fireEvent.change(screen.getByLabelText('Service-account key JSON'), {
      target: { value: '{"type": "service_account", "project_id": "p"}' },
    });
    mocks.setupConnectVertex.mockResolvedValue({ project_id: 'p', credentials_source: 'env', check: null });
    fireEvent.click(screen.getByRole('button', { name: 'Use Vertex AI' }));
    await vi.waitFor(() => expect(onOutcome).toHaveBeenCalledWith({ kind: 'vertex', projectId: 'p', ok: null, error: null }));
    expect(mocks.setupConnectVertex).toHaveBeenCalledWith({
      token: 'tok', service_account: { type: 'service_account', project_id: 'p' }, project_id: 'p',
    });
  });

  it('describes outcomes for the review step', () => {
    expect(describeModelsOutcome(null)).toBe('');
    expect(describeModelsOutcome({ kind: 'provider', label: 'OpenRouter', added: 3, ok: true, error: null })).toBe('OpenRouter: 3 models connected');
    expect(describeModelsOutcome({ kind: 'provider', label: 'Fireworks', added: 0, ok: true, error: null })).toContain('pick its models');
    expect(describeModelsOutcome({ kind: 'vertex', projectId: 'p', ok: false, error: 'x' })).toContain('check failed');
  });
});
