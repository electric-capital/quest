/**
 * SetupModelsStep -- the optional models step of first-run setup.
 *
 * Three ways out: connect an API-key provider or self-hosted server (the
 * same quick setup as the composer's ModelSetupCard, through
 * POST /app/api/setup/inference/provider), set up Vertex AI
 * (POST /app/api/setup/inference/vertex -- a pasted service-account key, or
 * just the project id when the server already has Application Default
 * Credentials, which the step says so up front), or skip and let the
 * composer's model setup card offer it again after sign-in. A connection
 * takes effect immediately and is health-checked live, so the outcome shown
 * here (and on the review step) is the real one.
 */
import { useState } from 'react';
import { ExternalLink, LoaderCircle } from 'lucide-react';
import { setupConnectProvider, setupConnectVertex } from '../api/client';
import { ApiClientError } from '../api/request';
import type { SetupInferenceDefaults } from '../api/types';

// Where to create an API key, per kind (shown as a link next to the field).
const KEY_PAGES: Record<string, string> = {
  anthropic: 'https://console.anthropic.com/settings/keys',
  openai: 'https://platform.openai.com/api-keys',
  gemini: 'https://aistudio.google.com/apikey',
  openrouter: 'https://openrouter.ai/keys',
  fireworks: 'https://fireworks.ai/settings/users/api-keys',
  nearai: 'https://cloud.near.ai',
  trustedrouter: 'https://trustedrouter.com',
  uberllm: 'https://uberllm.dev/signup',
};

export type ModelsOutcome =
  | { kind: 'provider'; label: string; added: number; ok: boolean; error: string | null }
  | { kind: 'vertex'; projectId: string; ok: boolean | null; error: string | null };

export function describeModelsOutcome(outcome: ModelsOutcome | null): string {
  if (!outcome) return '';
  if (outcome.kind === 'provider') {
    if (!outcome.ok) return `${outcome.label}: key saved, but the check failed`;
    return outcome.added > 0
      ? `${outcome.label}: ${outcome.added} model${outcome.added === 1 ? '' : 's'} connected`
      : `${outcome.label} connected; pick its models in Settings`;
  }
  if (outcome.ok === false) return `Vertex AI project ${outcome.projectId}: saved, but the check failed`;
  return `Vertex AI project ${outcome.projectId}`;
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiClientError || (err instanceof Error && err.message)) return err.message;
  return fallback;
}

type Mode = 'choose' | 'provider' | 'vertex';

export interface SetupModelsStepProps {
  token: string;
  defaults: SetupInferenceDefaults;
  outcome: ModelsOutcome | null;
  onOutcome: (outcome: ModelsOutcome) => void;
  // "Skip for now": the parent moves on to the review.
  onSkip: () => void;
}

export function SetupModelsStep({ token, defaults, outcome, onOutcome, onSkip }: SetupModelsStepProps) {
  const [mode, setMode] = useState<Mode>('choose');
  const [kind, setKind] = useState(defaults.kinds[0]?.kind ?? '');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiType, setApiType] = useState(defaults.api_types[0]?.id ?? 'openai');
  const [keyText, setKeyText] = useState('');
  const [projectId, setProjectId] = useState(
    defaults.vertex.configured_project_id || defaults.vertex.detected_project_id || '',
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const vertex = defaults.vertex;
  const adcPresent = vertex.credentials_source !== null && !vertex.problem;
  const selected = defaults.kinds.find((k) => k.kind === kind) ?? null;
  const endpoint = selected?.endpoint ?? false;
  const keyPage = selected ? KEY_PAGES[selected.kind] : undefined;

  const choose = (next: Mode) => {
    setMode(next);
    setError(null);
  };

  const connectProvider = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const result = await setupConnectProvider(
        endpoint
          ? { token, kind: selected.kind, base_url: baseUrl.trim(), api_type: apiType, api_key: apiKey.trim() || undefined }
          : { token, kind: selected.kind, api_key: apiKey.trim() },
      );
      // A rejected key surfaces as a failed first-model check or, for kinds
      // whose model list is fetched with the key, as a catalog error.
      const failed = (result.check !== null && !result.check.ok)
        || (result.check === null && result.added.length === 0 && !!result.catalog_error);
      onOutcome({
        kind: 'provider',
        label: result.instance.label,
        added: result.added.length,
        ok: !failed,
        error: result.check && !result.check.ok ? result.check.error : result.catalog_error,
      });
      if (!failed) setApiKey('');
    } catch (err) {
      setError(errorMessage(err, 'Could not connect the provider.'));
    } finally {
      setBusy(false);
    }
  };

  const connectVertex = async () => {
    let key: Record<string, unknown> | null = null;
    if (keyText.trim()) {
      try {
        key = JSON.parse(keyText);
      } catch {
        setError('The service-account key is not valid JSON.');
        return;
      }
    } else if (!adcPresent) {
      setError('Paste a service-account key: no Google credentials were found on the server.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await setupConnectVertex({ token, service_account: key, project_id: projectId.trim() });
      onOutcome({
        kind: 'vertex',
        projectId: result.project_id,
        ok: result.check ? result.check.ok : null,
        error: result.check && !result.check.ok ? result.check.error : null,
      });
    } catch (err) {
      setError(errorMessage(err, 'Could not set up Vertex AI.'));
    } finally {
      setBusy(false);
    }
  };

  const handleKeyFile = async (file: File | undefined) => {
    if (!file) return;
    setKeyText(await file.text());
    setError(null);
  };

  if (mode === 'choose') {
    return (
      <>
        <h1 className="setup-title">Models</h1>
        <p className="setup-text">
          Connect the LLM your users will chat with. You can add more providers or change models
          later in Settings › Inference Providers.
          {defaults.llm_configured && ' Models are already configured on this deployment.'}
        </p>
        {outcome && (
          <p className="setup-notice" role="status">
            {describeModelsOutcome(outcome)}{outcome.error ? `: ${outcome.error}` : '.'}
            {' '}Connect another below or press Next.
          </p>
        )}
        <div className="setup-choices">
          <button type="button" className="setup-choice" onClick={() => choose('provider')}>
            <span>
              <span className="setup-choice-title">Connect a provider with an API key</span>
              <span className="setup-choice-text">
                {defaults.kinds.filter((k) => !k.endpoint).map((k) => k.label).join(', ')} or a
                self-hosted server. Paste a key and Quest adds the provider's models.
              </span>
            </span>
          </button>
          <button type="button" className="setup-choice" onClick={() => choose('vertex')}>
            <span>
              <span className="setup-choice-title">Use Google Vertex AI</span>
              <span className="setup-choice-text">
                {adcPresent
                  ? `Google credentials were found on this server${vertex.service_account_email ? ` (${vertex.service_account_email})` : ''}${vertex.detected_project_id ? `, project ${vertex.detected_project_id}` : ''}: only the project id is needed.`
                  : 'Claude and Gemini through your GCP project: paste a service-account key with the Vertex AI User role.'}
              </span>
            </span>
          </button>
          <button type="button" className="setup-choice" onClick={onSkip}>
            <span>
              <span className="setup-choice-title">Skip for now</span>
              <span className="setup-choice-text">
                The composer offers to connect a provider right after you sign in.
              </span>
            </span>
          </button>
        </div>
      </>
    );
  }

  if (mode === 'provider') {
    const canSubmit = !busy && !!selected && (endpoint ? baseUrl.trim() !== '' : apiKey.trim() !== '');
    return (
      <>
        <h1 className="setup-title">Connect a provider</h1>
        <div className="setup-kinds" role="radiogroup" aria-label="Provider">
          {defaults.kinds.map((k) => (
            <button
              key={k.kind} type="button" role="radio" aria-checked={k.kind === kind}
              className={'setup-kind' + (k.kind === kind ? ' selected' : '')}
              onClick={() => { setKind(k.kind); setError(null); }}
              disabled={busy}
            >
              {k.label}
            </button>
          ))}
        </div>
        {selected && (endpoint ? (
          <>
            <label className="setup-field">
              <span className="setup-label">Server URL</span>
              <input
                type="text" className="setup-input" value={baseUrl} autoComplete="off"
                onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://localhost:11434"
                disabled={busy} aria-label="Server URL"
              />
            </label>
            <label className="setup-field">
              <span className="setup-label">API type</span>
              <select className="setup-input" value={apiType} onChange={(e) => setApiType(e.target.value)} disabled={busy} aria-label="API type">
                {defaults.api_types.map((t) => (
                  <option key={t.id} value={t.id}>{t.label} — {t.description}</option>
                ))}
              </select>
            </label>
            <label className="setup-field">
              <span className="setup-label">API key <span className="setup-muted">(optional)</span></span>
              <input
                type="password" className="setup-input" value={apiKey} autoComplete="off"
                onChange={(e) => setApiKey(e.target.value)} placeholder={selected.hint}
                disabled={busy} aria-label="API key"
              />
            </label>
          </>
        ) : (
          <label className="setup-field">
            <span className="setup-label">
              {selected.label} API key
              {keyPage && (
                <>
                  {' '}
                  <a className="setup-link" href={keyPage} target="_blank" rel="noreferrer">
                    Get a key <ExternalLink size={12} aria-hidden="true" />
                  </a>
                </>
              )}
            </span>
            <input
              type="password" className="setup-input" value={apiKey} autoComplete="off" spellCheck={false}
              onChange={(e) => setApiKey(e.target.value)} placeholder={selected.hint}
              disabled={busy} aria-label={`${selected.label} API key`}
            />
          </label>
        ))}
        {error && <p className="setup-error" role="alert">{error}</p>}
        {outcome?.kind === 'provider' && !error && (
          <p className={outcome.ok ? 'setup-notice' : 'setup-error'} role={outcome.ok ? 'status' : 'alert'}>
            {describeModelsOutcome(outcome)}{outcome.error ? `: ${outcome.error}` : '.'}
            {outcome.ok ? ' Press Next to continue.' : ' Check the key and try again.'}
          </p>
        )}
        <div className="setup-actions">
          <button type="button" className="setup-button secondary" onClick={() => choose('choose')} disabled={busy}>
            Other options
          </button>
          <button type="button" className="setup-button" onClick={() => void connectProvider()} disabled={!canSubmit}>
            {busy ? <><LoaderCircle size={14} className="setup-spin" /> Connecting…</> : 'Connect'}
          </button>
        </div>
        <p className="setup-footnote">
          Keys are stored encrypted on the server and never shown again. Models connected here
          are allowed in private conversations.
        </p>
      </>
    );
  }

  return (
    <>
      <h1 className="setup-title">Google Vertex AI</h1>
      {adcPresent ? (
        <p className="setup-notice" role="status">
          Google credentials were found on this server
          {vertex.credentials_source === 'env' ? ' (GOOGLE_APPLICATION_CREDENTIALS)' : ' (gcloud application-default credentials)'}
          {vertex.service_account_email ? `: ${vertex.service_account_email}` : ''}. Paste a key only
          to use a different service account.
        </p>
      ) : (
        <p className="setup-text">
          Create a service account with the <strong>Vertex AI User</strong> role in your GCP project,
          download a JSON key and paste it here.
          {vertex.problem && ` (${vertex.problem})`}
        </p>
      )}
      <label className="setup-field">
        <span className="setup-label">
          Service-account key JSON{adcPresent && <span className="setup-muted"> (optional)</span>}
        </span>
        <textarea
          className="setup-input" spellCheck={false} value={keyText}
          onChange={(e) => { setKeyText(e.target.value); setError(null); }}
          placeholder='{ "type": "service_account", "project_id": "…", … }'
          disabled={busy} aria-label="Service-account key JSON"
        />
        <input
          type="file" accept="application/json,.json"
          onChange={(e) => void handleKeyFile(e.target.files?.[0])}
          disabled={busy} aria-label="Service-account key file"
        />
      </label>
      <label className="setup-field">
        <span className="setup-label">GCP project id for Vertex AI</span>
        <input
          type="text" className="setup-input" value={projectId} autoComplete="off" spellCheck={false}
          onChange={(e) => setProjectId(e.target.value)} disabled={busy}
          aria-label="GCP project id for Vertex AI"
        />
        <span className="setup-hint">Defaults to the key's project.</span>
      </label>
      {error && <p className="setup-error" role="alert">{error}</p>}
      {outcome?.kind === 'vertex' && !error && (
        <p className={outcome.ok === false ? 'setup-error' : 'setup-notice'} role={outcome.ok === false ? 'alert' : 'status'}>
          {describeModelsOutcome(outcome)}{outcome.error ? `: ${outcome.error}` : '.'}
          {outcome.ok === false ? ' Check the project and the key.' : ' Press Next to continue.'}
        </p>
      )}
      <div className="setup-actions">
        <button type="button" className="setup-button secondary" onClick={() => choose('choose')} disabled={busy}>
          Other options
        </button>
        <button
          type="button" className="setup-button" onClick={() => void connectVertex()}
          disabled={busy || (!keyText.trim() && !projectId.trim())}
        >
          {busy ? <><LoaderCircle size={14} className="setup-spin" /> Checking…</> : 'Use Vertex AI'}
        </button>
      </div>
    </>
  );
}
