// TableFilterBar: chips by column name with remove buttons, editing a chip,
// the add form (column -> op -> typed value -> Apply) emitting the DSL,
// Apply blocked while invalid, Escape / Cancel closing with focus returned,
// Clear all, custom / unreadable / stale filters, and server error marking.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { FilterNode, TableColumn } from '../../api/types';
import { TableFilterBar } from './TableFilterBar';
import type { TableFilterError } from './TableFilterBar';

function col(name: string, type: TableColumn['type'], extra: Partial<TableColumn> = {}): TableColumn {
  return {
    key: `k_${name}`, name, type, required: false, unique: false, default: null, options: null,
    multiple: false, precision: null, description: '', ...extra,
  };
}

const COLUMNS: TableColumn[] = [
  col('Name', 'text'),
  col('Site', 'url'),
  col('Revenue', 'number', { precision: 2 }),
  col('Active', 'boolean'),
  col('Status', 'enum', { options: ['Lead', 'Customer'] }),
  col('Tags', 'enum', { options: ['a', 'b', 'c'], multiple: true }),
];

function setup(filter: FilterNode | null, extra: { error?: TableFilterError | null; invalidParam?: boolean } = {}) {
  const onChange = vi.fn();
  const result = render(
    <TableFilterBar columns={COLUMNS} filter={filter} onChange={onChange} error={extra.error} invalidParam={extra.invalidParam} />,
  );
  return { onChange, ...result };
}

function openAdd() {
  fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
  return screen.getByRole('dialog', { name: 'Add filter' });
}

const apply = () => screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement;

afterEach(() => cleanup());

describe('TableFilterBar chips', () => {
  it('renders one chip per condition by column name, and removes one', () => {
    const { onChange } = setup({
      and: [
        { column: 'Status', op: 'eq', value: 'Customer' },
        { column: 'Revenue', op: 'between', value: ['10', '20'] },
      ],
    });
    expect(screen.getByRole('button', { name: 'Edit filter: Status is Customer' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Edit filter: Revenue between 10 and 20' })).toBeTruthy();
    // Column keys are never shown.
    expect(document.body.textContent).not.toContain('k_Status');
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Status is Customer' }));
    // The one left is emitted unwrapped.
    expect(onChange).toHaveBeenCalledWith({ column: 'Revenue', op: 'between', value: ['10', '20'] });
  });

  it('removing the last chip clears the filter', () => {
    const { onChange } = setup({ column: 'Name', op: 'is_null' });
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Name is empty' }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('Clear all clears; no Clear without a filter', () => {
    const { onChange, unmount } = setup({ column: 'Name', op: 'is_null' });
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    expect(onChange).toHaveBeenCalledWith(null);
    unmount();
    setup(null);
    expect(screen.queryByRole('button', { name: /Clear/ })).toBeNull();
  });

  it('clicking a chip edits it in place', () => {
    const { onChange } = setup({
      and: [
        { column: 'Name', op: 'contains', value: 'ab' },
        { column: 'Revenue', op: 'gt', value: '5' },
      ],
    });
    fireEvent.click(screen.getByRole('button', { name: 'Edit filter: Revenue > 5' }));
    const dialog = screen.getByRole('dialog', { name: 'Edit filter' });
    expect((within(dialog).getByLabelText('Column') as HTMLSelectElement).value).toBe('Revenue');
    fireEvent.change(within(dialog).getByLabelText('Value'), { target: { value: '7.5' } });
    fireEvent.click(apply());
    expect(onChange).toHaveBeenCalledWith({
      and: [
        { column: 'Name', op: 'contains', value: 'ab' },
        { column: 'Revenue', op: 'gt', value: '7.5' },
      ],
    });
  });
});

describe('TableFilterBar add form', () => {
  it('adds a text condition (default column and op) appended to existing ones', () => {
    const { onChange } = setup({ column: 'Status', op: 'eq', value: 'Lead' });
    const dialog = openAdd();
    // Focus moves into the dialog.
    expect(document.activeElement).toBe(within(dialog).getByLabelText('Column'));
    expect(apply().disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText('Value'), { target: { value: 'Ada' } });
    expect(apply().disabled).toBe(false);
    fireEvent.click(apply());
    expect(onChange).toHaveBeenCalledWith({
      and: [
        { column: 'Status', op: 'eq', value: 'Lead' },
        { column: 'Name', op: 'eq', value: 'Ada' },
      ],
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('number: decimal strings only, between needs ordered bounds', () => {
    const { onChange } = setup(null);
    const dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Revenue' } });
    const value = within(dialog).getByLabelText('Value') as HTMLInputElement;
    expect(value.getAttribute('inputmode')).toBe('decimal');
    fireEvent.change(value, { target: { value: '1,234' } });
    expect(apply().disabled).toBe(true);
    expect(within(dialog).getByText('Enter a number')).toBeTruthy();

    fireEvent.change(within(dialog).getByLabelText('Condition'), { target: { value: 'between' } });
    fireEvent.change(within(dialog).getByLabelText('From'), { target: { value: '20' } });
    fireEvent.change(within(dialog).getByLabelText('To'), { target: { value: '10' } });
    expect(apply().disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText('To'), { target: { value: '30' } });
    expect(apply().disabled).toBe(false);
    fireEvent.click(apply());
    expect(onChange).toHaveBeenCalledWith({ column: 'Revenue', op: 'between', value: ['20', '30'] });
  });

  it('number in: one per line; a comma is not a separator', () => {
    const { onChange } = setup(null);
    const dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Revenue' } });
    fireEvent.change(within(dialog).getByLabelText('Condition'), { target: { value: 'in' } });
    const values = within(dialog).getByLabelText('Values (one per line)');
    fireEvent.change(values, { target: { value: '1,234\n3.5' } });
    expect(apply().disabled).toBe(true);
    expect(within(dialog).getByText('Enter numbers only')).toBeTruthy();
    fireEvent.change(values, { target: { value: ' 1\n2\n3.5\n' } });
    fireEvent.click(apply());
    expect(onChange).toHaveBeenCalledWith({ column: 'Revenue', op: 'in', value: ['1', '2', '3.5'] });
  });

  it('text in keeps values as typed', () => {
    const { onChange } = setup(null);
    const dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Condition'), { target: { value: 'in' } });
    fireEvent.change(within(dialog).getByLabelText('Values (one per line)'), { target: { value: ' a \n\nb' } });
    fireEvent.click(apply());
    expect(onChange).toHaveBeenCalledWith({ column: 'Name', op: 'in', value: [' a ', 'b'] });
  });

  it('url is / is not needs an http(s) URL; contains takes any text', () => {
    const { onChange } = setup(null);
    const dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Site' } });
    const value = within(dialog).getByLabelText('Value');
    fireEvent.change(value, { target: { value: 'example.com' } });
    expect(apply().disabled).toBe(true);
    expect(within(dialog).getByText('Enter an http(s) URL')).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Condition'), { target: { value: 'contains' } });
    expect(apply().disabled).toBe(false);
    fireEvent.click(apply());
    expect(onChange).toHaveBeenCalledWith({ column: 'Site', op: 'contains', value: 'example.com' });
  });

  it('links the value input to the validation hint while it shows a problem', () => {
    setup(null);
    const dialog = openAdd();
    expect(within(dialog).getByLabelText('Value').getAttribute('aria-describedby')).toBeNull();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Revenue' } });
    const number = within(dialog).getByLabelText('Value');
    fireEvent.change(number, { target: { value: 'abc' } });
    const hint = within(dialog).getByText('Enter a number');
    expect(hint.id).toBeTruthy();
    expect(number.getAttribute('aria-describedby')).toBe(hint.id);

    fireEvent.change(within(dialog).getByLabelText('Condition'), { target: { value: 'between' } });
    fireEvent.change(within(dialog).getByLabelText('From'), { target: { value: '5' } });
    const pairHint = within(dialog).getByText('Enter both bounds');
    expect(within(dialog).getByLabelText('From').getAttribute('aria-describedby')).toBe(pairHint.id);
    expect(within(dialog).getByLabelText('To').getAttribute('aria-describedby')).toBe(pairHint.id);
    fireEvent.change(within(dialog).getByLabelText('To'), { target: { value: '6' } });
    expect(within(dialog).getByLabelText('To').getAttribute('aria-describedby')).toBeNull();
  });

  it('Add filter toggles the dialog closed when it is open', () => {
    setup(null);
    const add = screen.getByRole('button', { name: 'Add filter' });
    openAdd();
    expect(add.getAttribute('aria-expanded')).toBe('true');
    // A real click: mousedown (not an outside click) then click (toggle).
    fireEvent.mouseDown(add);
    expect(screen.getByRole('dialog')).toBeTruthy();
    fireEvent.click(add);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(add.getAttribute('aria-expanded')).toBe('false');
  });

  it('a second click on the chip being edited closes the dialog', () => {
    setup({ column: 'Name', op: 'eq', value: 'x' });
    const chip = screen.getByRole('button', { name: 'Edit filter: Name is x' });
    fireEvent.click(chip);
    expect(screen.getByRole('dialog', { name: 'Edit filter' })).toBeTruthy();
    fireEvent.mouseDown(chip);
    fireEvent.click(chip);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('boolean: a JSON boolean from the true / false control', () => {
    const { onChange } = setup(null);
    const dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Active' } });
    expect(apply().disabled).toBe(true);
    fireEvent.click(within(dialog).getByLabelText('false'));
    fireEvent.click(apply());
    expect(onChange).toHaveBeenCalledWith({ column: 'Active', op: 'eq', value: false });
  });

  it('single enum: a select, and `is any of` as a checklist', () => {
    const { onChange } = setup(null);
    let dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Status' } });
    fireEvent.change(within(dialog).getByLabelText('Value'), { target: { value: 'Customer' } });
    fireEvent.click(apply());
    expect(onChange).toHaveBeenLastCalledWith({ column: 'Status', op: 'eq', value: 'Customer' });

    dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Status' } });
    fireEvent.change(within(dialog).getByLabelText('Condition'), { target: { value: 'in' } });
    expect(apply().disabled).toBe(true);
    fireEvent.click(within(dialog).getByLabelText('Customer'));
    fireEvent.click(within(dialog).getByLabelText('Lead'));
    fireEvent.click(apply());
    expect(onChange).toHaveBeenLastCalledWith({ column: 'Status', op: 'in', value: ['Lead', 'Customer'] });
  });

  it('multi enum: has all of, and is empty takes no value', () => {
    const { onChange } = setup(null);
    let dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Tags' } });
    fireEvent.change(within(dialog).getByLabelText('Condition'), { target: { value: 'contains_all' } });
    fireEvent.click(within(dialog).getByLabelText('c'));
    fireEvent.click(within(dialog).getByLabelText('a'));
    fireEvent.click(apply());
    expect(onChange).toHaveBeenLastCalledWith({ column: 'Tags', op: 'contains_all', value: ['a', 'c'] });

    dialog = openAdd();
    fireEvent.change(within(dialog).getByLabelText('Column'), { target: { value: 'Tags' } });
    fireEvent.change(within(dialog).getByLabelText('Condition'), { target: { value: 'is_empty' } });
    expect(within(dialog).queryByLabelText('Value')).toBeNull();
    expect(within(dialog).queryByText('Values')).toBeNull();
    fireEvent.click(apply());
    expect(onChange).toHaveBeenLastCalledWith({ column: 'Tags', op: 'is_empty' });
  });

  it('Escape and Cancel close without applying and return focus', async () => {
    const { onChange } = setup(null);
    const add = screen.getByRole('button', { name: 'Add filter' });
    let dialog = openAdd();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(document.activeElement).toBe(add);

    dialog = openAdd();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('a click outside closes the dialog', () => {
    setup(null);
    openAdd();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('no Add filter at the 20-condition cap', () => {
    setup({ and: Array.from({ length: 20 }, () => ({ column: 'Name', op: 'not_null' as const })) });
    expect(screen.getAllByRole('button', { name: /^Edit filter/ })).toHaveLength(20);
    expect(screen.queryByRole('button', { name: 'Add filter' })).toBeNull();
  });
});

describe('TableFilterBar special states', () => {
  it('a custom filter is one chip with Clear and no Add', () => {
    const { onChange } = setup({ or: [{ column: 'Name', op: 'is_null' }, { column: 'Active', op: 'eq', value: true }] });
    expect(screen.getByText('Custom filter')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add filter' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('an unreadable filter param and a stale column say so', () => {
    const { unmount } = setup(null, { invalidParam: true });
    expect(screen.getByText(/could not be read/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Clear' })).toBeTruthy();
    unmount();
    setup({ and: [{ column: 'Gone', op: 'is_null' }, { column: 'Name', op: 'is_null' }] });
    expect(screen.getByText('This filter references a column that no longer exists (Gone).')).toBeTruthy();
  });

  it('a server error path marks its chip; a root error marks none', () => {
    const filter: FilterNode = {
      and: [
        { column: 'Name', op: 'contains', value: 'x' },
        { column: 'Status', op: 'eq', value: 'Lead' },
      ],
    };
    const { rerender } = setup(filter, { error: { path: 'and[1].value', message: 'Nope.' } });
    expect(screen.getByRole('alert').textContent).toBe('This filter cannot be applied: Nope.');
    const marked = screen.getByRole('button', { name: 'Edit filter: Status is Lead (invalid)' });
    expect(marked.closest('.table-filter-chip')?.className).toContain('table-filter-chip--invalid');
    expect(marked.getAttribute('aria-describedby')).toBe(screen.getByRole('alert').id);
    expect(document.querySelectorAll('.table-filter-chip--invalid')).toHaveLength(1);

    rerender(<TableFilterBar columns={COLUMNS} filter={filter} onChange={vi.fn()} error={{ path: '', message: 'Too deep.' }} />);
    expect(screen.getByRole('alert').textContent).toContain('Too deep.');
    expect(document.querySelectorAll('.table-filter-chip--invalid')).toHaveLength(0);

    // A single (unwrapped) condition: the leaf is the root.
    rerender(
      <TableFilterBar
        columns={COLUMNS}
        filter={{ column: 'Status', op: 'eq', value: 'Lead' }}
        onChange={vi.fn()}
        error={{ path: 'value', message: 'Bad.' }}
      />,
    );
    expect(screen.getByRole('button', { name: 'Edit filter: Status is Lead (invalid)' })).toBeTruthy();

    // A sort error is not the bar's.
    rerender(<TableFilterBar columns={COLUMNS} filter={filter} onChange={vi.fn()} error={{ path: 'sort[0]', message: 'Bad sort.' }} />);
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
