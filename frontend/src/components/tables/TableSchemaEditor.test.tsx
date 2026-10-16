// TableSchemaEditor (the Columns panel): refused edits blocked in the form
// (fixed type, single -> multi only, unique only on text / number / url,
// duplicate names, a new required column without a default on a table with
// rows, the last column, the last option, the default's option, a 101st
// column), Save as one updateTableSchema with the derived operations and
// the base version, the footer counting edited things, the stale_schema
// banner and its two ways out, a schema_version move while open,
// rows_violate links into the grid (a normal history entry), op_index
// errors at their column, the discard confirm (the panel's Close, the
// footer's Discard -- also clearing the conflict banner -- Escape inside
// the panel, and the viewer switching panels through the ref handle),
// the shared type labels in summaries, keyboard focus across moves
// and saves, "Has problems" on a collapsed card and the "N problems to fix"
// reveal, a removed option typed again, the read-only view (defaults in the
// cards' true / false vocabulary) and the phone sheet as a modal dialog.
import { createRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigationType } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { TableColumn, TableDetail } from '../../api/types';
import { draftFromSchema } from '../../utils/tableSchemaOps';
import {
  COLUMNS_FULL_NOTE,
  STALE_SCHEMA_BANNER,
  TableSchemaEditor,
  violationLink,
  type TableSchemaEditorHandle,
} from './TableSchemaEditor';

const mocks = vi.hoisted(() => ({ updateTableSchema: vi.fn(), isMobile: false }));

vi.mock('../../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/tablesApi')>()),
  updateTableSchema: mocks.updateTableSchema,
}));

vi.mock('../../hooks/useIsMobile', () => ({
  useIsMobile: () => mocks.isMobile,
}));

function col(key: string, name: string, overrides: Partial<TableColumn> = {}): TableColumn {
  return {
    key, name, type: 'text', required: false, unique: false, default: null, options: null,
    multiple: false, precision: null, description: '', ...overrides,
  };
}

const COLUMNS: TableColumn[] = [
  col('c_00000001', 'Name'),
  col('c_00000002', 'Status', { type: 'enum', options: ['open', 'won'], default: 'open' }),
  col('c_00000003', 'Tags', { type: 'enum', options: ['a', 'b'], multiple: true }),
  col('c_00000004', 'Done', { type: 'boolean' }),
];

function detail(overrides: Partial<TableDetail> = {}): TableDetail {
  return {
    id: 't1', owner_id: 1, project_id: null, title: 'Leads', description: '', mode: 'private',
    schema_version: 3, row_count: 5, last_write_source: 'ui:1', require_approval: false,
    created_at: '2026-10-01T00:00:00', updated_at: '2026-10-02T00:00:00', scope: 'user',
    column_count: COLUMNS.length, shared: false, shared_with_me: false, permission: null, owner: null,
    shares: [], last_write_user: null,
    access: {
      can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
      can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
      can_require_approval: true, can_download_history: true, deny_reason: null,
      schema_deny_reason: null,
    },
    schema: COLUMNS,
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  return (
    <>
      <output data-testid="location">{location.pathname + location.search}</output>
      <output data-testid="navigation-type">{navigationType}</output>
    </>
  );
}

const handlers = { onClose: vi.fn(), onDetailApplied: vi.fn(), onRefresh: vi.fn() };

function ui(table: TableDetail) {
  return (
    <MemoryRouter initialEntries={['/tables/t1']}>
      <TableSchemaEditor table={table} {...handlers} />
      <LocationProbe />
    </MemoryRouter>
  );
}

function renderPanel(table = detail()) {
  return render(ui(table));
}

function expand(name: string) {
  fireEvent.click(screen.getByRole('button', { name: `Expand ${name}` }));
}

function card(name: string): HTMLElement {
  return screen.getByRole('button', { name: new RegExp(`(Expand|Collapse) ${name}$`) }).closest('li') as HTMLElement;
}

function saveButton() {
  return screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
}

beforeEach(() => {
  mocks.isMobile = false;
  mocks.updateTableSchema.mockReset();
  handlers.onClose.mockReset();
  handlers.onDetailApplied.mockReset();
  handlers.onRefresh.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('refused edits are blocked in the form', () => {
  it('fixes the type of an existing column, not of a new one', () => {
    renderPanel();
    expand('Name');
    const nameCard = card('Name');
    expect((within(nameCard).getByLabelText('Type') as HTMLSelectElement).disabled).toBe(true);
    expect(within(nameCard).getByText("Type can't be changed after creation")).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add column' }));
    const fresh = card('untitled column');
    expect((within(fresh).getByLabelText('Type') as HTMLSelectElement).disabled).toBe(false);
  });

  it('allows single -> multi but not back once a column allows several', () => {
    renderPanel();
    expand('Tags');
    const multi = within(card('Tags')).getByLabelText('Allow several options') as HTMLInputElement;
    expect(multi.checked).toBe(true);
    expect(multi.disabled).toBe(true);
    expect(within(card('Tags')).getByText("Can't switch back to a single option")).toBeTruthy();
    expand('Status');
    expect((within(card('Status')).getByLabelText('Allow several options') as HTMLInputElement).disabled).toBe(false);
  });

  it('offers Unique only on text, number and URL columns', () => {
    renderPanel();
    expand('Name');
    expand('Done');
    expand('Status');
    expect(within(card('Name')).queryByLabelText('Unique')).toBeTruthy();
    expect(within(card('Done')).queryByLabelText('Unique')).toBeNull();
    expect(within(card('Status')).queryByLabelText('Unique')).toBeNull();
  });

  it('a duplicate name (any case) shows at the field and blocks Save', () => {
    renderPanel();
    expand('Done');
    fireEvent.change(within(card('Done')).getByLabelText('Name'), { target: { value: 'name' } });
    // Both cards are flagged; a collapsed one shows its message when opened.
    expect(card('Name').className).toContain('schema-editor-column--invalid');
    expect(screen.getAllByText('Another column has this name (names ignore case).').length).toBe(1);
    expand('Name');
    expect(screen.getAllByText('Another column has this name (names ignore case).').length).toBe(2);
    expect(saveButton().disabled).toBe(true);
  });

  it('a new required column needs a default while the table has rows', () => {
    renderPanel(detail({ row_count: 5 }));
    fireEvent.click(screen.getByRole('button', { name: 'Add column' }));
    const fresh = card('untitled column');
    fireEvent.change(within(fresh).getByLabelText('Name'), { target: { value: 'Owner' } });
    fireEvent.click(within(card('Owner')).getByLabelText('Required'));
    expect(within(card('Owner')).getByText('A required column needs a default while the table has rows.')).toBeTruthy();
    expect(saveButton().disabled).toBe(true);
    fireEvent.change(within(card('Owner')).getByLabelText(/^Default/), { target: { value: 'me' } });
    expect(saveButton().disabled).toBe(false);
  });

  it('disables Delete for the last column', () => {
    renderPanel(detail({ schema: [COLUMNS[0]] }));
    expect((screen.getByRole('button', { name: 'Delete Name' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("disables Remove for the last option and for the default's option", () => {
    renderPanel(detail({ schema: [COLUMNS[1], col('c_00000005', 'One', { type: 'enum', options: ['x'] })] }));
    expand('Status');
    expand('One');
    const status = card('Status');
    expect((within(status).getByRole('button', { name: 'Remove option open' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(status).getByRole('button', { name: 'Remove option won' }) as HTMLButtonElement).disabled).toBe(false);
    expect((within(card('One')).getByRole('button', { name: 'Remove option x' }) as HTMLButtonElement).disabled).toBe(true);
    // Existing options never move; a new column's options do.
    expect(within(status).queryByRole('button', { name: 'Move open down' })).toBeNull();
  });
});

describe('Save', () => {
  it('sends one updateTableSchema with the derived operations and the base version', async () => {
    const saved = detail({ schema_version: 4, schema: [col('c_00000001', 'Full name'), ...COLUMNS.slice(1)] });
    mocks.updateTableSchema.mockResolvedValue({ ...saved, change_id: 9 });
    renderPanel();
    expect(saveButton().disabled).toBe(true);
    expect(screen.getByText('No changes')).toBeTruthy();
    expand('Name');
    fireEvent.change(within(card('Name')).getByLabelText('Name'), { target: { value: 'Full name' } });
    fireEvent.click(screen.getByRole('button', { name: 'Move Done up' }));
    expect(screen.getByText('1 column changed, new column order')).toBeTruthy();
    fireEvent.click(saveButton());
    await waitFor(() => expect(handlers.onDetailApplied).toHaveBeenCalledTimes(1));
    expect(mocks.updateTableSchema).toHaveBeenCalledTimes(1);
    expect(mocks.updateTableSchema).toHaveBeenCalledWith('t1', {
      operations: [
        { op: 'rename_column', name: 'Name', new_name: 'Full name' },
        { op: 'reorder_columns', names: ['Full name', 'Status', 'Done', 'Tags'] },
      ],
      expected_schema_version: 3,
    });
    expect(handlers.onDetailApplied.mock.calls[0][0].schema_version).toBe(4);
    expect(screen.getByText('Saved: 1 column changed, new column order.')).toBeTruthy();
    expect(saveButton().disabled).toBe(true);
  });

  it('counts edited columns, not operations (a name swap is two columns, three renames)', async () => {
    renderPanel();
    expand('Name');
    expand('Done');
    fireEvent.change(within(card('Name')).getByLabelText('Name'), { target: { value: 'Tmp' } });
    fireEvent.change(within(card('Done')).getByLabelText('Name'), { target: { value: 'Name' } });
    fireEvent.change(within(card('Tmp')).getByLabelText('Name'), { target: { value: 'Done' } });
    expect(screen.getByText('2 columns changed')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Close columns panel' }));
    expect(screen.getByText('Your edits to the columns of "Leads" (2 columns changed) will be lost.')).toBeTruthy();
  });

  it('keeps the expanded cards across a save and moves focus off the disabled Save to the heading', async () => {
    mocks.updateTableSchema.mockImplementation(async () => ({
      ...detail({ schema_version: 4, schema: [COLUMNS[0], COLUMNS[1], COLUMNS[2], COLUMNS[3], col('c_0000000a', 'Owner')] }),
      change_id: 1,
    }));
    renderPanel(detail({ row_count: 0 }));
    expand('Status');
    fireEvent.click(screen.getByRole('button', { name: 'Add column' }));
    fireEvent.change(within(card('untitled column')).getByLabelText('Name'), { target: { value: 'Owner' } });
    saveButton().focus();
    fireEvent.click(saveButton());
    await screen.findByText('Saved: 1 column added.');
    // Not remounted: Status stays open, and the saved column's card follows it to its key.
    expect(screen.getByRole('button', { name: 'Collapse Status' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Collapse Owner' })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Columns' }));
  });

  it('stale_schema: banner, then "Reload and keep my edits" re-bases and saves against the new version', async () => {
    const newer = [...COLUMNS, col('c_00000009', 'Score', { type: 'number' })];
    mocks.updateTableSchema.mockRejectedValueOnce(
      new ApiClientError('Stale', 409, 'stale_schema', undefined, {
        detail: { error: 'stale_schema', message: 'Stale', schema_version: 5, schema: newer },
      }),
    );
    renderPanel();
    expand('Name');
    fireEvent.change(within(card('Name')).getByLabelText('Description (optional)'), { target: { value: 'Who' } });
    fireEvent.click(saveButton());
    await screen.findByText("This table's columns changed while you were editing.");
    expect(handlers.onRefresh).toHaveBeenCalled();
    expect(saveButton().disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Reload and keep my edits' }));
    expect(screen.queryByText("This table's columns changed while you were editing.")).toBeNull();
    expect(screen.getByRole('button', { name: 'Expand Score' })).toBeTruthy();
    mocks.updateTableSchema.mockResolvedValueOnce({ ...detail({ schema_version: 6, schema: newer }), change_id: 2 });
    fireEvent.click(saveButton());
    await waitFor(() => expect(mocks.updateTableSchema).toHaveBeenCalledTimes(2));
    expect(mocks.updateTableSchema.mock.calls[1][1]).toEqual({
      operations: [{ op: 'set_description', name: 'Name', description: 'Who' }],
      expected_schema_version: 5,
    });
  });

  it('stale_schema: "Discard my edits" takes the current schema', async () => {
    mocks.updateTableSchema.mockRejectedValueOnce(
      new ApiClientError('Stale', 409, 'stale_schema', undefined, {
        detail: { error: 'stale_schema', schema_version: 5, schema: [COLUMNS[0]] },
      }),
    );
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Done' }));
    fireEvent.click(saveButton());
    fireEvent.click(await screen.findByRole('button', { name: 'Discard my edits' }));
    expect(screen.queryByRole('button', { name: 'Expand Status' })).toBeNull();
    expect(screen.getByText('No changes')).toBeTruthy();
  });

  it('rows_violate shows the message and a link that filters the grid on the column', async () => {
    mocks.updateTableSchema.mockRejectedValueOnce(
      new ApiClientError("Column 'Full name' cannot be made required: 2 rows have no value (_id 3, 7).", 400, 'rows_violate', undefined, {
        detail: { error: 'rows_violate', column: 'Full name', check: 'required', option: null, row_ids: [3, 7], total: 2 },
      }),
    );
    renderPanel();
    expand('Name');
    fireEvent.change(within(card('Name')).getByLabelText('Name'), { target: { value: 'Full name' } });
    fireEvent.click(within(card('Full name')).getByLabelText('Required'));
    fireEvent.click(saveButton());
    const link = await screen.findByRole('link', { name: 'Show the rows with no Name' });
    // The server's message already lists the ids; nothing repeats them.
    expect(screen.getAllByText(/_id 3, 7/)).toHaveLength(1);
    const filter = new URLSearchParams(link.getAttribute('href')!.split('?')[1]).get('filter');
    expect(JSON.parse(filter!)).toEqual({ column: 'Name', op: 'is_null' });
    fireEvent.click(link);
    // Same table: no discard prompt, the draft stays; Back returns to the unfiltered grid.
    expect(screen.getByTestId('location').textContent).toContain('/tables/t1?filter=');
    expect(screen.getByTestId('navigation-type').textContent).toBe('PUSH');
    expect(screen.queryByText('Discard column changes?')).toBeNull();
    expect(screen.getByText('1 column changed')).toBeTruthy();
  });

  it('an op_index error shows at its column', async () => {
    mocks.updateTableSchema.mockRejectedValueOnce(
      new ApiClientError('Operation 1 (set_default): invalid default', 400, 'invalid_default', undefined, {
        detail: { error: 'invalid_default', op_index: 0, op: 'set_default', column: 'Done', field: 'default' },
      }),
    );
    renderPanel();
    expand('Done');
    fireEvent.change(within(card('Done')).getByLabelText(/^Default/), { target: { value: 'true' } });
    fireEvent.click(saveButton());
    expect(await within(card('Done')).findByText('Operation 1 (set_default): invalid default')).toBeTruthy();
  });
});

describe('schema_version moving while open', () => {
  it('re-bases silently without edits, raises the banner with edits', () => {
    const { rerender } = renderPanel();
    rerender(ui(detail({ schema_version: 4, schema: [...COLUMNS, col('c_00000009', 'Score')] })));
    expect(screen.getByRole('button', { name: 'Expand Score' })).toBeTruthy();
    expect(screen.queryByText("This table's columns changed while you were editing.")).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Score' }));
    rerender(ui(detail({ schema_version: 5, schema: COLUMNS })));
    expect(screen.getByText("This table's columns changed while you were editing.")).toBeTruthy();
  });
});

describe('schema_version moving while open: keeping the edits', () => {
  it('"Reload and keep my edits" from the proactive banner re-bases on the new schema', async () => {
    const { rerender } = renderPanel();
    expand('Name');
    fireEvent.change(within(card('Name')).getByLabelText('Description (optional)'), { target: { value: 'Who' } });
    const newer = [...COLUMNS, col('c_00000009', 'Score', { type: 'number' })];
    rerender(ui(detail({ schema_version: 4, schema: newer })));
    expect(screen.getByText("This table's columns changed while you were editing.")).toBeTruthy();
    expect(saveButton().disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Reload and keep my edits' }));
    expect(screen.getByRole('button', { name: 'Expand Score' })).toBeTruthy();
    mocks.updateTableSchema.mockResolvedValueOnce({ ...detail({ schema_version: 5, schema: newer }), change_id: 3 });
    fireEvent.click(saveButton());
    await waitFor(() => expect(mocks.updateTableSchema).toHaveBeenCalledTimes(1));
    expect(mocks.updateTableSchema.mock.calls[0][1]).toEqual({
      operations: [{ op: 'set_description', name: 'Name', description: 'Who' }],
      expected_schema_version: 4,
    });
  });
});

describe('the footer Discard', () => {
  it('asks first; Cancel keeps the edits, Discard drops them and focuses the heading', () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Done' }));
    const discard = screen.getByRole('button', { name: 'Discard' });
    discard.focus();
    fireEvent.click(discard);
    let dialog = screen.getByRole('dialog', { name: 'Discard column changes?' });
    expect(within(dialog).getByText('Your edits to the columns of "Leads" (1 column removed) will be lost.')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.getByText('1 column removed')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    dialog = screen.getByRole('dialog', { name: 'Discard column changes?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard' }));
    expect(screen.getByText('No changes')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Expand Done' })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Columns' }));
    expect(handlers.onClose).not.toHaveBeenCalled();
  });

  it('while the conflict banner is up, takes the newer schema and clears the banner', () => {
    const { rerender } = renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Done' }));
    rerender(ui(detail({ schema_version: 4, schema: [...COLUMNS, col('c_00000009', 'Score')] })));
    expect(screen.getByText(STALE_SCHEMA_BANNER)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Discard column changes?' })).getByRole('button', { name: 'Discard' }));
    expect(screen.queryByText(STALE_SCHEMA_BANNER)).toBeNull();
    expect(screen.getByRole('button', { name: 'Expand Score' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Expand Done' })).toBeTruthy();
    expect(screen.getByText('No changes')).toBeTruthy();
  });
});

describe('keyboard focus across moves', () => {
  function liveRegion() {
    return document.querySelector('.schema-editor [role="status"]') as HTMLElement;
  }

  it('refocuses the pressed Move button on the moved card and announces the new position', () => {
    renderPanel();
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');
    fireEvent.click(screen.getByRole('button', { name: 'Move Done up' }));
    const up = screen.getByRole('button', { name: 'Move Done up' });
    expect(focus.mock.contexts.at(-1)).toBe(up);
    expect(document.activeElement).toBe(up);
    expect(liveRegion().textContent).toBe('Done moved to position 3 of 4');
    focus.mockRestore();
  });

  it('at an end, where the pressed button turns disabled, focuses the opposite one', () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Move Status up' }));
    expect((screen.getByRole('button', { name: 'Move Status up' }) as HTMLButtonElement).disabled).toBe(true);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Move Status down' }));
    expect(liveRegion().textContent).toBe('Status moved to position 1 of 4');
    fireEvent.click(screen.getByRole('button', { name: 'Move Status down' }));
    expect(screen.getByText('No changes')).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Move Status down' }));
    expect(liveRegion().textContent).toBe('Status moved to position 2 of 4');
  });

  it("does the same for a new column's options", () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Add column' }));
    const fresh = card('untitled column');
    fireEvent.change(within(fresh).getByLabelText('Type'), { target: { value: 'enum' } });
    fireEvent.change(within(fresh).getByLabelText('Option 1'), { target: { value: 'low' } });
    fireEvent.click(within(fresh).getByRole('button', { name: 'Add option' }));
    fireEvent.change(within(fresh).getByLabelText('Option 2'), { target: { value: 'high' } });
    fireEvent.click(within(fresh).getByRole('button', { name: 'Move high up' }));
    expect((within(fresh).getByLabelText('Option 1') as HTMLInputElement).value).toBe('high');
    expect(document.activeElement).toBe(within(fresh).getByRole('button', { name: 'Move high down' }));
    expect(liveRegion().textContent).toBe('high moved to position 1 of 2');
  });

  it('deleting a column focuses the card taking its place (the new last one at the end)', () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Status' }));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Expand Tags' }));
    expect(liveRegion().textContent).toBe('Status deleted');
    fireEvent.click(screen.getByRole('button', { name: 'Delete Done' }));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Expand Tags' }));
    // Focus stayed in the panel: Escape still reaches it.
    fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: 'Discard column changes?' })).toBeTruthy();
  });

  it('removing an option focuses the option input taking its place', () => {
    renderPanel();
    expand('Tags');
    const tags = card('Tags');
    fireEvent.click(within(tags).getByRole('button', { name: 'Remove option a' }));
    expect(document.activeElement).toBe(within(tags).getByLabelText('Option 1'));
    expect((document.activeElement as HTMLInputElement).value).toBe('b');
    expect(liveRegion().textContent).toBe('Option a removed');
  });
});

describe('problems on collapsed cards', () => {
  it('a collapsed card with a problem says "Has problems"; the footer button expands and focuses it', () => {
    renderPanel();
    expand('Done');
    fireEvent.change(within(card('Done')).getByLabelText('Name'), { target: { value: 'name' } });
    const toggle = screen.getByRole('button', { name: 'Expand Name' });
    const cue = within(card('Name')).getByText('Has problems');
    expect(toggle.getAttribute('aria-describedby')).toBe(cue.id);
    // The open card shows its field error instead.
    expect(within(card('name')).queryByText('Has problems')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '2 problems to fix' }));
    expect(screen.getByRole('button', { name: 'Collapse Name' })).toBeTruthy();
    expect(within(card('Name')).queryByText('Has problems')).toBeNull();
    expect(document.activeElement).toBe(within(card('Name')).getByLabelText('Name'));
  });
});

describe('a removed option typed again', () => {
  it('goes back to its place when its field is left: the option and its rows are kept', () => {
    renderPanel();
    expand('Status');
    const status = card('Status');
    fireEvent.click(within(status).getByRole('button', { name: 'Remove option won' }));
    expect(screen.getByText('1 column changed')).toBeTruthy();
    fireEvent.click(within(status).getByRole('button', { name: 'Add option' }));
    const input = within(status).getByLabelText('Option 2') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'won' } });
    fireEvent.blur(input);
    expect(screen.getByText('No changes')).toBeTruthy();
    expect(saveButton().disabled).toBe(true);
  });
});

describe('column limit', () => {
  it('disables Add column at 100 columns and says why', () => {
    const many = Array.from({ length: 100 }, (_, i) => col(`c_${String(i).padStart(8, '0')}`, `C${i}`));
    renderPanel(detail({ schema: many }));
    const add = screen.getByRole('button', { name: 'Add column' }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    expect(add.title).toBe(COLUMNS_FULL_NOTE);
    expect(screen.getByText(COLUMNS_FULL_NOTE).id).toBe(add.getAttribute('aria-describedby'));
  });
});

describe('closing', () => {
  it('closes at once without edits and asks first with edits', () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Close columns panel' }));
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Delete Done' }));
    fireEvent.click(screen.getByRole('button', { name: 'Close columns panel' }));
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
    const dialog = screen.getByRole('dialog', { name: 'Discard column changes?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard' }));
    expect(handlers.onClose).toHaveBeenCalledTimes(2);
  });
});

describe('Escape', () => {
  it('closes at once without edits, from a field too', () => {
    renderPanel();
    fireEvent.keyDown(screen.getByRole('heading', { name: 'Columns' }), { key: 'Escape' });
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
    expand('Name');
    fireEvent.keyDown(screen.getByLabelText('Name'), { key: 'Escape' });
    expect(handlers.onClose).toHaveBeenCalledTimes(2);
  });

  it('with edits, asks first; Escape in the confirm closes only the confirm', () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Done' }));
    fireEvent.keyDown(screen.getByRole('heading', { name: 'Columns' }), { key: 'Escape' });
    expect(handlers.onClose).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog', { name: 'Discard column changes?' });
    fireEvent.keyDown(within(dialog).getByRole('button', { name: 'Cancel' }), { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(handlers.onClose).not.toHaveBeenCalled();
    expect(screen.getByText('1 column removed')).toBeTruthy();
  });

  it('consumes the key, so a document Escape listener (the confirm\'s own) never sees it', () => {
    renderPanel();
    const seen = vi.fn();
    document.addEventListener('keydown', seen);
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Delete Done' }));
      fireEvent.keyDown(screen.getByRole('heading', { name: 'Columns' }), { key: 'Escape' });
      expect(seen).not.toHaveBeenCalled();
      expect(screen.getByRole('dialog', { name: 'Discard column changes?' })).toBeTruthy();
    } finally {
      document.removeEventListener('keydown', seen);
    }
  });

  it('closes the read-only view', () => {
    renderPanel(detail({ access: { ...detail().access, can_edit_schema: false } }));
    fireEvent.keyDown(screen.getByRole('heading', { name: 'Columns' }), { key: 'Escape' });
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
  });
});

describe('type labels', () => {
  it('summaries and the read-only list use the shared vocabulary', () => {
    const columns = [
      col('c_00000001', 'Name'),
      col('c_00000003', 'Tags', { type: 'enum', options: ['a', 'b'], multiple: true }),
      col('c_00000005', 'Budget', { type: 'number', precision: 2, required: true }),
    ];
    renderPanel(detail({ schema: columns }));
    expect(within(card('Tags')).getByText('Choice (several)')).toBeTruthy();
    expect(within(card('Budget')).getByText('Number (2 decimal places) · required')).toBeTruthy();
    cleanup();
    renderPanel(detail({ schema: columns, access: { ...detail().access, can_edit_schema: false } }));
    expect(screen.getByText('Number (2 decimal places) · required')).toBeTruthy();
    expect(screen.getByText('Choice (several)')).toBeTruthy();
  });
});

describe('requestClose (the viewer switching panels)', () => {
  function renderWithRef(table = detail()) {
    const ref = createRef<TableSchemaEditorHandle>();
    render(
      <MemoryRouter initialEntries={['/tables/t1']}>
        <TableSchemaEditor ref={ref} table={table} {...handlers} />
      </MemoryRouter>,
    );
    return ref;
  }

  it('runs the callback at once without edits, instead of onClose', () => {
    const ref = renderWithRef();
    const then = vi.fn();
    act(() => ref.current!.requestClose(then));
    expect(then).toHaveBeenCalledTimes(1);
    expect(handlers.onClose).not.toHaveBeenCalled();
  });

  it('with edits, asks first: Discard runs the callback, Cancel keeps the draft', () => {
    const ref = renderWithRef();
    const then = vi.fn();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Done' }));
    act(() => ref.current!.requestClose(then));
    expect(then).not.toHaveBeenCalled();
    let dialog = screen.getByRole('dialog', { name: 'Discard column changes?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(then).not.toHaveBeenCalled();
    expect(screen.getByText('1 column removed')).toBeTruthy();
    act(() => ref.current!.requestClose(then));
    dialog = screen.getByRole('dialog', { name: 'Discard column changes?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard' }));
    expect(then).toHaveBeenCalledTimes(1);
    expect(handlers.onClose).not.toHaveBeenCalled();
  });

  it('the read-only view runs the callback at once', () => {
    const ref = renderWithRef(detail({ access: { ...detail().access, can_edit_schema: false } }));
    const then = vi.fn();
    act(() => ref.current!.requestClose(then));
    expect(then).toHaveBeenCalledTimes(1);
  });
});

describe('read-only for non-owners', () => {
  it('lists the columns and their settings without inputs, with the reason', () => {
    renderPanel(
      detail({
        shared_with_me: true,
        access: {
          ...detail().access, can_edit_schema: false, schema: 'denied', can_rename: false,
          schema_deny_reason: "Only the table's owner can change its columns.",
        },
      }),
    );
    expect(screen.getByText("Only the table's owner can change its columns.")).toBeTruthy();
    expect(screen.getByText('Options: open, won')).toBeTruthy();
    expect(screen.getByText('Default: open')).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
  });

  it('writes boolean and multi defaults as the cards and History do', () => {
    const schema = [
      col('c_00000004', 'Done', { type: 'boolean', default: true }),
      col('c_00000005', 'Archived', { type: 'boolean', default: false }),
      col('c_00000003', 'Tags', { type: 'enum', options: ['a', 'b'], multiple: true, default: ['a', 'b'] }),
    ];
    renderPanel(detail({ schema, access: { ...detail().access, can_edit_schema: false } }));
    expect(screen.getByText('Default: true')).toBeTruthy();
    expect(screen.getByText('Default: false')).toBeTruthy();
    expect(screen.getByText('Default: a, b')).toBeTruthy();
    expect(screen.queryByText(/Checked/)).toBeNull();
  });
});

describe('boolean defaults in the form', () => {
  it('offers true / false, the one boolean vocabulary', () => {
    renderPanel();
    expand('Done');
    const select = within(card('Done')).getByLabelText(/Default/) as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual(['No default', 'true', 'false']);
  });
});

describe('phone sheet', () => {
  it('is a modal dialog named by its heading on phones, a landmark otherwise', () => {
    mocks.isMobile = true;
    renderPanel();
    const sheet = screen.getByRole('dialog', { name: 'Columns' });
    expect(sheet.getAttribute('aria-modal')).toBe('true');
    expect(screen.queryByRole('complementary')).toBeNull();
    // Escape inside it still closes it.
    fireEvent.keyDown(within(sheet).getByRole('button', { name: 'Expand Name' }), { key: 'Escape' });
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
    cleanup();
    mocks.isMobile = false;
    renderPanel();
    expect(screen.getByRole('complementary', { name: 'Columns' })).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('violationLink', () => {
  const draft = draftFromSchema('Leads', COLUMNS);
  const base = { column: null, check: null, option: null, row_ids: [1], total: 1 };
  const params = (to: string) => new URLSearchParams(to.split('?')[1]);

  it('enum_option: eq on a single column, contains on a multi one', () => {
    const single = violationLink({ ...base, column: 'Status', check: 'enum_option', option: 'won' }, draft, COLUMNS, 't1')!;
    expect(JSON.parse(params(single.to).get('filter')!)).toEqual({ column: 'Status', op: 'eq', value: 'won' });
    const multi = violationLink({ ...base, column: 'Tags', check: 'enum_option', option: 'a' }, draft, COLUMNS, 't1')!;
    expect(JSON.parse(params(multi.to).get('filter')!)).toEqual({ column: 'Tags', op: 'contains', value: 'a' });
  });

  it('unique: the non-empty cells sorted by the column', () => {
    const link = violationLink({ ...base, column: 'Name', check: 'unique' }, draft, COLUMNS, 't1')!;
    expect(JSON.parse(params(link.to).get('filter')!)).toEqual({ column: 'Name', op: 'not_null' });
    expect(JSON.parse(params(link.to).get('sort')!)).toEqual([{ column: 'Name', direction: 'asc' }]);
  });

  it('no link for an option the loaded column does not have (a temporary spelling)', () => {
    expect(
      violationLink({ ...base, column: 'Status', check: 'enum_option', option: 'Removing option 1' }, draft, COLUMNS, 't1'),
    ).toBeNull();
  });

  it('no link for a column the grid does not have', () => {
    expect(violationLink({ ...base, column: 'Nope', check: 'required' }, draft, COLUMNS, 't1')).toBeNull();
  });
});
