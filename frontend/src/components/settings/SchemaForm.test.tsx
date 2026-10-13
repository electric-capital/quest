// SchemaForm: the schema-driven field rows shared by the admin Service
// Credentials cards and the per-user form-kind connections -- every field
// type incl. select, read-only keys, per-field errors and the value helpers.
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import {
  SchemaForm,
  entrySummary,
  initialSchemaValues,
  missingRequiredFields,
} from './SchemaForm';
import type { SchemaFormField, SchemaFormValue, SchemaFormValues } from '../../api/types';

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
  onChangeSpy?: (key: string, value: SchemaFormValue) => void;
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

  it('renders help text under the label and links it to the control', () => {
    const fields: SchemaFormField[] = [
      { key: 'hosts', label: 'Allowed servers', type: 'textarea', placeholder: '', required: true, help: 'One server per line.' },
      { key: 'name', label: 'Name', type: 'text', placeholder: '', required: false, help: 'Shown to the assistant.' },
      { key: 'kind', label: 'Kind', type: 'select', placeholder: '', required: false, options: ['a'], help: 'Pick one.' },
      { key: 'on', label: 'On', type: 'bool', placeholder: '', required: false, help: 'Toggle it.' },
      { key: 'plain', label: 'Plain', type: 'text', placeholder: '', required: false },
    ];
    const Local = () => {
      const [values, setValues] = useState<SchemaFormValues>(() => initialSchemaValues(fields));
      return (
        <SchemaForm
          fields={fields}
          values={values}
          onChange={(k, v) => setValues((prev) => ({ ...prev, [k]: v }))}
          idPrefix="h"
          errors={{ hosts: 'Bad line' }}
        />
      );
    };
    render(<Local />);
    const hosts = screen.getByLabelText('Allowed servers');
    expect(screen.getByText('One server per line.').getAttribute('id')).toBe('h-hosts-help');
    // Help and error both describe the control.
    expect(hosts.getAttribute('aria-describedby')).toBe('h-hosts-help h-hosts-error');
    expect(screen.getByLabelText(/^Name/).getAttribute('aria-describedby')).toBe('h-name-help');
    expect(screen.getByLabelText(/^Kind/).getAttribute('aria-describedby')).toBe('h-kind-help');
    expect(screen.getByLabelText('On').getAttribute('aria-describedby')).toBe('h-on-help');
    expect(screen.getByText('Toggle it.')).toBeTruthy();
    // No help: no paragraph and no describedby.
    expect(screen.getByLabelText(/^Plain/).getAttribute('aria-describedby')).toBeNull();
    expect(document.querySelectorAll('.svc-cred-help')).toHaveLength(4);
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

// A list field: entries of a host / mode / port / note sub-form, capped
// at three, as the IMAP admin card uses it.
const SERVERS: SchemaFormField = {
  key: 'servers',
  label: 'Servers',
  type: 'list',
  placeholder: '',
  help: 'Where users may connect.',
  required: true,
  item_label: 'server',
  max_items: 3,
  item_fields: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'imap.example.com', required: true },
    { key: 'mode', label: 'Mode', type: 'select', placeholder: '', required: true, options: ['ssl', 'starttls'], default: 'ssl' },
    { key: 'port', label: 'Port', type: 'text', placeholder: 'Default', required: false },
    { key: 'pinned', label: 'Pinned', type: 'bool', placeholder: '', required: false },
  ],
};
const LIST_FIELDS: SchemaFormField[] = [
  { key: 'title', label: 'Title', type: 'text', placeholder: '', required: false, default: 'Mail' },
  SERVERS,
];

function ListHarness({ initial, disabled }: { initial?: SchemaFormValues; disabled?: boolean }) {
  const [values, setValues] = useState<SchemaFormValues>(
    () => initial ?? initialSchemaValues(LIST_FIELDS),
  );
  return (
    <div>
      <SchemaForm
        fields={LIST_FIELDS}
        values={values}
        idPrefix="l"
        onChange={(key, value) => setValues((prev) => ({ ...prev, [key]: value }))}
        disabled={disabled}
      />
      <output data-testid="values">{JSON.stringify(values)}</output>
    </div>
  );
}

const STORED_SERVERS = [
  { host: 'imap.a.com', mode: 'ssl', port: '993', pinned: false },
  { host: 'imap.b.com', mode: 'starttls', port: '', pinned: true },
];

describe('SchemaForm list fields', () => {
  it('builds initial values from stored entries and schema defaults', () => {
    expect(initialSchemaValues(LIST_FIELDS, { servers: STORED_SERVERS })).toEqual({
      title: 'Mail',
      servers: [
        { host: 'imap.a.com', mode: 'ssl', port: '993', pinned: false },
        { host: 'imap.b.com', mode: 'starttls', port: '', pinned: true },
      ],
    });
    expect(initialSchemaValues(LIST_FIELDS, { title: 'Set', servers: 'junk' })).toEqual({
      title: 'Set',
      servers: [],
    });
  });

  it('treats an empty required list as missing', () => {
    expect(missingRequiredFields(LIST_FIELDS, { title: '', servers: [] })).toEqual(['servers']);
    expect(missingRequiredFields(LIST_FIELDS, { title: '', servers: STORED_SERVERS })).toEqual([]);
  });

  it('summarises an entry as title plus details', () => {
    expect(entrySummary(SERVERS, STORED_SERVERS[1])).toEqual({
      title: 'imap.b.com',
      details: ['Mode: starttls', 'Pinned'],
    });
    expect(entrySummary(SERVERS, { host: '', mode: '', port: '', pinned: false })).toEqual({
      title: '',
      details: [],
    });
  });

  it('lists stored entries with Edit / Remove and the empty state', () => {
    render(<ListHarness initial={{ title: '', servers: STORED_SERVERS }} />);
    expect(screen.getByText('Where users may connect.')).toBeTruthy();
    expect(screen.getByText('imap.a.com')).toBeTruthy();
    expect(screen.getByText('Mode: starttls')).toBeTruthy();
    expect(screen.getByText('Pinned')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Edit imap.a.com' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Remove imap.a.com' }));
    expect(currentValues().servers).toEqual([STORED_SERVERS[1]]);
    fireEvent.click(screen.getByRole('button', { name: 'Remove imap.b.com' }));
    expect(currentValues().servers).toEqual([]);
    expect(screen.getByText('No servers yet.')).toBeTruthy();
  });

  it('adds an entry through the inline editor with the schema defaults', () => {
    render(<ListHarness />);
    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }));
    // The add button hides while the editor is open.
    expect(screen.queryByRole('button', { name: '+ Add server' })).toBeNull();
    const done = screen.getByRole('button', { name: 'Done' }) as HTMLButtonElement;
    expect(done.disabled).toBe(true); // host required
    const mode = screen.getByLabelText('Mode') as HTMLSelectElement;
    expect(mode.value).toBe('ssl'); // schema default
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'imap.c.com' } });
    expect(done.disabled).toBe(false);
    fireEvent.change(screen.getByLabelText(/Port/), { target: { value: '1993' } });
    fireEvent.click(done);
    expect(currentValues().servers).toEqual([
      { host: 'imap.c.com', mode: 'ssl', port: '1993', pinned: false },
    ]);
    expect(screen.queryByTestId('l-servers-new-editor')).toBeNull();
    expect(screen.getByRole('button', { name: '+ Add server' })).toBeTruthy();
  });

  it('edits an entry in place and Cancel discards the draft', () => {
    render(<ListHarness initial={{ title: '', servers: STORED_SERVERS }} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit imap.b.com' }));
    const host = screen.getByLabelText('Host') as HTMLInputElement;
    expect(host.value).toBe('imap.b.com');
    // Other rows' buttons wait while an editor is open.
    expect((screen.getByRole('button', { name: 'Edit imap.a.com' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(host, { target: { value: 'imap.x.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(currentValues().servers).toEqual(STORED_SERVERS);
    expect(screen.getByText('imap.b.com')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Edit imap.b.com' }));
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'imap.y.com' } });
    fireEvent.click(screen.getByLabelText('Pinned'));
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(currentValues().servers).toEqual([
      STORED_SERVERS[0],
      { host: 'imap.y.com', mode: 'starttls', port: '', pinned: false },
    ]);
  });

  it('stops adding at max_items', () => {
    render(
      <ListHarness
        initial={{ title: '', servers: [...STORED_SERVERS, { host: 'imap.c.com', mode: 'ssl', port: '', pinned: false }] }}
      />,
    );
    expect(screen.queryByRole('button', { name: '+ Add server' })).toBeNull();
    expect(screen.getByText('At most 3 servers.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Remove imap.c.com' }));
    expect(screen.getByRole('button', { name: '+ Add server' })).toBeTruthy();
  });

  it('hides the entry controls while disabled', () => {
    render(<ListHarness initial={{ title: '', servers: STORED_SERVERS }} disabled />);
    expect(screen.getByText('imap.a.com')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Edit|Remove|Add server/ })).toBeNull();
  });
});
