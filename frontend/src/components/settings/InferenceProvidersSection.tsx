import { useState, useEffect, useCallback, useRef } from 'react';
import { Loader2, MoreVertical, Plus, X } from 'lucide-react';
import {
  createInferenceInstance,
  deleteInferenceInstance,
  fetchInferenceProviders,
  searchInstanceCatalog,
  searchOpenRouterCatalog,
  testInferenceModel,
  updateInferenceInstance,
  updateVertexModels,
} from '../../api/client';
import type {
  InferenceApiType,
  InferenceInstanceStatus,
  InferenceModelInfo,
  InferenceProvidersListResponse,
  OpenRouterCatalogModel,
  VertexProviderStatus,
  VertexSectionInfo,
} from '../../api/types';
import {
  CredentialCard,
  CredentialField,
  SaveActions,
  secretPlaceholder,
} from './ServiceCredentialsSection';
import type { SaveStatus } from './ServiceCredentialsSection';
import './ServiceCredentialsSection.css';
import './InferenceProvidersSection.css';

const CREDENTIALS_SOURCE_LABELS: Record<string, string> = {
  env: 'Service-account key file (GOOGLE_APPLICATION_CREDENTIALS)',
  gcloud_adc: 'gcloud Application Default Credentials',
};

const PROJECT_SOURCE_LABELS: Record<string, string> = {
  env: 'environment variable',
  server_config: 'server_config.json',
  anthropic_fallback: 'inherited from the Claude on Vertex project',
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function InfoRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="inf-prov-info-row">
      <span className="inf-prov-info-label">{label}</span>
      <span className={`inf-prov-info-value${mono ? ' mono' : ''}`}>{value}</span>
    </div>
  );
}

function VertexSectionRows({ title, section }: { title: string; section: VertexSectionInfo }) {
  return (
    <div className="inf-prov-subsection">
      <h5 className="inf-prov-subsection-title">{title}</h5>
      {section.configured ? (
        <>
          <InfoRow
            label="Project"
            value={
              section.project_source
                ? `${section.vertex_project_id} (${PROJECT_SOURCE_LABELS[section.project_source]})`
                : section.vertex_project_id
            }
            mono
          />
          <InfoRow label="Region" value={section.vertex_region} mono />
        </>
      ) : (
        <p className="inf-prov-unconfigured-note">
          No Vertex project configured — these models are hidden from the
          model picker.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-model health state (server-stored, recheckable)
// ---------------------------------------------------------------------------

type ModelCheckState =
  | { status: 'idle' }
  | { status: 'checking' }
  | { status: 'ok'; checkedAt: string }
  | { status: 'error'; error: string; checkedAt: string };

const IDLE_CHECK: ModelCheckState = { status: 'idle' };

/** The stored server verdict for a model, as row state. */
function storedState(model: InferenceModelInfo): ModelCheckState {
  if (!model.status) return IDLE_CHECK;
  return model.status.ok
    ? { status: 'ok', checkedAt: model.status.checked_at }
    : {
        status: 'error',
        error: model.status.error ?? 'Check failed',
        checkedAt: model.status.checked_at,
      };
}

/**
 * Row state for a set of models: the server-stored verdict (from the startup
 * sweep) until a recheck is triggered here, then the live/local result --
 * rechecks also update the server-global store, so what this shows is what
 * the rest of the app will eventually see.
 */
function useModelChecks() {
  const [overrides, setOverrides] = useState<Record<string, ModelCheckState>>({});

  const runCheck = useCallback(async (modelId: string) => {
    setOverrides((prev) => ({ ...prev, [modelId]: { status: 'checking' } }));
    let next: ModelCheckState;
    try {
      const result = await testInferenceModel(modelId);
      next = result.ok
        ? { status: 'ok', checkedAt: result.checked_at }
        : {
            status: 'error',
            error: result.error ?? 'Check failed',
            checkedAt: result.checked_at,
          };
    } catch (error) {
      console.error(`Model health check failed for ${modelId}:`, error);
      next = {
        status: 'error',
        error: errorMessage(error, 'Check failed'),
        checkedAt: new Date().toISOString(),
      };
    }
    setOverrides((prev) => ({ ...prev, [modelId]: next }));
  }, []);

  const runAll = useCallback(
    (models: InferenceModelInfo[]) => {
      models.forEach((model) => runCheck(model.id));
    },
    [runCheck],
  );

  const stateFor = useCallback(
    (model: InferenceModelInfo): ModelCheckState =>
      overrides[model.id] ?? storedState(model),
    [overrides],
  );

  const anyChecking = Object.values(overrides).some((s) => s.status === 'checking');
  return { stateFor, runCheck, runAll, anyChecking };
}

/** Liveness indicator: grey dot (never checked) / spinner / green / red. */
function LivenessDot({ state }: { state: ModelCheckState }) {
  if (state.status === 'checking') {
    return <Loader2 size={12} className="inf-prov-model-spinner" aria-label="Checking" />;
  }
  const title =
    state.status === 'idle'
      ? 'Not checked yet'
      : `${state.status === 'ok' ? 'Responding' : 'Failing'} — last checked ${new Date(state.checkedAt).toLocaleString()}`;
  return (
    <span
      className={`inf-prov-dot ${state.status}`}
      title={title}
      role="img"
      aria-label={state.status === 'idle' ? 'Not checked' : state.status === 'ok' ? 'Responding' : 'Failing'}
    />
  );
}

/** A per-model override the admin typed on a self-hosted model row. */
export interface ModelEdit {
  name?: string;
  context_length?: number;
}

/**
 * Inline editor under a self-hosted model row: the friendly name (local
 * servers publish none, so the default is derived from the id) and the
 * context window in tokens (what the app assumes for the model; for Ollama
 * also the ``num_ctx`` requested on every call). Each field saves on blur
 * or Enter when its value changed.
 */
function ModelEditFields({
  model,
  busy,
  onEdit,
}: {
  model: InferenceModelInfo;
  busy: boolean;
  onEdit: (edit: ModelEdit) => void;
}) {
  // Initial values only: ModelRow keys this editor by the stored values, so
  // a save that changes them remounts it with fresh state.
  const [name, setName] = useState(model.display_name);
  const [context, setContext] = useState(String(model.max_input_tokens));

  const commitName = () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setName(model.display_name);
      return;
    }
    if (trimmed !== model.display_name) onEdit({ name: trimmed });
  };
  const commitContext = () => {
    const parsed = Number.parseInt(context, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      setContext(String(model.max_input_tokens));
      return;
    }
    if (parsed !== model.max_input_tokens) onEdit({ context_length: parsed });
  };
  const blurOnEnter = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') (event.target as HTMLInputElement).blur();
  };

  return (
    <div className="inf-prov-model-edit">
      <label className="inf-prov-model-edit-field">
        <span>Name</span>
        <input
          type="text"
          className="inf-prov-model-edit-input"
          value={name}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
          onBlur={commitName}
          onKeyDown={blurOnEnter}
          aria-label={`Display name for ${model.wire_id}`}
        />
      </label>
      <label className="inf-prov-model-edit-field">
        <span>Context</span>
        <input
          type="number"
          min={1}
          step={1024}
          className="inf-prov-model-edit-input inf-prov-model-edit-number"
          value={context}
          disabled={busy}
          onChange={(e) => setContext(e.target.value)}
          onBlur={commitContext}
          onKeyDown={blurOnEnter}
          aria-label={`Context window in tokens for ${model.wire_id}`}
        />
        <span>tokens</span>
      </label>
    </div>
  );
}

/**
 * One model row: enabled checkbox, the wire id (what the API call sends) as
 * the primary label with the friendly name muted beside it, the liveness
 * dot + Recheck (only while the model is enabled and its provider is
 * credentialed -- disabled models are never checked), an optional remove
 * button for instance models, and -- for self-hosted models -- the inline
 * name / context editor.
 */
function ModelRow({
  model,
  state,
  checkable,
  busy,
  onCheck,
  onToggle,
  onRemove,
  onEdit,
}: {
  model: InferenceModelInfo;
  state: ModelCheckState;
  checkable: boolean;
  busy: boolean;
  onCheck: () => void;
  onToggle: (enabled: boolean) => void;
  onRemove?: () => void;
  onEdit?: (edit: ModelEdit) => void;
}) {
  const showLiveness = model.enabled && checkable;
  const showName = model.display_name && model.display_name !== model.wire_id;
  return (
    <div className={`inf-prov-model-row${model.enabled ? '' : ' disabled'}`}>
      <div className="inf-prov-model-line">
        <input
          type="checkbox"
          className="inf-prov-model-checkbox"
          checked={model.enabled}
          disabled={busy}
          onChange={(e) => onToggle(e.target.checked)}
          aria-label={`Enable ${model.wire_id}`}
          title={model.enabled ? 'Enabled — offered in the model picker' : 'Disabled — hidden from the model picker, not health-checked'}
        />
        <span className="inf-prov-model-wire" title={model.id}>
          {model.wire_id}
        </span>
        {/* Always rendered (possibly empty) so the grid columns stay put */}
        <span className="inf-prov-model-display" title={showName ? model.display_name : undefined}>
          {showName ? model.display_name : ''}
        </span>
        <span className="inf-prov-model-controls">
          {showLiveness ? (
            <>
              <LivenessDot state={state} />
              <button
                className="inf-prov-model-check-btn"
                onClick={onCheck}
                disabled={state.status === 'checking' || busy}
              >
                {state.status === 'idle' || state.status === 'checking' ? 'Check' : 'Recheck'}
              </button>
            </>
          ) : (
            <span className="inf-prov-model-off-note">
              {model.enabled ? 'not configured' : 'disabled'}
            </span>
          )}
          {onRemove && (
            <button
              className="inf-prov-model-remove-btn"
              onClick={onRemove}
              disabled={busy}
              title={`Remove ${model.wire_id} from this configuration`}
              aria-label={`Remove ${model.wire_id}`}
            >
              <X size={12} />
            </button>
          )}
        </span>
      </div>
      {showLiveness && state.status === 'error' && (
        <p className="inf-prov-model-error">{state.error}</p>
      )}
      {onEdit && (
        <ModelEditFields
          key={`${model.display_name}\u0000${model.max_input_tokens}`}
          model={model}
          busy={busy}
          onEdit={onEdit}
        />
      )}
    </div>
  );
}

interface ModelGroup {
  // null renders the group without its own heading (single-family providers)
  title: string | null;
  configured: boolean;
  models: InferenceModelInfo[];
}

/**
 * Right-hand Models panel: checkbox-enabled rows with the server-stored
 * liveness verdict per model and Recheck buttons that re-run the live check
 * and update the server-global store.
 */
function ModelsPanel({
  groups,
  busy,
  saveError,
  note,
  onToggle,
  onRemove,
  onEdit,
  children,
}: {
  groups: ModelGroup[];
  busy: boolean;
  saveError: string;
  // Extra sentence appended to the panel note (kind-specific guidance)
  note?: string;
  onToggle: (model: InferenceModelInfo, enabled: boolean) => void;
  onRemove?: (model: InferenceModelInfo) => void;
  onEdit?: (model: InferenceModelInfo, edit: ModelEdit) => void;
  children?: React.ReactNode;
}) {
  const { stateFor, runCheck, runAll, anyChecking } = useModelChecks();
  const nonEmpty = groups.filter((group) => group.models.length > 0);
  const checkable = nonEmpty
    .filter((g) => g.configured)
    .flatMap((g) => g.models.filter((m) => m.enabled));

  return (
    <div className="inf-prov-models">
      <div className="inf-prov-models-header">
        <h5 className="inf-prov-subsection-title">Models</h5>
        <button
          className="inf-prov-model-check-btn"
          onClick={() => runAll(checkable)}
          disabled={anyChecking || busy || checkable.length === 0}
        >
          Recheck all
        </button>
      </div>
      <p className="inf-prov-models-note">
        Unchecked models are hidden from the model picker and never
        health-checked. Enabled models are checked at server startup with a
        minimal real inference call; the dot shows the latest result.
        {note ? ` ${note}` : ''}
      </p>
      {nonEmpty.map((group, index) => (
        <div key={group.title ?? index} className="inf-prov-subsection">
          {group.title && <h5 className="inf-prov-subsection-title">{group.title}</h5>}
          {group.models.map((model) => (
            <ModelRow
              key={model.id}
              model={model}
              state={stateFor(model)}
              checkable={group.configured}
              busy={busy}
              onCheck={() => runCheck(model.id)}
              onToggle={(enabled) => onToggle(model, enabled)}
              onRemove={onRemove ? () => onRemove(model) : undefined}
              onEdit={onEdit ? (edit) => onEdit(model, edit) : undefined}
            />
          ))}
          {!group.configured && (
            <p className="inf-prov-unconfigured-note">
              Not configured — checks unavailable.
            </p>
          )}
        </div>
      ))}
      {children}
      {saveError && <p className="inf-prov-model-error">{saveError}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Vertex card
// ---------------------------------------------------------------------------

/**
 * Read-only environment info for Vertex AI plus the fixed model catalog with
 * per-model enable checkboxes (saved immediately as the disabled set).
 */
function VertexProviderCard({ status: initial }: { status: VertexProviderStatus }) {
  const [status, setStatus] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState('');
  const creds = status.detail.credentials;

  const handleToggle = useCallback(async (model: InferenceModelInfo, enabled: boolean) => {
    const disabled = status.models
      .filter((m) => (m.id === model.id ? !enabled : !m.enabled))
      .map((m) => m.id);
    setBusy(true);
    setSaveError('');
    try {
      setStatus(await updateVertexModels({ disabled_models: disabled }));
    } catch (error) {
      console.error('Failed to update Vertex models:', error);
      setSaveError(errorMessage(error, 'Failed to save'));
    } finally {
      setBusy(false);
    }
  }, [status.models]);

  return (
    <CredentialCard
      fallbackLabel={status.label}
      loading={false}
      loadError=""
      detail={{ label: status.label, configured: status.configured, source: null }}
    >
      <div className="inf-prov-columns">
        <div className="inf-prov-main">
          <p className="inf-prov-detected-note">
            Detected from the server environment and server_config.json — read-only.
          </p>
          <div className="inf-prov-subsection">
            <h5 className="inf-prov-subsection-title">Credentials</h5>
            {creds.source ? (
              <>
                <InfoRow label="Source" value={CREDENTIALS_SOURCE_LABELS[creds.source]} />
                {creds.key_path && <InfoRow label="File" value={creds.key_path} mono />}
                {creds.service_account_email && (
                  <InfoRow label="Service account" value={creds.service_account_email} mono />
                )}
                {creds.project_id && <InfoRow label="Key project" value={creds.project_id} mono />}
                {creds.problem && <p className="inf-prov-problem">{creds.problem}</p>}
              </>
            ) : (
              <p className="inf-prov-unconfigured-note">
                No Google credentials detected (GOOGLE_APPLICATION_CREDENTIALS or
                gcloud Application Default Credentials). On Google Cloud VMs the
                instance service account may still apply.
              </p>
            )}
          </div>
          <VertexSectionRows title="Claude on Vertex" section={status.detail.anthropic} />
          <VertexSectionRows title="Gemini on Vertex" section={status.detail.gemini_vertex} />
        </div>
        <ModelsPanel
          groups={[
            {
              title: 'Claude on Vertex',
              configured: status.detail.anthropic.configured,
              models: status.models.filter((m) => m.family === 'anthropic'),
            },
            {
              title: 'Gemini on Vertex',
              configured: status.detail.gemini_vertex.configured,
              models: status.models.filter((m) => m.family === 'gemini_vertex'),
            },
          ]}
          busy={busy}
          saveError={saveError}
          onToggle={handleToggle}
        />
      </div>
    </CredentialCard>
  );
}

// ---------------------------------------------------------------------------
// Model typeahead (OpenRouter catalog / self-hosted server discovery)
// ---------------------------------------------------------------------------

function formatContext(tokens: number | null): string {
  if (!tokens) return '';
  return tokens >= 1_000_000
    ? `${(tokens / 1_000_000).toFixed(tokens % 1_000_000 ? 1 : 0)}M ctx`
    : `${Math.round(tokens / 1000)}K ctx`;
}

/** One typeahead row's muted second line. */
function catalogMeta(m: OpenRouterCatalogModel): string {
  const bits = [m.name];
  if (m.detail) bits.push(m.detail);
  if (m.context_length) bits.push(formatContext(m.context_length));
  if (m.capabilities && !m.capabilities.includes('tools')) bits.push('no tool support');
  return bits.join(' · ');
}

type CatalogSearch = (query: string) => Promise<{ models: OpenRouterCatalogModel[]; error: string | null }>;

/**
 * "Add model" combobox: debounced substring search over a catalog (the
 * cached OpenRouter list, or what a self-hosted server reports right now),
 * plus a "use as custom id" row so a model the catalog does not list yet
 * (or an unreachable catalog/server) never blocks the admin. Enter picks
 * the highlighted row; Escape closes.
 */
function AddModelCombobox({
  existing,
  disabled,
  search,
  placeholder,
  emptyHint,
  onAdd,
}: {
  existing: Set<string>;
  disabled: boolean;
  search: CatalogSearch;
  placeholder: string;
  emptyHint: string;
  onAdd: (wireId: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [results, setResults] = useState<OpenRouterCatalogModel[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const requestSeq = useRef(0);

  useEffect(() => {
    if (!open) return;
    const seq = ++requestSeq.current;
    setSearching(true);
    const timer = window.setTimeout(async () => {
      try {
        const response = await search(query.trim());
        if (seq !== requestSeq.current) return;
        setResults(response.models.filter((m) => !existing.has(m.id)));
        setCatalogError(response.error);
      } catch (error) {
        if (seq !== requestSeq.current) return;
        setResults([]);
        setCatalogError(errorMessage(error, 'Catalog unavailable'));
      } finally {
        if (seq === requestSeq.current) setSearching(false);
      }
    }, 200);
    return () => window.clearTimeout(timer);
  }, [query, open, existing, search]);

  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  const trimmed = query.trim();
  const customCandidate =
    trimmed && !existing.has(trimmed) && !results.some((m) => m.id === trimmed) ? trimmed : null;
  const optionCount = results.length + (customCandidate ? 1 : 0);

  const pick = useCallback((wireId: string) => {
    onAdd(wireId);
    setQuery('');
    setOpen(false);
    setHighlight(0);
  }, [onAdd]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      setOpen(false);
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setOpen(true);
      setHighlight((h) => Math.min(h + 1, Math.max(optionCount - 1, 0)));
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (optionCount === 0) return;
      const index = Math.min(highlight, optionCount - 1);
      if (index < results.length) pick(results[index].id);
      else if (customCandidate) pick(customCandidate);
    }
  };

  return (
    <div className="inf-prov-add-model" ref={containerRef}>
      <input
        type="text"
        className="inf-prov-add-model-input"
        placeholder={placeholder}
        value={query}
        disabled={disabled}
        onChange={(e) => {
          setQuery(e.target.value);
          setHighlight(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={handleKeyDown}
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        aria-controls="inf-prov-add-model-listbox"
      />
      {open && (
        <div className="inf-prov-typeahead-dropdown" role="listbox" id="inf-prov-add-model-listbox">
          {results.map((m, index) => (
            <div
              key={m.id}
              role="option"
              aria-selected={index === highlight}
              className={`inf-prov-typeahead-option${index === highlight ? ' active' : ''}`}
              onMouseEnter={() => setHighlight(index)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(m.id)}
            >
              <span className="inf-prov-typeahead-id">{m.id}</span>
              <span className="inf-prov-typeahead-meta">{catalogMeta(m)}</span>
            </div>
          ))}
          {customCandidate && (
            <div
              role="option"
              aria-selected={highlight === results.length}
              className={`inf-prov-typeahead-option${highlight === results.length ? ' active' : ''}`}
              onMouseEnter={() => setHighlight(results.length)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(customCandidate)}
            >
              <span className="inf-prov-typeahead-id">{customCandidate}</span>
              <span className="inf-prov-typeahead-meta">Use as a custom model id</span>
            </div>
          )}
          {optionCount === 0 && (
            <div className="inf-prov-typeahead-empty">
              {searching ? 'Searching…' : trimmed ? 'Already added' : emptyHint}
            </div>
          )}
          {catalogError && (
            <div className="inf-prov-typeahead-empty inf-prov-typeahead-warning">
              Model list unavailable ({catalogError}) — custom ids still work.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const openRouterSearch: CatalogSearch = async (q) => {
  const response = await searchOpenRouterCatalog(q, { limit: 12 });
  return { models: response.models, error: response.error };
};

// ---------------------------------------------------------------------------
// Provider-instance card (one OpenRouter configuration or self-hosted server)
// ---------------------------------------------------------------------------

/** Three-dot header menu holding the card's destructive action. */
function CardMenu({ items }: { items: { label: string; onClick: () => void; disabled?: boolean; danger?: boolean }[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className="inf-prov-kebab" ref={ref}>
      <button
        type="button"
        className="inf-prov-kebab-btn"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More actions"
        title="More actions"
      >
        <MoreVertical size={16} />
      </button>
      {open && (
        <div className="inf-prov-kebab-menu" role="menu">
          {items.map((item) => (
            <button
              type="button"
              key={item.label}
              role="menuitem"
              className={`inf-prov-kebab-item${item.danger ? ' danger' : ''}`}
              disabled={item.disabled}
              onClick={() => {
                setOpen(false);
                item.onClick();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function InstanceCard({
  status: initial,
  endpoint,
  apiTypes,
  onDeleted,
}: {
  status: InferenceInstanceStatus;
  // The kind carries its own base URL + API type (self-hosted server)
  endpoint: boolean;
  apiTypes: InferenceApiType[];
  onDeleted: (instanceId: string) => void;
}) {
  const [status, setStatus] = useState(initial);
  const [label, setLabel] = useState(initial.label);
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState(initial.base_url ?? '');
  const [apiType, setApiType] = useState(initial.api_type ?? apiTypes[0]?.id ?? 'openai');
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [saveError, setSaveError] = useState('');
  const [modelsBusy, setModelsBusy] = useState(false);
  const [modelsError, setModelsError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const statusTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
    };
  }, []);

  const labelDirty = label.trim() !== status.label;
  const baseUrlDirty = endpoint && baseUrl.trim() !== (status.base_url ?? '');
  const apiTypeDirty = endpoint && apiType !== status.api_type;
  const selectedApiType = apiTypes.find((t) => t.id === apiType);

  const handleSave = useCallback(async () => {
    setSaveStatus('saving');
    setSaveError('');
    try {
      const updated = await updateInferenceInstance(status.id, {
        label: labelDirty ? label.trim() : undefined,
        api_key: apiKey,
        base_url: baseUrlDirty ? baseUrl.trim() : undefined,
        api_type: apiTypeDirty ? apiType : undefined,
      });
      setStatus(updated);
      setLabel(updated.label);
      setBaseUrl(updated.base_url ?? '');
      setApiType(updated.api_type ?? apiType);
      setApiKey('');
      setSaveStatus('saved');
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      statusTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 2000);
    } catch (error) {
      console.error(`Failed to save instance ${status.id}:`, error);
      setSaveStatus('error');
      setSaveError(errorMessage(error, 'Failed to save'));
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      statusTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 4000);
    }
  }, [status.id, label, labelDirty, apiKey, baseUrl, baseUrlDirty, apiType, apiTypeDirty]);

  type ModelListEntry = { id: string; enabled: boolean; name?: string; context_length?: number };

  const saveModels = useCallback(async (models: ModelListEntry[]) => {
    setModelsBusy(true);
    setModelsError('');
    try {
      setStatus(await updateInferenceInstance(status.id, { models }));
    } catch (error) {
      console.error(`Failed to update models for ${status.id}:`, error);
      setModelsError(errorMessage(error, 'Failed to save'));
    } finally {
      setModelsBusy(false);
    }
  }, [status.id]);

  const currentModels = useCallback(
    (): ModelListEntry[] => status.models.map((m) => ({ id: m.wire_id, enabled: m.enabled })),
    [status.models],
  );

  const handleToggle = (model: InferenceModelInfo, enabled: boolean) =>
    saveModels(currentModels().map((m) => (m.id === model.wire_id ? { ...m, enabled } : m)));
  const handleRemove = (model: InferenceModelInfo) =>
    saveModels(currentModels().filter((m) => m.id !== model.wire_id));
  const handleAdd = (wireId: string) =>
    saveModels([...currentModels(), { id: wireId, enabled: true }]);
  const handleEdit = (model: InferenceModelInfo, edit: ModelEdit) =>
    saveModels(currentModels().map((m) => (m.id === model.wire_id ? { ...m, ...edit } : m)));

  const instanceSearch = useCallback<CatalogSearch>(
    async (q) => {
      const response = await searchInstanceCatalog(status.id, q, { limit: 12 });
      return { models: response.models, error: response.error };
    },
    [status.id],
  );

  const handleDelete = useCallback(async () => {
    const confirmed = window.confirm(
      `Remove the "${status.label}" configuration? Its ${endpoint ? 'server settings' : 'API key'} and model list are deleted; ` +
      'conversations already using its models will fail on their next message.',
    );
    if (!confirmed) return;
    setDeleting(true);
    try {
      await deleteInferenceInstance(status.id);
      onDeleted(status.id);
    } catch (error) {
      console.error(`Failed to delete instance ${status.id}:`, error);
      setModelsError(errorMessage(error, 'Failed to remove'));
      setDeleting(false);
    }
  }, [status.id, status.label, endpoint, onDeleted]);

  const existing = new Set(status.models.map((m) => m.wire_id));
  const dirty = !!apiKey.trim() || labelDirty || baseUrlDirty || apiTypeDirty;

  return (
    <CredentialCard
      fallbackLabel={status.label}
      loading={false}
      loadError=""
      detail={{ label: `${status.label} · ${status.kind_label}`, configured: status.configured, source: status.source }}
      hideBadge
      headerAction={
        <CardMenu
          items={[{
            label: deleting ? 'Removing…' : 'Remove configuration',
            onClick: handleDelete,
            disabled: deleting,
            danger: true,
          }]}
        />
      }
    >
      <div className="inf-prov-columns">
        <div className="inf-prov-main">
          <p className="inf-prov-detected-note">
            Instance id <code>{status.id}</code> — its models are stored as{' '}
            <code>{status.id}:&lt;model id&gt;</code>.
          </p>
          <CredentialField
            id={`inf-prov-${status.id}-label`}
            label="Label"
            value={label}
            onChange={setLabel}
            placeholder={status.kind_label}
          />
          {endpoint && (
            <>
              <CredentialField
                id={`inf-prov-${status.id}-base-url`}
                label="Server URL"
                value={baseUrl}
                onChange={setBaseUrl}
                placeholder="http://192.168.1.20:8080 or http://ollama-box:11434"
              />
              <div className="svc-cred-row">
                <label htmlFor={`inf-prov-${status.id}-api-type`} className="svc-cred-label">
                  API type
                </label>
                <select
                  id={`inf-prov-${status.id}-api-type`}
                  className="svc-cred-input inf-prov-select"
                  value={apiType}
                  onChange={(e) => setApiType(e.target.value)}
                >
                  {apiTypes.map((t) => (
                    <option key={t.id} value={t.id}>{t.label}</option>
                  ))}
                </select>
                {selectedApiType && (
                  <p className="inf-prov-field-hint">{selectedApiType.description}</p>
                )}
              </div>
            </>
          )}
          <CredentialField
            id={`inf-prov-${status.id}-api-key`}
            label="API key"
            value={apiKey}
            onChange={setApiKey}
            placeholder={secretPlaceholder(status.credentials.api_key_set, status.hint)}
            secret
            optional={!status.key_required}
          />
          <SaveActions
            saveStatus={saveStatus}
            saveError={saveError}
            disabled={!dirty}
            onSave={handleSave}
          />
        </div>
        <ModelsPanel
          groups={[{ title: null, configured: status.configured, models: status.models }]}
          busy={modelsBusy || deleting}
          saveError={modelsError}
          note={
            endpoint
              ? 'Self-hosted models are priced at $0. Rename a model or set its context window below each row' +
                (apiType === 'ollama' ? ' — for Ollama the context window is requested on every call.' : '.') +
                ' The system prompt alone is roughly 17K tokens, so the server must allow at least a 32K context' +
                (apiType === 'ollama' ? '.' : ' (llama.cpp: -c 32768).')
              : undefined
          }
          onToggle={handleToggle}
          onRemove={handleRemove}
          onEdit={endpoint ? handleEdit : undefined}
        >
          <AddModelCombobox
            existing={existing}
            disabled={modelsBusy || deleting || (endpoint && !status.configured)}
            search={endpoint ? instanceSearch : openRouterSearch}
            placeholder={
              endpoint
                ? status.configured
                  ? 'Add a model — pick one the server reports or type its name'
                  : 'Save the server URL first'
                : 'Add a model — search the OpenRouter catalog or type an id'
            }
            emptyHint={endpoint ? 'The server reports no other models' : 'Type to search the catalog'}
            onAdd={handleAdd}
          />
        </ModelsPanel>
      </div>
    </CredentialCard>
  );
}

// ---------------------------------------------------------------------------
// Section
// ---------------------------------------------------------------------------

/**
 * Admin-only panel for LLM inference provider configuration. Vertex AI is
 * shown read-only (its credentials and project config are detected from the
 * server environment) with per-model enable checkboxes; every provider
 * instance gets its own editable card -- an OpenRouter configuration with a
 * label, a write-only API key and a model list fed by the OpenRouter
 * catalog typeahead, or a self-hosted server with a label, server URL, API
 * type (OpenAI-compatible / Ollama), optional key and a model list fed by
 * what the server reports, with inline name / context editing per model.
 * Each card's Models panel shows the real model id string used in API
 * calls, the server-stored liveness verdict per model (startup sweep +
 * rechecks), and Recheck buttons. Instances are added from the buttons at
 * the bottom.
 */
export function InferenceProvidersSection() {
  const [data, setData] = useState<InferenceProvidersListResponse | null>(null);
  const [loadError, setLoadError] = useState('');
  const [adding, setAdding] = useState<string | null>(null);
  const [addError, setAddError] = useState('');

  useEffect(() => {
    const load = async () => {
      try {
        setData(await fetchInferenceProviders());
      } catch (error) {
        console.error('Failed to load inference providers:', error);
        setLoadError(errorMessage(error, 'Failed to load'));
      }
    };
    load();
  }, []);

  const handleAdd = useCallback(async (kind: string) => {
    setAdding(kind);
    setAddError('');
    try {
      const created = await createInferenceInstance({ kind });
      setData((prev) => (prev ? { ...prev, instances: [...prev.instances, created] } : prev));
    } catch (error) {
      console.error(`Failed to add ${kind} instance:`, error);
      setAddError(errorMessage(error, 'Failed to add configuration'));
    } finally {
      setAdding(null);
    }
  }, []);

  const handleDeleted = useCallback((instanceId: string) => {
    setData((prev) =>
      prev ? { ...prev, instances: prev.instances.filter((i) => i.id !== instanceId) } : prev,
    );
  }, []);

  return (
    <div className="settings-section">
      <h3>Inference Providers</h3>
      <p className="settings-description">
        LLM backends used to run conversations. These apply to all users;
        only enabled models with configured credentials appear in the model
        picker. Admin only.
      </p>
      {loadError ? (
        <div className="svc-cred-load-error">{loadError}</div>
      ) : data === null ? (
        <div className="settings-loading">Loading...</div>
      ) : (
        <>
          <div className="inf-prov-cards">
            <VertexProviderCard status={data.vertex} />
            {data.instances.map((instance) => (
              <InstanceCard
                key={instance.id}
                status={instance}
                endpoint={data.kinds.find((k) => k.kind === instance.kind)?.endpoint ?? false}
                apiTypes={data.api_types}
                onDeleted={handleDeleted}
              />
            ))}
          </div>
          <div className="inf-prov-footer">
            {data.kinds.map((kind) => (
              <button
                key={kind.kind}
                className="inf-prov-add-btn"
                onClick={() => handleAdd(kind.kind)}
                disabled={adding !== null}
              >
                <Plus size={14} />
                {adding === kind.kind ? 'Adding…' : `Add ${kind.label} configuration`}
              </button>
            ))}
            {addError && <span className="inf-prov-model-error">{addError}</span>}
          </div>
        </>
      )}
    </div>
  );
}
