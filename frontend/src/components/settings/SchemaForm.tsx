import { Fragment } from 'react';
import type { SchemaFormField, SchemaFormValues } from '../../api/types';
import './ServiceCredentialsSection.css';

/**
 * Schema-driven form fields shared by the admin Service Credentials cards
 * (ServiceCredentialsSection) and the per-user form-kind connections
 * (Data Connections). The backend describes a form as a list of
 * SchemaFormField rows (text / secret / bool / textarea / select); this
 * module renders them and holds the schema rules (visible_if, required_if).
 *
 * SchemaForm renders only the field rows: the parent owns the wrapper
 * (`.svc-cred-form`), any note above the fields and the submit / cancel
 * buttons, and keeps the values (controlled).
 */

export function secretPlaceholder(isSet: boolean, hint: string): string {
  return isSet ? 'Leave blank to keep the stored value' : hint;
}

export function isFieldVisible(field: SchemaFormField, values: SchemaFormValues): boolean {
  return !field.visible_if || values[field.visible_if] === true;
}

export function isFieldRequired(field: SchemaFormField, values: SchemaFormValues): boolean {
  if (field.required_if) return values[field.required_if] === true;
  return field.required;
}

/**
 * Initial editable values for `fields`: bool fields are booleans, secrets
 * always start empty (the server never returns them; empty = keep the
 * stored one where the route supports it), a required select with exactly
 * one option and no stored value starts on that option, every other field
 * takes its stored string or ''.
 */
export function initialSchemaValues(
  fields: readonly SchemaFormField[],
  stored: Record<string, unknown> = {},
): SchemaFormValues {
  const values: SchemaFormValues = {};
  for (const field of fields) {
    if (field.type === 'bool') {
      values[field.key] = stored[field.key] === true;
    } else if (field.type === 'secret') {
      values[field.key] = '';
    } else if (
      field.type === 'select' &&
      field.required &&
      !field.required_if &&
      (field.options ?? []).length === 1 &&
      !stored[field.key]
    ) {
      // A required select with a single choice has nothing to choose.
      values[field.key] = field.options![0];
    } else {
      const raw = stored[field.key];
      values[field.key] = typeof raw === 'string' ? raw : '';
    }
  }
  return values;
}

/**
 * Keys of visible, required, non-bool fields that are still empty. Secrets
 * are skipped unless `checkSecrets` is set: where an empty secret means
 * "keep the stored one" (admin cards, re-entering a connection) the server
 * decides; a brand-new connection has nothing stored, so it checks them.
 */
export function missingRequiredFields(
  fields: readonly SchemaFormField[],
  values: SchemaFormValues,
  { checkSecrets = false }: { checkSecrets?: boolean } = {},
): string[] {
  return fields
    .filter(
      (field) =>
        field.type !== 'bool' &&
        (checkSecrets || field.type !== 'secret') &&
        isFieldVisible(field, values) &&
        isFieldRequired(field, values) &&
        !String(values[field.key] ?? '').trim(),
    )
    .map((field) => field.key);
}

function FieldError({ id, error }: { id: string; error?: string }) {
  if (!error) return null;
  return (
    <div id={id} className="svc-cred-field-error">
      {error}
    </div>
  );
}

function FieldLabel({ id, label, optional }: { id: string; label: string; optional: boolean }) {
  return (
    <label htmlFor={id} className="svc-cred-label">
      {label} {optional && <span className="svc-cred-optional">(optional)</span>}
    </label>
  );
}

/** Optional plain-text guidance under the label, linked to the control via
 * aria-describedby (see describedByIds). Renders nothing when empty. */
function FieldHelp({ id, help }: { id: string; help?: string }) {
  if (!help) return null;
  return (
    <p id={id} className="svc-cred-help">
      {help}
    </p>
  );
}

/** The aria-describedby value for a control with optional help and error
 * paragraphs, or undefined when it has neither. */
function describedByIds(helpId: string, help: string | undefined, errorId: string, error: string | undefined) {
  const ids = [help ? helpId : null, error ? errorId : null].filter(Boolean);
  return ids.length ? ids.join(' ') : undefined;
}

/** One labelled single-line text / password input. */
export function CredentialField({
  id,
  label,
  value,
  onChange,
  placeholder,
  help,
  secret = false,
  optional = false,
  readOnly = false,
  disabled = false,
  error,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  help?: string;
  secret?: boolean;
  optional?: boolean;
  readOnly?: boolean;
  disabled?: boolean;
  error?: string;
}) {
  const errorId = `${id}-error`;
  const helpId = `${id}-help`;
  return (
    <div className="svc-cred-row">
      <FieldLabel id={id} label={label} optional={optional} />
      <FieldHelp id={helpId} help={help} />
      <input
        id={id}
        className="svc-cred-input"
        type={secret ? 'password' : 'text'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete={secret ? 'new-password' : 'off'}
        readOnly={readOnly || undefined}
        disabled={disabled || undefined}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedByIds(helpId, help, errorId, error)}
      />
      <FieldError id={errorId} error={error} />
    </div>
  );
}

export interface SchemaFormProps {
  /** The field schema, rendered in order. */
  fields: readonly SchemaFormField[];
  /** Current values keyed by field key (see initialSchemaValues). */
  values: SchemaFormValues;
  /** Called with the field key and its new value on every edit. */
  onChange: (key: string, value: string | boolean) => void;
  /** Prefix of the element ids: each control is `${idPrefix}-${key}`. */
  idPrefix: string;
  /** Secret keys that have a stored value: their placeholder says leaving
   * the field blank keeps it. */
  storedSecrets?: readonly string[];
  /** Keys shown but not editable (e.g. the account-determining fields when
   * re-entering a connection). Text fields become readOnly, select and
   * bool fields disabled. */
  readOnlyKeys?: readonly string[];
  /** Disable every field (e.g. while submitting). */
  disabled?: boolean;
  /** Per-field error messages (e.g. an `invalid_values` `fields` map),
   * shown under the field. */
  errors?: Record<string, string>;
}

/** The field rows of a schema-described form; see the module comment. */
export function SchemaForm({
  fields,
  values,
  onChange,
  idPrefix,
  storedSecrets = [],
  readOnlyKeys = [],
  disabled = false,
  errors = {},
}: SchemaFormProps) {
  return (
    <>
      {fields.map((field) => {
        if (!isFieldVisible(field, values)) return null;
        const id = `${idPrefix}-${field.key}`;
        const errorId = `${id}-error`;
        const helpId = `${id}-help`;
        const error = errors[field.key];
        const optional = !isFieldRequired(field, values);
        const readOnly = readOnlyKeys.includes(field.key);
        const describedBy = describedByIds(helpId, field.help, errorId, error);
        if (field.type === 'bool') {
          return (
            <Fragment key={field.key}>
              <label className="svc-cred-toggle">
                <input
                  id={id}
                  type="checkbox"
                  className="svc-cred-toggle-input"
                  checked={values[field.key] === true}
                  onChange={(e) => onChange(field.key, e.target.checked)}
                  disabled={disabled || readOnly || undefined}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={describedBy}
                />
                <span className="svc-cred-toggle-track" aria-hidden="true">
                  <span className="svc-cred-toggle-knob" />
                </span>
                <span className="svc-cred-toggle-text">{field.label}</span>
              </label>
              <FieldHelp id={helpId} help={field.help} />
              <FieldError id={errorId} error={error} />
            </Fragment>
          );
        }
        if (field.type === 'textarea') {
          return (
            <div key={field.key} className="svc-cred-row">
              <FieldLabel id={id} label={field.label} optional={optional} />
              <FieldHelp id={helpId} help={field.help} />
              <textarea
                id={id}
                className="svc-cred-textarea"
                value={String(values[field.key] ?? '')}
                onChange={(e) => onChange(field.key, e.target.value)}
                placeholder={field.placeholder}
                rows={3}
                spellCheck={false}
                readOnly={readOnly || undefined}
                disabled={disabled || undefined}
                aria-invalid={error ? true : undefined}
                aria-describedby={describedBy}
              />
              <FieldError id={errorId} error={error} />
            </div>
          );
        }
        if (field.type === 'select') {
          const value = String(values[field.key] ?? '');
          const options = field.options ?? [];
          return (
            <div key={field.key} className="svc-cred-row">
              <FieldLabel id={id} label={field.label} optional={optional} />
              <FieldHelp id={helpId} help={field.help} />
              <select
                id={id}
                className="svc-cred-input svc-cred-select"
                value={value}
                onChange={(e) => onChange(field.key, e.target.value)}
                disabled={disabled || readOnly || undefined}
                aria-invalid={error ? true : undefined}
                aria-describedby={describedBy}
              >
                {optional ? (
                  <option value="">{field.placeholder || 'None'}</option>
                ) : (
                  !value && (
                    <option value="" disabled>
                      {field.placeholder || 'Choose...'}
                    </option>
                  )
                )}
                {/* A stored value no longer offered stays visible as-is. */}
                {value && !options.includes(value) && <option value={value}>{value}</option>}
                {options.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
              <FieldError id={errorId} error={error} />
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
            onChange={(value) => onChange(field.key, value)}
            placeholder={
              secret
                ? secretPlaceholder(storedSecrets.includes(field.key), field.placeholder)
                : field.placeholder
            }
            help={field.help}
            secret={secret}
            optional={optional}
            readOnly={readOnly}
            disabled={disabled}
            error={error}
          />
        );
      })}
    </>
  );
}
