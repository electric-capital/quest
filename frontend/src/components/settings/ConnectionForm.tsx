import { useEffect, useMemo, useRef, useState } from 'react';
import { ConnectionApiError, createConnection, updateConnection } from '../../api/client';
import type {
  ConnectionRow,
  ConnectorRow,
  CreateConnectionBody,
  SchemaFormValues,
  UpdateConnectionBody,
} from '../../api/types';
import { SchemaForm, initialSchemaValues, missingRequiredFields } from './SchemaForm';
import {
  DESCRIPTION_MAX_LENGTH,
  accountLabel,
  connectionErrorMessage,
  connectionFormFields,
  normalizeDescription,
  reconnectsSecretsOnly,
} from './connectionHelpers';

interface FormError {
  message: string;
  /** connection_exists: the existing connection to point at. */
  existing?: { id: number | null; account?: string };
}

export interface ConnectionFormProps {
  connector: ConnectorRow;
  /** Omitted: add a new connection. Set: re-enter that connection's credentials. */
  connection?: ConnectionRow;
  /** Called after a successful save (the parent closes the form and refreshes). */
  onDone: () => void;
  /** connection_exists: scroll to / highlight the existing connection's card. */
  onShowExisting?: (connectionId: number | null, account?: string) => void;
  /** Escape inside the form: close just this form (the Settings modal stays open). */
  onCancel?: () => void;
  /** Focus the first field on mount. */
  autoFocus?: boolean;
}

/**
 * The add / re-enter form of a multi-connection service (form or api_key
 * kind). Adding POSTs `{values, description?}` (form) or
 * `{api_key, description?}` (api_key) to the row's connections_url;
 * re-entering PUTs `{values}` / `{api_key}` to that connection, see
 * connectionFormFields for which fields that form shows. Errors render
 * inline and the form keeps its values.
 */
export function ConnectionForm({
  connector,
  connection,
  onDone,
  onShowExisting,
  onCancel,
  autoFocus = false,
}: ConnectionFormProps) {
  const mode = connection ? 'reconnect' : 'add';
  const fields = useMemo(() => connectionFormFields(connector, mode), [connector, mode]);
  const [values, setValues] = useState<SchemaFormValues>(() => initialSchemaValues(fields));
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<FormError | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    if (!autoFocus) return;
    formRef.current?.querySelector<HTMLElement>('input, select, textarea')?.focus();
  }, [autoFocus]);

  const idPrefix = `connection-${connector.service}-${connection ? connection.id : 'new'}`;
  // Re-entering with every field optional (no secret fields) needs at least one value.
  const anyValue = fields.some((field) => field.type !== 'bool' && String(values[field.key] ?? '').trim());
  const missing = missingRequiredFields(fields, values, { checkSecrets: true }).length > 0;
  const canSubmit = !submitting && !missing && (mode === 'add' || anyValue);

  const handleChange = (key: string, value: string | boolean) => {
    setValues((prev) => ({ ...prev, [key]: value }));
    if (fieldErrors[key]) {
      setFieldErrors((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  };

  const buildBody = (): CreateConnectionBody | UpdateConnectionBody => {
    if (connector.kind !== 'form') {
      const apiKey = String(values.api_key ?? '').trim();
      if (mode === 'reconnect') return { api_key: apiKey };
      const desc = normalizeDescription(description);
      return desc ? { api_key: apiKey, description: desc } : { api_key: apiKey };
    }
    if (mode === 'reconnect') {
      // Blank fields are omitted so the server keeps their stored values.
      const submitted: SchemaFormValues = {};
      for (const field of fields) {
        const value = values[field.key];
        if (typeof value === 'string' && value.trim()) submitted[field.key] = value;
      }
      return { values: submitted };
    }
    const desc = normalizeDescription(description);
    return desc ? { values, description: desc } : { values };
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    setFieldErrors({});
    try {
      const url = connector.connections_url || '';
      if (connection) {
        await updateConnection(url, connection.id, buildBody() as UpdateConnectionBody);
      } else {
        await createConnection(url, buildBody() as CreateConnectionBody);
      }
      setSubmitting(false);
      onDone();
    } catch (err) {
      setSubmitting(false);
      const message = connectionErrorMessage(err, connector.label);
      if (err instanceof ConnectionApiError && err.code === 'invalid_values') {
        const perField: Record<string, string> = {};
        const unmatched: string[] = [];
        for (const [key, msg] of Object.entries(err.fields ?? {})) {
          if (fields.some((field) => field.key === key)) perField[key] = msg;
          else unmatched.push(`${key}: ${msg}`);
        }
        setFieldErrors(perField);
        setError({ message: [message, ...unmatched].join(' ') });
      } else if (err instanceof ConnectionApiError && err.code === 'connection_exists') {
        setError({ message, existing: { id: err.connectionId ?? null, account: err.account } });
      } else {
        setError({ message });
      }
    }
  };

  return (
    <form
      ref={formRef}
      className="svc-cred-form connection-form"
      onSubmit={handleSubmit}
      noValidate
      onKeyDown={(e) => {
        if (e.key === 'Escape' && onCancel) {
          // Close only this form, not the Settings modal around it.
          e.preventDefault();
          e.stopPropagation();
          onCancel();
        }
      }}
    >
      {connection && (
        <p className="connection-form-note">
          {reconnectsSecretsOnly(connector)
            ? `Re-enter the credentials for ${accountLabel(connection)}. Its other settings stay as they are.`
            : connector.kind === 'form'
              ? `Update the settings for ${accountLabel(connection)}. Fields left blank keep their current values.`
              : `Enter a new key for ${accountLabel(connection)}.`}
        </p>
      )}
      <SchemaForm
        fields={fields}
        values={values}
        onChange={handleChange}
        idPrefix={idPrefix}
        disabled={submitting}
        errors={fieldErrors}
      />
      {!connection && (
        <div className="svc-cred-row">
          <label htmlFor={`${idPrefix}-description`} className="svc-cred-label">
            Description <span className="svc-cred-optional">(optional)</span>
          </label>
          <input
            id={`${idPrefix}-description`}
            className="svc-cred-input"
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={DESCRIPTION_MAX_LENGTH}
            placeholder="A note for you and the assistant, e.g. what this account is for"
            autoComplete="off"
            disabled={submitting || undefined}
          />
        </div>
      )}
      {error && (
        <div className="connection-form-error" role="alert">
          <span>{error.message}</span>
          {error.existing && onShowExisting && (
            <>
              {' '}
              <button
                type="button"
                className="connection-link-btn"
                onClick={() => onShowExisting(error.existing!.id, error.existing!.account)}
              >
                Show it
              </button>
            </>
          )}
        </div>
      )}
      <div className="svc-cred-form-actions">
        <button type="submit" className="connector-btn connector-btn-connect connection-form-submit" disabled={!canSubmit}>
          {connection ? (submitting ? 'Saving...' : 'Save') : submitting ? 'Connecting...' : 'Connect'}
        </button>
      </div>
    </form>
  );
}
