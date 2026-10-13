import { Fragment, useState } from 'react';
import type {
  SchemaFormField,
  SchemaFormItem,
  SchemaFormValue,
  SchemaFormValues,
} from '../../api/types';
import './ServiceCredentialsSection.css';

/**
 * Schema-driven form fields shared by the admin Service Credentials cards
 * (ServiceCredentialsSection) and the per-user form-kind connections
 * (Data Connections). The backend describes a form as a list of
 * SchemaFormField rows (text / secret / bool / textarea / select / list);
 * this module renders them and holds the schema rules (visible_if,
 * required_if, defaults).
 *
 * A `list` field holds zero or more entries, each a sub-form described by
 * the field's `item_fields`. ListField renders the entries as rows with
 * per-entry Edit / Remove and an "Add <item_label>" button; adding or
 * editing opens the entry sub-form inline (a nested SchemaForm) with
 * Done / Cancel, and the field's value is the whole entry array.
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
 * one option and no stored value starts on that option, a list field takes
 * its stored entries (each through the same rules over `item_fields`),
 * every other field takes its stored string, else its schema `default`,
 * else ''.
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
    } else if (field.type === 'list') {
      const raw = stored[field.key];
      values[field.key] = Array.isArray(raw)
        ? raw
            .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
            .map((item) => initialSchemaValues(field.item_fields ?? [], item) as SchemaFormItem)
        : [];
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
      values[field.key] = typeof raw === 'string' ? raw : (field.default ?? '');
    }
  }
  return values;
}

/** The entries of a list field's value ([] for anything else). */
export function listEntries(value: SchemaFormValue | undefined): SchemaFormItem[] {
  return Array.isArray(value) ? value : [];
}

/**
 * One list entry condensed for a row: the first visible non-bool field's
 * value as the title and the other visible fields as "Label: value" details
 * (bool fields only when on, as their label). Used by the ListField rows
 * and by the configured-service summary.
 */
export function entrySummary(
  field: SchemaFormField,
  item: SchemaFormItem,
): { title: string; details: string[] } {
  let title = '';
  const details: string[] = [];
  for (const itemField of field.item_fields ?? []) {
    if (!isFieldVisible(itemField, item)) continue;
    const value = item[itemField.key];
    if (itemField.type === 'bool') {
      if (value === true) details.push(itemField.label);
      continue;
    }
    const text = typeof value === 'string' ? value.trim() : '';
    if (!title && text) {
      title = text;
    } else if (text) {
      details.push(`${itemField.label}: ${text}`);
    }
  }
  return { title, details };
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
    .filter((field) => {
      if (field.type === 'bool') return false;
      if (!checkSecrets && field.type === 'secret') return false;
      if (!isFieldVisible(field, values) || !isFieldRequired(field, values)) return false;
      // A required list needs at least one entry.
      if (field.type === 'list') return listEntries(values[field.key]).length === 0;
      return !String(values[field.key] ?? '').trim();
    })
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

/** The inline sub-form for one entry of a list field (add or edit). */
function ListEntryEditor({
  field,
  idPrefix,
  initial,
  onDone,
  onCancel,
}: {
  field: SchemaFormField;
  idPrefix: string;
  initial: SchemaFormItem;
  onDone: (item: SchemaFormItem) => void;
  onCancel: () => void;
}) {
  const itemFields = field.item_fields ?? [];
  const [draft, setDraft] = useState<SchemaFormItem>(initial);
  // Entries have no secrets, so every required field is checked here.
  const incomplete = missingRequiredFields(itemFields, draft, { checkSecrets: true }).length > 0;
  return (
    <div className="svc-cred-list-editor" data-testid={`${idPrefix}-editor`}>
      <SchemaForm
        fields={itemFields}
        values={draft}
        idPrefix={idPrefix}
        onChange={(key, value) =>
          setDraft((prev) => ({ ...prev, [key]: value as string | boolean }))
        }
      />
      <div className="svc-cred-list-editor-actions">
        <button
          type="button"
          className="svc-cred-list-done"
          onClick={() => onDone(draft)}
          disabled={incomplete}
        >
          Done
        </button>
        <button type="button" className="svc-cred-cancel" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/**
 * A list field: its entries as rows (title + details, Edit / Remove), an
 * "Add <item_label>" button, and the inline entry editor while adding or
 * editing. Reports the whole entry array on every change.
 */
function ListField({
  field,
  id,
  entries,
  onChange,
  optional,
  readOnly,
  disabled,
  error,
}: {
  field: SchemaFormField;
  id: string;
  entries: SchemaFormItem[];
  onChange: (entries: SchemaFormItem[]) => void;
  optional: boolean;
  readOnly: boolean;
  disabled: boolean;
  error?: string;
}) {
  // Which entry's editor is open: an index for an existing entry, 'new'
  // for the add form, null for none.
  const [editing, setEditing] = useState<number | 'new' | null>(null);
  const noun = field.item_label || 'entry';
  const locked = disabled || readOnly;
  const atCap = !!field.max_items && entries.length >= field.max_items;
  const errorId = `${id}-error`;
  const helpId = `${id}-help`;
  const itemFields = field.item_fields ?? [];

  const commit = (index: number | 'new', item: SchemaFormItem) => {
    const next = index === 'new' ? [...entries, item] : entries.map((e, i) => (i === index ? item : e));
    onChange(next);
    setEditing(null);
  };
  const remove = (index: number) => {
    onChange(entries.filter((_, i) => i !== index));
    if (editing === index) setEditing(null);
  };

  return (
    <div className="svc-cred-row svc-cred-list" role="group" aria-labelledby={`${id}-label`}>
      <div id={`${id}-label`} className="svc-cred-label">
        {field.label} {optional && <span className="svc-cred-optional">(optional)</span>}
      </div>
      <FieldHelp id={helpId} help={field.help} />
      {entries.length === 0 && editing !== 'new' && (
        <p className="svc-cred-list-empty">No {noun}s yet.</p>
      )}
      {entries.length > 0 && (
        <ul className="svc-cred-list-items">
          {entries.map((item, index) => {
            const summary = entrySummary(field, item);
            const rowLabel = summary.title || `${noun} ${index + 1}`;
            return (
              <li key={index} className="svc-cred-list-item">
                {editing === index ? (
                  <ListEntryEditor
                    field={field}
                    idPrefix={`${id}-${index}`}
                    initial={item}
                    onDone={(next) => commit(index, next)}
                    onCancel={() => setEditing(null)}
                  />
                ) : (
                  <div className="svc-cred-list-item-row">
                    <div className="svc-cred-list-item-text">
                      <span className="svc-cred-list-item-title">
                        {summary.title || <span className="svc-cred-summary-empty">{rowLabel}</span>}
                      </span>
                      {summary.details.length > 0 && (
                        <span className="svc-cred-list-item-details">
                          {summary.details.map((detail) => (
                            <span key={detail} className="svc-cred-list-item-detail">
                              {detail}
                            </span>
                          ))}
                        </span>
                      )}
                    </div>
                    {!locked && (
                      <div className="svc-cred-list-item-actions">
                        <button
                          type="button"
                          className="svc-cred-list-action"
                          onClick={() => setEditing(index)}
                          disabled={editing !== null}
                          aria-label={`Edit ${rowLabel}`}
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          className="svc-cred-list-action svc-cred-list-remove"
                          onClick={() => remove(index)}
                          disabled={editing !== null}
                          aria-label={`Remove ${rowLabel}`}
                        >
                          Remove
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {editing === 'new' && (
        <ListEntryEditor
          field={field}
          idPrefix={`${id}-new`}
          initial={initialSchemaValues(itemFields) as SchemaFormItem}
          onDone={(item) => commit('new', item)}
          onCancel={() => setEditing(null)}
        />
      )}
      {!locked && editing === null && !atCap && (
        <button
          type="button"
          className="svc-cred-list-add"
          onClick={() => setEditing('new')}
          aria-describedby={describedByIds(helpId, field.help, errorId, error)}
        >
          + Add {noun}
        </button>
      )}
      {atCap && editing === null && (
        <p className="svc-cred-help">At most {field.max_items} {noun}s.</p>
      )}
      <FieldError id={errorId} error={error} />
    </div>
  );
}

export interface SchemaFormProps {
  /** The field schema, rendered in order. */
  fields: readonly SchemaFormField[];
  /** Current values keyed by field key (see initialSchemaValues). */
  values: SchemaFormValues;
  /** Called with the field key and its new value on every edit (a list
   * field reports its whole entry array). */
  onChange: (key: string, value: SchemaFormValue) => void;
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
        if (field.type === 'list') {
          return (
            <ListField
              key={field.key}
              field={field}
              id={id}
              entries={listEntries(values[field.key])}
              onChange={(entries) => onChange(field.key, entries)}
              optional={optional}
              readOnly={readOnly}
              disabled={disabled}
              error={error}
            />
          );
        }
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
