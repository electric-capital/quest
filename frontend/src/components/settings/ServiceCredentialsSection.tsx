import { useState, useEffect, useCallback, useRef } from 'react';
import type { ReactNode } from 'react';
import { ServiceIcon } from './serviceIcons';
import { fetchServiceCredentials, updateServiceCredentials } from '../../api/client';
import type {
  CredentialFieldSchema,
  ServiceCredentialDetail,
} from '../../api/types';
import './ServiceCredentialsSection.css';

export type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

export function secretPlaceholder(isSet: boolean, hint: string): string {
  return isSet ? 'Leave blank to keep the stored value' : hint;
}

/**
 * The subset of a credential/provider status the shared card chrome needs;
 * satisfied structurally by both ServiceCredentialDetail and the inference
 * provider statuses.
 */
export interface CredentialCardStatus {
  label: string;
  configured: boolean;
  source: 'store' | 'legacy' | null;
}

/** Card chrome shared by every service: header, badge, legacy note, body.
 *
 * ``hideBadge`` drops the Configured / Not configured pill (for cards whose
 * presence already implies configuration, e.g. inference provider
 * instances) and ``headerAction`` renders a control at the header's right
 * edge (e.g. a kebab menu). */
export function CredentialCard({
  fallbackLabel,
  loading,
  loadError,
  detail,
  hideBadge = false,
  headerAction,
  children,
}: {
  fallbackLabel: string;
  loading: boolean;
  loadError: string;
  detail: CredentialCardStatus | null;
  hideBadge?: boolean;
  headerAction?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="svc-cred-card">
      <div className="svc-cred-card-header">
        <h4>{detail?.label ?? fallbackLabel}</h4>
        <div className="svc-cred-card-header-actions">
          {!loading && !loadError && !hideBadge && (
            <span className={`svc-cred-badge ${detail?.configured ? 'configured' : 'unconfigured'}`}>
              {detail?.configured ? 'Configured' : 'Not configured'}
            </span>
          )}
          {headerAction}
        </div>
      </div>
      {loading ? (
        <div className="settings-loading">Loading...</div>
      ) : loadError ? (
        <div className="svc-cred-load-error">{loadError}</div>
      ) : (
        <>
          {detail?.source === 'legacy' && (
            <p className="svc-cred-legacy-note">
              Currently read from a legacy credentials file on the server.
              Saving here moves the credentials to the per-service store,
              which takes precedence from then on.
            </p>
          )}
          {children}
        </>
      )}
    </div>
  );
}

export function CredentialField({
  id,
  label,
  value,
  onChange,
  placeholder,
  secret = false,
  optional = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  secret?: boolean;
  optional?: boolean;
}) {
  return (
    <div className="svc-cred-row">
      <label htmlFor={id} className="svc-cred-label">
        {label} {optional && <span className="svc-cred-optional">(optional)</span>}
      </label>
      <input
        id={id}
        className="svc-cred-input"
        type={secret ? 'password' : 'text'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete={secret ? 'new-password' : 'off'}
      />
    </div>
  );
}

export function SaveActions({
  saveStatus,
  saveError,
  disabled,
  onSave,
}: {
  saveStatus: SaveStatus;
  saveError: string;
  disabled: boolean;
  onSave: () => void;
}) {
  return (
    <div className="settings-actions">
      <button
        className="settings-save-button"
        onClick={onSave}
        disabled={saveStatus === 'saving' || disabled}
      >
        {saveStatus === 'saving' ? 'Saving...' : 'Save'}
      </button>
      {saveStatus === 'saved' && (
        <span className="settings-save-status saved">Saved</span>
      )}
      {saveStatus === 'error' && (
        <span className="settings-save-status error">
          {saveError || 'Failed to save'}
        </span>
      )}
    </div>
  );
}

type FormValues = Record<string, string | boolean>;

/** Initial editable values from a fetched detail: secrets always reset to
 * empty (the server never returns them; empty means keep on save). */
function formToValues(detail: ServiceCredentialDetail): FormValues {
  const values: FormValues = {};
  for (const field of detail.fields) {
    if (field.type === 'bool') {
      values[field.key] = detail.credentials[field.key] === true;
    } else if (field.type === 'secret') {
      values[field.key] = '';
    } else {
      const raw = detail.credentials[field.key];
      values[field.key] = typeof raw === 'string' ? raw : '';
    }
  }
  return values;
}

function isFieldVisible(field: CredentialFieldSchema, values: FormValues): boolean {
  return !field.visible_if || values[field.visible_if] === true;
}

function isFieldRequired(field: CredentialFieldSchema, values: FormValues): boolean {
  if (field.required_if) return values[field.required_if] === true;
  return field.required;
}

/**
 * One service's credential form, rendered entirely from the backend's
 * CredentialFieldSchema list -- core services and plugins share this
 * component, so adding a service requires no frontend change. Chrome
 * (card, header, icon) is supplied by the caller; ``onSaved`` receives the
 * server's updated detail after a successful save.
 */
function CredentialForm({
  detail,
  onSaved,
  onCancel,
}: {
  detail: ServiceCredentialDetail;
  onSaved: (updated: ServiceCredentialDetail) => void;
  onCancel: () => void;
}) {
  const [values, setValues] = useState<FormValues>(() => formToValues(detail));
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [saveError, setSaveError] = useState('');
  const statusTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
    };
  }, []);

  const setValue = useCallback((key: string, value: string | boolean) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handleSave = useCallback(async () => {
    setSaveStatus('saving');
    setSaveError('');
    try {
      const updated = await updateServiceCredentials(detail.service, values);
      onSaved(updated);
    } catch (error) {
      console.error(`Failed to save ${detail.service} credentials:`, error);
      setSaveStatus('error');
      setSaveError(error instanceof Error ? error.message : 'Failed to save');
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      statusTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 4000);
    }
  }, [detail.service, values, onSaved]);

  // Required text fields must be filled before saving; required secrets are
  // left to the server (an empty value may legitimately keep a stored one).
  const missingRequired = detail.fields.some(
    (field) =>
      field.type !== 'bool' &&
      field.type !== 'secret' &&
      isFieldVisible(field, values) &&
      isFieldRequired(field, values) &&
      !String(values[field.key] ?? '').trim(),
  );

  return (
    <div className="svc-cred-form">
      {detail.source === 'legacy' && (
        <p className="svc-cred-legacy-note">
          Currently read from a legacy credentials file on the server.
          Saving here moves the credentials to the per-service store,
          which takes precedence from then on.
        </p>
      )}
      {detail.fields.map((field) => {
        if (!isFieldVisible(field, values)) return null;
        const id = `svc-cred-${detail.service}-${field.key}`;
        const optional = !isFieldRequired(field, values);
        if (field.type === 'bool') {
          return (
            <label key={field.key} className="svc-cred-toggle">
              <input
                type="checkbox"
                className="svc-cred-toggle-input"
                checked={values[field.key] === true}
                onChange={(e) => setValue(field.key, e.target.checked)}
              />
              <span className="svc-cred-toggle-track" aria-hidden="true">
                <span className="svc-cred-toggle-knob" />
              </span>
              <span className="svc-cred-toggle-text">{field.label}</span>
            </label>
          );
        }
        if (field.type === 'textarea') {
          return (
            <div key={field.key} className="svc-cred-row">
              <label htmlFor={id} className="svc-cred-label">
                {field.label}{' '}
                {optional && <span className="svc-cred-optional">(optional)</span>}
              </label>
              <textarea
                id={id}
                className="svc-cred-textarea"
                value={String(values[field.key] ?? '')}
                onChange={(e) => setValue(field.key, e.target.value)}
                placeholder={field.placeholder}
                rows={3}
                spellCheck={false}
              />
            </div>
          );
        }
        const secret = field.type === 'secret';
        return (
          <CredentialField
            key={field.key}
            id={id}
            label={field.label}
            value={String(values[field.key] ?? '')}
            onChange={(value) => setValue(field.key, value)}
            placeholder={
              secret
                ? secretPlaceholder(
                    detail.credentials[`${field.key}_set`] === true,
                    field.placeholder,
                  )
                : field.placeholder
            }
            secret={secret}
            optional={optional}
          />
        );
      })}
      <div className="svc-cred-form-actions">
        <SaveActions
          saveStatus={saveStatus}
          saveError={saveError}
          disabled={missingRequired}
          onSave={handleSave}
        />
        <button
          type="button"
          className="svc-cred-cancel"
          onClick={onCancel}
          disabled={saveStatus === 'saving'}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/** Read-only value shown for one field on a configured service's card. */
function summaryValue(field: CredentialFieldSchema, detail: ServiceCredentialDetail): ReactNode {
  const creds = detail.credentials;
  if (field.type === 'bool') {
    return creds[field.key] === true ? 'On' : 'Off';
  }
  if (field.type === 'secret') {
    return creds[`${field.key}_set`] === true ? (
      <span className="svc-cred-summary-secret" aria-label="Set">
        ••••••••
      </span>
    ) : (
      <span className="svc-cred-summary-empty">Not set</span>
    );
  }
  const raw = creds[field.key];
  const text = typeof raw === 'string' ? raw.trim() : '';
  if (!text) return <span className="svc-cred-summary-empty">Not set</span>;
  return field.type === 'textarea' ? <pre className="svc-cred-summary-pre">{text}</pre> : text;
}

/** Fields worth listing on the summary: hidden-by-toggle fields are skipped. */
function summaryFields(detail: ServiceCredentialDetail): CredentialFieldSchema[] {
  const values = formToValues(detail);
  return detail.fields.filter((field) => isFieldVisible(field, values));
}

/**
 * Card for a configured service: brand icon, label, a read-only listing of
 * the stored parameters (secrets masked) and an Edit button that swaps the
 * listing for the form in place.
 */
function ConfiguredServiceCard({
  detail,
  editing,
  onEdit,
  onSaved,
  onCancel,
}: {
  detail: ServiceCredentialDetail;
  editing: boolean;
  onEdit: () => void;
  onSaved: (updated: ServiceCredentialDetail) => void;
  onCancel: () => void;
}) {
  return (
    <div className="svc-cred-card svc-cred-configured">
      <div className="svc-cred-card-header">
        <div className="svc-cred-card-title">
          <span className="svc-cred-card-icon">
            <ServiceIcon service={detail.service} />
          </span>
          <h4>{detail.label}</h4>
        </div>
        <div className="svc-cred-card-header-actions">
          {detail.source === 'legacy' && (
            <span className="svc-cred-badge legacy" title="Read from a legacy credentials file">
              Legacy file
            </span>
          )}
          {!editing && (
            <button type="button" className="svc-cred-edit" onClick={onEdit}>
              Edit
            </button>
          )}
        </div>
      </div>
      {editing ? (
        <CredentialForm detail={detail} onSaved={onSaved} onCancel={onCancel} />
      ) : (
        <dl className="svc-cred-summary">
          {summaryFields(detail).map((field) => (
            <div key={field.key} className="svc-cred-summary-row">
              <dt>{field.label}</dt>
              <dd>{summaryValue(field, detail)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

/**
 * Admin-only editor for server-level upstream service credentials. The
 * service roster and every service's fields come from
 * GET /admin/service-credentials (core services and loaded plugins alike) --
 * nothing here is per-service. Configured services are listed at the top
 * with their stored parameters; unconfigured ones sit in an icon grid below
 * and expand into their form when picked. Credentials are persisted
 * server-side in per-service files under the data directory. Secret fields
 * are write-only: the server never returns them, and leaving a field blank
 * keeps the stored value.
 */
export function ServiceCredentialsSection() {
  const [services, setServices] = useState<ServiceCredentialDetail[] | null>(null);
  const [loadError, setLoadError] = useState('');
  // Service id whose form is currently open (a configured card being edited
  // or an unconfigured service picked from the grid); one at a time.
  const [openService, setOpenService] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const response = await fetchServiceCredentials();
        setServices(response.services);
      } catch (error) {
        console.error('Failed to load service credentials:', error);
        setLoadError(error instanceof Error ? error.message : 'Failed to load');
      }
    };
    load();
  }, []);

  const handleSaved = useCallback((updated: ServiceCredentialDetail) => {
    setServices((prev) =>
      prev ? prev.map((s) => (s.service === updated.service ? updated : s)) : prev,
    );
    setOpenService(null);
  }, []);

  const closeForm = useCallback(() => setOpenService(null), []);

  const configured = services?.filter((s) => s.configured) ?? [];
  const addable = services?.filter((s) => !s.configured) ?? [];
  const picked = addable.find((s) => s.service === openService) ?? null;

  return (
    <div className="settings-section">
      <h3>Service Credentials</h3>
      <p className="settings-description">
        Server-level credentials for upstream API integrations. These apply to
        all users and are stored in per-service files in the server's data
        directory. Admin only.
      </p>
      {loadError ? (
        <div className="svc-cred-load-error">{loadError}</div>
      ) : services === null ? (
        <div className="settings-loading">Loading...</div>
      ) : (
        <>
          {configured.length === 0 ? (
            <p className="svc-cred-empty">
              No services configured yet. Pick one below to set it up.
            </p>
          ) : (
            <div className="svc-cred-configured-list">
              {configured.map((detail) => (
                <ConfiguredServiceCard
                  key={detail.service}
                  detail={detail}
                  editing={openService === detail.service}
                  onEdit={() => setOpenService(detail.service)}
                  onSaved={handleSaved}
                  onCancel={closeForm}
                />
              ))}
            </div>
          )}
          {addable.length > 0 && (
            <div className="svc-cred-add">
              {picked ? (
                <div className="svc-cred-card svc-cred-add-panel">
                  <div className="svc-cred-card-header">
                    <div className="svc-cred-card-title">
                      <span className="svc-cred-card-icon">
                        <ServiceIcon service={picked.service} />
                      </span>
                      <h4>Configure {picked.label}</h4>
                    </div>
                  </div>
                  <CredentialForm
                    key={picked.service}
                    detail={picked}
                    onSaved={handleSaved}
                    onCancel={closeForm}
                  />
                </div>
              ) : (
                <>
                  <h4 className="svc-cred-add-title">Add a service</h4>
                  <div className="svc-cred-add-grid">
                    {addable.map((detail) => (
                      <button
                        key={detail.service}
                        type="button"
                        className="svc-cred-add-tile"
                        title={`Configure ${detail.label}`}
                        onClick={() => setOpenService(detail.service)}
                      >
                        <span className="svc-cred-add-tile-icon">
                          <ServiceIcon service={detail.service} />
                        </span>
                        <span className="svc-cred-add-tile-name">{detail.label}</span>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
