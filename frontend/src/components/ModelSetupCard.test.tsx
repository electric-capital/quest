// ModelSetupCard: the admin's first-run "connect a model" card. Connect posts
// the quick-setup request for the picked kind; a passing check hands off to
// onConnected, a failed check keeps the form with the error, and a kind with
// nothing to suggest points at Settings > Inference Providers.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ModelSetupCard } from './ModelSetupCard';

const mocks = vi.hoisted(() => ({
  fetchInferenceProviders: vi.fn(),
  quickSetupInferenceProvider: vi.fn(),
  setSettingsOpen: vi.fn(),
  setSettingsInitialSection: vi.fn(),
}));

vi.mock('../api/client', () => ({
  fetchInferenceProviders: mocks.fetchInferenceProviders,
  quickSetupInferenceProvider: mocks.quickSetupInferenceProvider,
}));

vi.mock('../contexts/NavigationContext', () => ({
  useNavigationState: () => ({
    setSettingsOpen: mocks.setSettingsOpen,
    setSettingsInitialSection: mocks.setSettingsInitialSection,
  }),
}));

const KINDS = [
  { kind: 'anthropic', label: 'Anthropic', endpoint: false, catalog: 'anthropic', hint: 'sk-ant-...', key_required: true },
  { kind: 'openrouter', label: 'OpenRouter', endpoint: false, catalog: 'openrouter', hint: 'sk-or-v1-...', key_required: true },
  { kind: 'local', label: 'Self-hosted', endpoint: true, catalog: 'server', hint: 'optional', key_required: false },
];
const API_TYPES = [
  { id: 'openai', label: 'OpenAI-compatible', description: 'llama.cpp, vLLM…' },
  { id: 'ollama', label: 'Ollama', description: "Ollama's native API" },
];

function instance(kind: string, configured = true) {
  return {
    id: kind, kind, kind_label: kind, label: kind, configured, source: 'store', credentials: { api_key_set: true },
    hint: '', key_required: true, base_url: null, api_type: null, models: [],
  };
}

describe('ModelSetupCard', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  function setup() {
    mocks.fetchInferenceProviders.mockResolvedValue({ kinds: KINDS, api_types: API_TYPES, instances: [], vertex: {} });
    const onConnected = vi.fn().mockResolvedValue(undefined);
    render(<ModelSetupCard onConnected={onConnected} />);
    return { onConnected };
  }

  it('connects the picked provider and hands off once the first model checks out', async () => {
    const { onConnected } = setup();
    mocks.quickSetupInferenceProvider.mockResolvedValue({
      instance: instance('anthropic'),
      added: ['anthropic:claude-sonnet-5', 'anthropic:claude-haiku-5-5'],
      check: { model: 'anthropic:claude-sonnet-5', ok: true, error: null, checked_at: 'now' },
      catalog_error: null,
    });
    const input = await screen.findByLabelText('Anthropic API key');
    expect((screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: '  sk-ant-secret ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(onConnected).toHaveBeenCalledTimes(1));
    expect(mocks.quickSetupInferenceProvider).toHaveBeenCalledWith({ kind: 'anthropic', api_key: 'sk-ant-secret' });
    expect(screen.getByRole('status').textContent).toContain('Added 2 models');
  });

  it('keeps the form with the provider error when the first check fails', async () => {
    const { onConnected } = setup();
    mocks.quickSetupInferenceProvider.mockResolvedValue({
      instance: instance('anthropic'),
      added: [],
      check: { model: 'anthropic:claude-sonnet-5', ok: false, error: 'invalid x-api-key', checked_at: 'now' },
      catalog_error: null,
    });
    fireEvent.change(await screen.findByLabelText('Anthropic API key'), { target: { value: 'sk-ant-bad' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('invalid x-api-key');
    expect(onConnected).not.toHaveBeenCalled();
    // The key stays in the field for a correction
    expect((screen.getByLabelText('Anthropic API key') as HTMLInputElement).value).toBe('sk-ant-bad');
  });

  it('sends the admin to Settings when the kind has nothing to suggest', async () => {
    const { onConnected } = setup();
    mocks.quickSetupInferenceProvider.mockResolvedValue({
      instance: instance('openrouter'),
      added: [],
      check: null,
      catalog_error: null,
    });
    await screen.findByLabelText('Anthropic API key');
    fireEvent.click(screen.getByRole('radio', { name: 'OpenRouter' }));
    fireEvent.change(screen.getByLabelText('OpenRouter API key'), { target: { value: 'sk-or-x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('no default models');
    fireEvent.click(screen.getByRole('button', { name: 'Open Inference Providers' }));
    expect(mocks.setSettingsInitialSection).toHaveBeenCalledWith('inference-providers');
    expect(mocks.setSettingsOpen).toHaveBeenCalledWith(true);
    expect(onConnected).not.toHaveBeenCalled();
  });

  it('treats a catalog error with nothing added as a rejected key', async () => {
    const { onConnected } = setup();
    mocks.quickSetupInferenceProvider.mockResolvedValue({
      instance: instance('anthropic'), added: [], check: null, catalog_error: 'HTTP 401: invalid x-api-key',
    });
    fireEvent.change(await screen.findByLabelText('Anthropic API key'), { target: { value: 'sk-ant-bad' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect((await screen.findByRole('alert')).textContent).toContain('did not accept it: HTTP 401');
    expect(screen.queryByRole('status')).toBeNull();
    expect(onConnected).not.toHaveBeenCalled();
  });

  it('asks for a server URL and API type for the self-hosted kind', async () => {
    setup();
    mocks.quickSetupInferenceProvider.mockResolvedValue({
      instance: instance('local'), added: ['local:qwen2.5:7b'],
      check: { model: 'local:qwen2.5:7b', ok: true, error: null, checked_at: 'now' }, catalog_error: null,
    });
    await screen.findByLabelText('Anthropic API key');
    fireEvent.click(screen.getByRole('radio', { name: 'Self-hosted' }));
    fireEvent.change(screen.getByPlaceholderText('http://localhost:11434'), { target: { value: 'box:8080' } });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'ollama' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(mocks.quickSetupInferenceProvider).toHaveBeenCalledWith({
      kind: 'local', base_url: 'box:8080', api_type: 'ollama', api_key: undefined,
    }));
  });

  it('shows a request failure', async () => {
    setup();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    mocks.quickSetupInferenceProvider.mockRejectedValue({ detail: { message: 'An API key is required for Anthropic.' } });
    fireEvent.change(await screen.findByLabelText('Anthropic API key'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect((await screen.findByRole('alert')).textContent).toContain('An API key is required');
  });
});
