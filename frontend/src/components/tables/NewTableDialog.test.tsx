// NewTableDialog: the POST /tables body for a user table and for a project
// table (no mode sent; the project's mode is named under the select), Create
// blocked while the draft or the title has a problem -- with the reason
// beside it ("Add a title ...", or "N problems to fix", which reveals the
// first one) and the title measured trimmed in code points -- then the
// refusals at their field -- duplicate_title at the title, a column spec
// error at its column, table_limit in a banner -- and navigation to the new
// table.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { Project, TableDetail } from '../../api/types';
import { ADD_TITLE_HINT, NewTableDialog } from './NewTableDialog';

const mocks = vi.hoisted(() => ({ createTable: vi.fn() }));

vi.mock('../../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/tablesApi')>()),
  createTable: mocks.createTable,
}));

function project(id: string, name: string, overrides: Partial<Project> = {}): Project {
  return {
    id, user_id: 1, name, guide: '', public: false, archived: false,
    created_at: '2026-10-01T00:00:00', updated_at: null, conversation_count: 0, ...overrides,
  };
}

const PROJECTS = [
  project('p1', 'Sales', { public: true }),
  project('p2', 'Old stuff', { archived: true }),
  project('p3', 'Research'),
];

function created(id: string): TableDetail {
  return { id } as TableDetail;
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

const onClose = vi.fn();

function renderDialog(initialProjectId: string | null = null) {
  return render(
    <MemoryRouter initialEntries={['/tables']}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <NewTableDialog isOpen onClose={onClose} projects={PROJECTS} initialProjectId={initialProjectId} />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function dialog() {
  return screen.getByRole('dialog', { name: 'New Table' });
}

function card(name: string): HTMLElement {
  return within(dialog())
    .getByRole('button', { name: new RegExp(`(Expand|Collapse) ${name}$`) })
    .closest('li') as HTMLElement;
}

function createButton() {
  return within(dialog()).getByRole('button', { name: 'Create Table' }) as HTMLButtonElement;
}

function typeTitle(value: string) {
  fireEvent.change(within(dialog()).getByLabelText('Title'), { target: { value } });
}

function apiError(message: string, status: number, code: string, extra: Record<string, unknown> = {}) {
  return new ApiClientError(message, status, code, undefined, { detail: { error: code, message, ...extra } });
}

beforeEach(() => {
  mocks.createTable.mockReset();
  onClose.mockReset();
});

afterEach(() => cleanup());

describe('NewTableDialog', () => {
  it('builds the user-table request from the title, description and columns, then opens the table', async () => {
    mocks.createTable.mockResolvedValue(created('t9'));
    renderDialog();
    expect((within(dialog()).getByLabelText('Location') as HTMLSelectElement).value).toBe('');
    // Archived projects are not offered.
    expect(within(dialog()).queryByRole('option', { name: /Old stuff/ })).toBeNull();
    typeTitle('  Leads ');
    fireEvent.change(within(dialog()).getByPlaceholderText('What this table is for'), {
      target: { value: 'Sales pipeline' },
    });

    // The starter column, renamed and required (no default needed: no rows yet).
    fireEvent.change(within(card('Name')).getByLabelText('Name'), { target: { value: 'Company' } });
    fireEvent.click(within(card('Company')).getByLabelText('Required'));

    // A choice column with two options and a default.
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Add column' }));
    fireEvent.change(within(card('untitled column')).getByLabelText('Name'), { target: { value: 'Status' } });
    fireEvent.change(within(card('Status')).getByLabelText('Type'), { target: { value: 'enum' } });
    fireEvent.change(within(card('Status')).getByLabelText('Option 1'), { target: { value: 'open' } });
    fireEvent.click(within(card('Status')).getByRole('button', { name: 'Add option' }));
    fireEvent.change(within(card('Status')).getByLabelText('Option 2'), { target: { value: 'won' } });
    const defaultSelect = within(card('Status')).getByLabelText(/^Default/) as HTMLSelectElement;
    const won = within(defaultSelect).getByRole('option', { name: 'won' }) as HTMLOptionElement;
    fireEvent.change(defaultSelect, { target: { value: won.value } });

    expect(createButton().disabled).toBe(false);
    fireEvent.click(createButton());
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/tables/t9'));
    expect(mocks.createTable).toHaveBeenCalledTimes(1);
    expect(mocks.createTable).toHaveBeenCalledWith({
      title: 'Leads',
      description: 'Sales pipeline',
      columns: [
        { name: 'Company', type: 'text', required: true },
        { name: 'Status', type: 'enum', options: ['open', 'won'], default: 'won' },
      ],
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a project table sends project_id (no mode) and names the mode it takes', async () => {
    mocks.createTable.mockResolvedValue(created('t10'));
    renderDialog('p1');
    const location = within(dialog()).getByLabelText('Location') as HTMLSelectElement;
    expect(location.value).toBe('p1');
    expect(within(dialog()).getByText('Public')).toBeTruthy();
    typeTitle('Accounts');
    fireEvent.click(createButton());
    await waitFor(() => expect(mocks.createTable).toHaveBeenCalledTimes(1));
    expect(mocks.createTable.mock.calls[0][0]).toEqual({
      title: 'Accounts',
      project_id: 'p1',
      columns: [{ name: 'Name', type: 'text' }],
    });
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/tables/t10'));
  });

  it('switching the location to "Your tables" drops project_id', async () => {
    mocks.createTable.mockResolvedValue(created('t11'));
    renderDialog('p3');
    fireEvent.change(within(dialog()).getByLabelText('Location'), { target: { value: '' } });
    expect(within(dialog()).queryByText(/project's mode/)).toBeNull();
    typeTitle('Mine');
    fireEvent.click(createButton());
    await waitFor(() => expect(mocks.createTable).toHaveBeenCalledTimes(1));
    expect(mocks.createTable.mock.calls[0][0]).not.toHaveProperty('project_id');
  });

  it('Create stays disabled while the title or a column has a problem', () => {
    renderDialog();
    expect(createButton().disabled).toBe(true);
    typeTitle('Leads');
    expect(createButton().disabled).toBe(false);
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Add column' }));
    fireEvent.change(within(card('untitled column')).getByLabelText('Name'), { target: { value: 'name' } });
    expect(within(card('name')).getByText('Another column has this name (names ignore case).')).toBeTruthy();
    expect(createButton().disabled).toBe(true);
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Delete name' }));
    typeTitle('   ');
    expect(within(dialog()).getByText('Enter a title.')).toBeTruthy();
    expect(createButton().disabled).toBe(true);
  });

  it('says why Create is disabled: a blank title, then the problems, which a click reveals', () => {
    renderDialog();
    const hint = within(dialog()).getByText(ADD_TITLE_HINT);
    expect(createButton().getAttribute('aria-describedby')).toBe(hint.id);
    typeTitle('Leads');
    expect(within(dialog()).queryByText(ADD_TITLE_HINT)).toBeNull();
    expect(createButton().getAttribute('aria-describedby')).toBeNull();
    // Two columns named alike, the first card collapsed.
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Add column' }));
    fireEvent.change(within(card('untitled column')).getByLabelText('Name'), { target: { value: 'name' } });
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Collapse Name' }));
    expect(within(card('Name')).getByText('Has problems')).toBeTruthy();
    fireEvent.click(within(dialog()).getByRole('button', { name: '2 problems to fix' }));
    expect(within(dialog()).getByRole('button', { name: 'Collapse Name' })).toBeTruthy();
    expect(document.activeElement).toBe(within(card('Name')).getByLabelText('Name'));
  });

  it('a title problem is revealed at the title', () => {
    renderDialog();
    typeTitle('   ');
    expect(within(dialog()).getByText(ADD_TITLE_HINT)).toBeTruthy();
    typeTitle('x'.repeat(201));
    fireEvent.click(within(dialog()).getByRole('button', { name: '1 problem to fix' }));
    expect(document.activeElement).toBe(within(dialog()).getByLabelText('Title'));
  });

  it('measures the title trimmed and in code points, as the server does', () => {
    renderDialog();
    const title = within(dialog()).getByLabelText('Title') as HTMLInputElement;
    expect(title.maxLength).toBe(-1);
    // 200 emoji are 400 UTF-16 units, but 200 characters to the server.
    typeTitle(`  ${'\u{1F600}'.repeat(200)}  `);
    expect(createButton().disabled).toBe(false);
    typeTitle('\u{1F600}'.repeat(201));
    expect(within(dialog()).getByText('Use at most 200 characters.')).toBeTruthy();
    expect(createButton().disabled).toBe(true);
  });

  it('duplicate_title shows at the title and keeps the dialog open', async () => {
    mocks.createTable.mockRejectedValue(
      apiError('You already have a table named "Leads".', 409, 'duplicate_title'),
    );
    renderDialog();
    typeTitle('Leads');
    fireEvent.click(createButton());
    const message = await within(dialog()).findByText('You already have a table named "Leads".');
    expect(message.id).toBe('new-table-title-error');
    expect(within(dialog()).getByLabelText('Title').getAttribute('aria-invalid')).toBe('true');
    expect(within(dialog()).queryByRole('alert')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByTestId('location').textContent).toBe('/tables');
    // Editing the title clears it.
    typeTitle('Leads 2');
    expect(within(dialog()).queryByText('You already have a table named "Leads".')).toBeNull();
  });

  it('a column spec error shows at its column', async () => {
    mocks.createTable.mockRejectedValue(
      apiError("Column 'Name': invalid default.", 400, 'invalid_default', { column: 'Name', field: 'default' }),
    );
    renderDialog();
    typeTitle('Leads');
    fireEvent.click(createButton());
    expect(await within(card('Name')).findByText("Column 'Name': invalid default.")).toBeTruthy();
    expect(within(dialog()).queryByText("Column 'Name': invalid default.", { selector: '.new-doc-error' })).toBeNull();
  });

  it('table_limit shows in a banner', async () => {
    mocks.createTable.mockRejectedValue(apiError('You have reached the limit of 200 tables.', 400, 'table_limit'));
    renderDialog();
    typeTitle('Leads');
    fireEvent.click(createButton());
    const alert = await within(dialog()).findByRole('alert');
    expect(alert.textContent).toBe('You have reached the limit of 200 tables.');
    expect(createButton().disabled).toBe(false);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('Escape closes an untouched dialog, not one with a draft', () => {
    renderDialog();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    typeTitle('Leads');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
