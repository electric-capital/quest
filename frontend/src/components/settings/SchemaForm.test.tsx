// SchemaForm: the schema-driven field rows shared by the admin Service
// Credentials cards and the per-user form-kind connections -- every field
// type incl. select, read-only keys, per-field errors and the value helpers.
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import {
  SchemaForm,
  initialSchemaValues,
  missingRequiredFields,
} from './SchemaForm';
import type { SchemaFormField, SchemaFormValues } from '../../api/types';

const FIELDS: SchemaFormField[] = [
  { key: 'host', label: 'IMAP server', type: 'select', placeholder: '', required: true, options: ['imap.a.com', 'imap.b.com'] },
  { key: 'folder', label: 'Folder', type: 'select', placeholder: '', required: false, options: ['INBOX'] },
  { key: 'username', label: 'Username', type: 'text', placeholder: 'you@example.com', required: true },
  { key: 'password', label: 'Password', type: 'secret', placeholder: 'App password', required: true },
  { key: 'notes', label: 'Notes', type: 'textarea', placeholder: 'Anything', required: false },
  { key: 'use_proxy', label: 'Use proxy', type: 'bool', placeholder: '', required: false },
  { key: 'proxy', label: 'Proxy', type: 'text', placeholder: '', required: false, required_if: 'use_proxy', visible_if: 'use_proxy' },
];

function Harness({
  initial,
  onChangeSpy,
  ...rest
}: {
  initial?: SchemaFormValues;
  onChangeSpy?: (key: string, value: string | boolean) => void;
  readOnlyKeys?: string[];
  storedSecrets?: string[];
  errors?: Record<string, string>;
  disabled?: boolean;
}) {
  const [values, setValues] = useState<SchemaFormValues>(
    () => initial ?? initialSchemaValues(FIELDS),
  );
  return (
    <div>
      <SchemaForm
        fields={FIELDS}
        values={values}
        idPrefix="t"
        onChange={(key, value) => {
          onChangeSpy?.(key, value);
          setValues((prev) => ({ ...prev, [key]: value }));
        }}
        {...rest}
      />
      <output data-testid="values">{JSON.stringify(values)}</output>
    </div>
  );
}

function currentValues(): SchemaFormValues {
  return JSON.parse(screen.getByTestId('values').textContent ?? '{}');
}

afterEach(() => cleanup());

describe('SchemaForm', () => {
  it('renders each field type with its label and placeholder', () => {
    render(<Harness />);
    const host = screen.getByLabelText('IMAP server') as HTMLSelectElement;
    expect(host.tagName).toBe('SELECT');
    // Required select with no value: a disabled "Choose..." placeholder first.
    expect([...host.options].map((o) => o.textContent)).toEqual(['Choose...', 'imap.a.com', 'imap.b.com']);
    expect(host.options[0].disabled).toBe(true);
    expect(host.value).toBe('');

    // Optional select: an always-selectable empty "None" option.
    const folder = screen.getByLabelText(/Folder/) as HTMLSelectElement;
    expect([...folder.options].map((o) => o.textContent)).toEqual(['None', 'INBOX']);
    expect(screen.getByText('Folder').parentElement?.textContent).toContain('(optional)');

    const user = screen.getByLabelText('Username') as HTMLInputElement;
    expect(user.type).toBe('text');
    expect(user.placeholder).toBe('you@example.com');
    const pw = screen.getByLabelText('Password') as HTMLInputElement;
    expect(pw.type).toBe('password');
    expect(pw.placeholder).toBe('App password');
    expect(pw.autocomplete).toBe('new-password');
    expect((screen.getByLabelText(/Notes/) as HTMLElement).tagName).toBe('TEXTAREA');
    expect((screen.getByLabelText('Use proxy') as HTMLInputElement).type).toBe('checkbox');
    // visible_if: hidden while the toggle is off.
    expect(screen.queryByLabelText(/Proxy/)).toBeNull();
  });

  it('reports value changes for every field type', () => {
    const spy = vi.fn();
    render(<Harness onChangeSpy={spy} />);
    fireEvent.change(screen.getByLabelText('IMAP server'), { target: { value: 'imap.b.com' } });
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'me@b.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.change(screen.getByLabelText(/Notes/), { target: { value: 'hi' } });
    fireEvent.click(screen.getByLabelText('Use proxy'));
    expect(spy).toHaveBeenCalledWith('host', 'imap.b.com');
    expect(spy).toHaveBeenCalledWith('use_proxy', true);
    expect(currentValues()).toMatchObject({
      host: 'imap.b.com', username: 'me@b.com', password: 'pw', notes: 'hi', use_proxy: true,
    });
    // required_if + visible_if: the proxy field appears, now required.
    const proxy = screen.getByLabelText('Proxy');
    expect(proxy.parentElement?.textContent).not.toContain('(optional)');
    // The placeholder option drops once a value is chosen.
    const host = screen.getByLabelText('IMAP server') as HTMLSelectElement;
    expect([...host.options].map((o) => o.value)).toEqual(['imap.a.com', 'imap.b.com']);
  });

  it('keeps a stored select value that is no longer offered', () => {
    render(<Harness initial={{ ...initialSchemaValues(FIELDS), host: 'imap.old.com' }} />);
    const host = screen.getByLabelText('IMAP server') as HTMLSelectElement;
    expect(host.value).toBe('imap.old.com');
  });

  it('makes readOnlyKeys non-editable', () => {
    render(
      <Harness
        initial={{ ...initialSchemaValues(FIELDS), host: 'imap.a.com', username: 'me@a.com' }}
        readOnlyKeys={['host', 'username', 'use_proxy']}
      />,
    );
    expect((screen.getByLabelText('IMAP server') as HTMLSelectElement).disabled).toBe(true);
    const user = screen.getByLabelText('Username') as HTMLInputElement;
    expect(user.readOnly).toBe(true);
    expect(user.value).toBe('me@a.com');
    expect((screen.getByLabelText('Use proxy') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText('Password') as HTMLInputElement).readOnly).toBe(false);
  });

  it('disables every field while disabled', () => {
    render(<Harness disabled />);
    for (const label of ['IMAP server', 'Username', 'Password', 'Use proxy']) {
      expect((screen.getByLabelText(label) as HTMLInputElement).disabled).toBe(true);
    }
    expect((screen.getByLabelText(/Notes/) as HTMLTextAreaElement).disabled).toBe(true);
  });

  it('shows per-field errors linked to their control', () => {
    render(<Harness errors={{ host: 'Not one of the allowed choices.', password: 'This field is required.' }} />);
    const host = screen.getByLabelText('IMAP server');
    expect(host.getAttribute('aria-invalid')).toBe('true');
    const errorId = host.getAttribute('aria-describedby');
    expect(errorId).toBe('t-host-error');
    expect(document.getElementById(errorId!)?.textContent).toBe('Not one of the allowed choices.');
    expect(screen.getByLabelText('Password').getAttribute('aria-describedby')).toBe('t-password-error');
    expect(screen.getByLabelText('Username').getAttribute('aria-invalid')).toBeNull();
    // Field errors are announced through aria-describedby, not as alerts
    // (the form-level summary owns role="alert").
    expect(screen.queryAllByRole('alert')).toHaveLength(0);
  });

  it('uses the keep-stored placeholder for stored secrets', () => {
    render(<Harness storedSecrets={['password']} />);
    expect((screen.getByLabelText('Password') as HTMLInputElement).placeholder).toBe(
      'Leave blank to keep the stored value',
    );
  });
});

describe('schema helpers', () => {
  it('builds initial values from stored data', () => {
    expect(
      initialSchemaValues(FIELDS, { host: 'imap.a.com', password: 'x', use_proxy: true, notes: 3 }),
    ).toEqual({
      host: 'imap.a.com', folder: '', username: '', password: '', notes: '', use_proxy: true, proxy: '',
    });
  });

  it('pre-fills a required select with exactly one option', () => {
    const fields: SchemaFormField[] = [
      { key: 'host', label: 'Host', type: 'select', placeholder: '', required: true, options: ['imap.only.com'] },
      { key: 'folder', label: 'Folder', type: 'select', placeholder: '', required: false, options: ['INBOX'] },
      { key: 'port', label: 'Port', type: 'select', placeholder: '', required: true, options: ['993', '143'] },
    ];
    expect(initialSchemaValues(fields)).toEqual({ host: 'imap.only.com', folder: '', port: '' });
    // A stored value wins over the single option.
    expect(initialSchemaValues(fields, { host: 'imap.old.com' }).host).toBe('imap.old.com');
  });

  it('flags an empty required secret only with checkSecrets', () => {
    const fields: SchemaFormField[] = [
      { key: 'password', label: 'Password', type: 'secret', placeholder: '', required: true },
    ];
    expect(missingRequiredFields(fields, { password: '' })).toEqual([]);
    expect(missingRequiredFields(fields, { password: '' }, { checkSecrets: true })).toEqual(['password']);
    expect(missingRequiredFields(fields, { password: ' ' }, { checkSecrets: true })).toEqual(['password']);
    expect(missingRequiredFields(fields, { password: 'x' }, { checkSecrets: true })).toEqual([]);
  });

  it('lists missing required fields, secrets only on request', () => {
    const values = initialSchemaValues(FIELDS);
    expect(missingRequiredFields(FIELDS, values)).toEqual(['host', 'username']);
    expect(missingRequiredFields(FIELDS, values, { checkSecrets: true })).toEqual([
      'host', 'username', 'password',
    ]);
    expect(missingRequiredFields(FIELDS, { ...values, use_proxy: true, host: 'h', username: ' u ' })).toEqual([
      'proxy',
    ]);
  });
});
