/**
 * ModelSetupCard -- the admin's first-run "connect a model" card.
 *
 * Rendered by <Composer> in place of the composer while GET /app/api/config
 * reports no available model AND the signed-in user is an admin (everyone
 * else sees the composer with its "no model yet" notice). It is the quickest
 * path from a fresh deployment to a first conversation: pick a provider kind
 * (the same kinds as Settings > Inference Providers, fetched from
 * GET /admin/inference-providers), paste its API key -- or, for a
 * self-hosted server, its URL and API type -- and Connect.
 *
 * Connect calls POST /admin/inference-providers/quick-setup, which stores
 * the credential, adds the models Quest knows for the kind and health-checks
 * the first one live. On a passing check the card calls ``onConnected``
 * (the Composer re-fetches the app config, available models appear and the
 * card unmounts in favour of the real composer). A failed check or a
 * rejected key keeps the form up with the provider's error so the admin can
 * correct the key; a kind Quest has no suggestions for leaves the key saved
 * and sends the admin to Settings > Inference Providers to pick models.
 */
import { useCallback, useEffect, useState } from 'react';
import { ExternalLink, KeyRound, LoaderCircle } from 'lucide-react';
import { fetchInferenceProviders, quickSetupInferenceProvider } from '../api/client';
import type {
  InferenceApiType,
  InferenceInstanceKind,
  InferenceQuickSetupResult,
} from '../api/types';
import { useNavigationState } from '../contexts/NavigationContext';
import './ModelSetupCard.css';

// Where to create an API key, per kind (shown as a link next to the field).
const KEY_PAGES: Record<string, string> = {
  anthropic: 'https://console.anthropic.com/settings/keys',
  openai: 'https://platform.openai.com/api-keys',
  gemini: 'https://aistudio.google.com/apikey',
  openrouter: 'https://openrouter.ai/keys',
  fireworks: 'https://fireworks.ai/settings/users/api-keys',
  nearai: 'https://cloud.near.ai',
  trustedrouter: 'https://trustedrouter.com',
};

function errorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === 'object') {
    const detail = (error as { detail?: unknown }).detail;
    if (detail && typeof detail === 'object' && typeof (detail as { message?: unknown }).message === 'string') {
      return (detail as { message: string }).message;
    }
    if (typeof (error as { message?: unknown }).message === 'string') {
      return (error as { message: string }).message;
    }
  }
  return fallback;
}

export interface ModelSetupCardProps {
  // Called after a provider was connected and its first model passed its
  // health check; the parent re-fetches the app config.
  onConnected: () => void | Promise<void>;
}

export function ModelSetupCard({ onConnected }: ModelSetupCardProps) {
  const { setSettingsOpen, setSettingsInitialSection } = useNavigationState();
  const [kinds, setKinds] = useState<InferenceInstanceKind[] | null>(null);
  const [apiTypes, setApiTypes] = useState<InferenceApiType[]>([]);
  const [loadError, setLoadError] = useState('');
  const [kind, setKind] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiType, setApiType] = useState('openai');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [outcome, setOutcome] = useState<InferenceQuickSetupResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await fetchInferenceProviders();
        if (cancelled) return;
        setKinds(data.kinds);
        setApiTypes(data.api_types);
        setApiType(data.api_types[0]?.id ?? 'openai');
        setKind((current) => current || data.kinds[0]?.kind || '');
      } catch (err) {
        if (cancelled) return;
        console.error('Failed to load inference provider kinds:', err);
        setLoadError(errorMessage(err, 'Failed to load the provider list'));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const selected = kinds?.find((k) => k.kind === kind) ?? null;
  const endpoint = selected?.endpoint ?? false;
  const canSubmit = !busy && !!selected && (endpoint ? baseUrl.trim() !== '' : apiKey.trim() !== '');

  const openSettings = useCallback(() => {
    setSettingsInitialSection('inference-providers');
    setSettingsOpen(true);
  }, [setSettingsInitialSection, setSettingsOpen]);

  const handleKindChange = useCallback((next: string) => {
    setKind(next);
    setError('');
    setOutcome(null);
  }, []);

  const handleSubmit = useCallback(async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit || !selected) return;
    setBusy(true);
    setError('');
    setOutcome(null);
    try {
      const result = await quickSetupInferenceProvider(
        endpoint
          ? { kind: selected.kind, base_url: baseUrl.trim(), api_type: apiType, api_key: apiKey.trim() || undefined }
          : { kind: selected.kind, api_key: apiKey.trim() },
      );
      setOutcome(result);
      if (result.check?.ok) {
        setApiKey('');
        await onConnected();
      }
    } catch (err) {
      console.error('Quick setup failed:', err);
      setError(errorMessage(err, 'Could not connect the provider'));
    } finally {
      setBusy(false);
    }
  }, [apiKey, apiType, baseUrl, canSubmit, endpoint, onConnected, selected]);

  const keyPage = selected ? KEY_PAGES[selected.kind] : undefined;
  // A rejected key surfaces either as a failed first-model check or, for
  // the kinds whose model list is fetched with the key, as a catalog error
  // with nothing added; a connected kind Quest has no suggestions for has
  // neither.
  const checkFailed = !!outcome?.check && !outcome.check.ok;
  const catalogFailed = !!outcome && !outcome.check && outcome.added.length === 0 && !!outcome.catalog_error;
  const nothingSuggested = !!outcome && !outcome.check && !outcome.catalog_error;

  return (
    <div className="model-setup-card" role="region" aria-label="Connect a model">
      <div className="model-setup-head">
        <span className="model-setup-icon" aria-hidden="true"><KeyRound size={18} /></span>
        <div>
          <h2 className="model-setup-title">Connect a model to start chatting</h2>
          <p className="model-setup-text">
            Quest has no LLM configured yet. Paste an API key from any of these providers
            and Quest adds its models for everyone on this deployment. You can add more
            providers or change models later in Settings › Inference Providers.
          </p>
        </div>
      </div>

      {loadError ? (
        <div className="model-setup-error" role="alert">{loadError}</div>
      ) : kinds === null ? (
        <div className="model-setup-loading"><LoaderCircle size={16} className="model-setup-spin" /> Loading providers…</div>
      ) : (
        <form className="model-setup-form" onSubmit={handleSubmit}>
          <div className="model-setup-kinds" role="radiogroup" aria-label="Provider">
            {kinds.map((k) => (
              <button
                key={k.kind}
                type="button"
                role="radio"
                aria-checked={k.kind === kind}
                className={'model-setup-kind' + (k.kind === kind ? ' selected' : '')}
                onClick={() => handleKindChange(k.kind)}
                disabled={busy}
              >
                {k.label}
              </button>
            ))}
          </div>

          {selected && (
            <div className="model-setup-fields">
              {endpoint ? (
                <>
                  <label className="model-setup-field">
                    <span className="model-setup-label">Server URL</span>
                    <input
                      type="text"
                      className="model-setup-input"
                      value={baseUrl}
                      onChange={(e) => setBaseUrl(e.target.value)}
                      placeholder="http://localhost:11434"
                      autoComplete="off"
                      disabled={busy}
                    />
                  </label>
                  <label className="model-setup-field">
                    <span className="model-setup-label">API type</span>
                    <select
                      className="model-setup-input"
                      value={apiType}
                      onChange={(e) => setApiType(e.target.value)}
                      disabled={busy}
                    >
                      {apiTypes.map((t) => (
                        <option key={t.id} value={t.id}>{t.label} — {t.description}</option>
                      ))}
                    </select>
                  </label>
                  <label className="model-setup-field">
                    <span className="model-setup-label">API key <span className="model-setup-muted">(optional)</span></span>
                    <input
                      type="password"
                      className="model-setup-input"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder={selected.hint}
                      autoComplete="off"
                      disabled={busy}
                    />
                  </label>
                </>
              ) : (
                <label className="model-setup-field">
                  <span className="model-setup-label">
                    {selected.label} API key
                    {keyPage && (
                      <a className="model-setup-link" href={keyPage} target="_blank" rel="noreferrer">
                        Get a key <ExternalLink size={12} aria-hidden="true" />
                      </a>
                    )}
                  </span>
                  <input
                    type="password"
                    className="model-setup-input"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={selected.hint}
                    autoComplete="off"
                    spellCheck={false}
                    disabled={busy}
                    aria-label={`${selected.label} API key`}
                  />
                </label>
              )}
              <button type="submit" className="model-setup-submit" disabled={!canSubmit}>
                {busy ? (<><LoaderCircle size={14} className="model-setup-spin" /> Connecting…</>) : 'Connect'}
              </button>
            </div>
          )}

          {error && <div className="model-setup-error" role="alert">{error}</div>}
          {checkFailed && outcome?.check && (
            <div className="model-setup-error" role="alert">
              The key was saved but the first model check failed: {outcome.check.error}
              {' '}Check the key and try again.
            </div>
          )}
          {catalogFailed && outcome && (
            <div className="model-setup-error" role="alert">
              The key was saved but {outcome.instance.label} did not accept it: {outcome.catalog_error}
              {' '}Check the key and try again.
            </div>
          )}
          {nothingSuggested && outcome && (
            <div className="model-setup-notice" role="status">
              {outcome.instance.label} is connected, but Quest has no default models for this
              provider. Pick its models in Settings.
              <button type="button" className="model-setup-settings" onClick={openSettings}>
                Open Inference Providers
              </button>
            </div>
          )}
          {outcome?.check?.ok && (
            <div className="model-setup-notice" role="status">
              Connected. Added {outcome.added.length} model{outcome.added.length === 1 ? '' : 's'} — the composer is ready.
            </div>
          )}

          <p className="model-setup-footnote">
            Keys are stored encrypted on the server and never shown again. Models connected here are
            allowed in private conversations. Using Vertex AI instead? Set the project id in
            server_config.json — see Settings › Inference Providers.
          </p>
        </form>
      )}
    </div>
  );
}
