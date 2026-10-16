// TableSchemaDiffPreview: snapshots of the change_table_schema card payload
// (docs/api/tables-api.md: State -> Status with Done -> Closed, 4 cells
// rewritten) and of a diff with added / dropped / moved / order_changed;
// the line and chip helpers; header accessibility; keys never shown
// (History-style diff); the unchanged-column fold.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { TableSchemaDiff, TableSchemaDiffColumn } from '../../api/types';
import { TABLE_CELL_MAX_CHARS } from '../../utils/tableCells';
import {
  STATUS_HEADER_LABEL,
  TableSchemaDiffPreview,
  UNCHANGED_COLLAPSE_AT,
  schemaChangeLines,
  schemaOptionChips,
} from './TableSchemaDiffPreview';

function col(name: string, extra: Partial<TableSchemaDiffColumn> = {}): TableSchemaDiffColumn {
  return {
    name, type: 'text', required: false, unique: false, default: null,
    options: null, multiple: false, precision: null, description: '', ...extra,
  };
}

const RENAME_DIFF: TableSchemaDiff = {
  columns: [{
    status: 'renamed',
    moved: false,
    before: col('State', { type: 'enum', options: ['Open', 'Done'] }),
    after: col('Status', { type: 'enum', options: ['Open', 'Closed'] }),
    changes: {
      name: { before: 'State', after: 'Status' },
      options: { before: ['Open', 'Done'], after: ['Open', 'Closed'] },
    },
    options_added: ['Closed'],
    options_removed: ['Done'],
    cells_rewritten: 4,
    options_renamed: [{ option: 'Done', new_option: 'Closed' }],
  }],
  order_changed: false,
  before_order: ['State'],
  after_order: ['Status'],
  changed: true,
  cells_rewritten_total: 4,
};

// Before: Name, Notes, Due, Owner. After: Owner moved first, Notes dropped,
// Priority added, Due made required with a default.
const MIXED_DIFF: TableSchemaDiff = {
  columns: [
    {
      status: 'unchanged', moved: true, before: col('Owner'), after: col('Owner'),
      changes: {}, options_added: [], options_removed: [],
      cells_rewritten: 0, options_renamed: [],
    },
    {
      status: 'unchanged', moved: false, before: col('Name'), after: col('Name'),
      changes: {}, options_added: [], options_removed: [],
      cells_rewritten: 0, options_renamed: [],
    },
    {
      status: 'dropped', moved: false, before: col('Notes'), after: null,
      changes: {}, options_added: [], options_removed: [],
      cells_rewritten: 0, options_renamed: [],
    },
    {
      status: 'changed', moved: false,
      before: col('Due', { type: 'number', precision: 2 }),
      after: col('Due', { type: 'number', precision: 0, required: true, default: '1', description: 'Days' }),
      changes: {
        precision: { before: 2, after: 0 },
        required: { before: false, after: true },
        default: { before: null, after: '1' },
        description: { before: '', after: 'Days' },
      },
      options_added: [], options_removed: [],
      cells_rewritten: 0, options_renamed: [],
    },
    {
      status: 'added', moved: false, before: null,
      after: col('Priority', { type: 'enum', options: ['Low', 'High'], multiple: true }),
      changes: {}, options_added: [], options_removed: [],
      cells_rewritten: 0, options_renamed: [],
    },
  ],
  order_changed: true,
  before_order: ['Name', 'Notes', 'Due', 'Owner'],
  after_order: ['Owner', 'Name', 'Due', 'Priority'],
  changed: true,
  cells_rewritten_total: 0,
};

describe('TableSchemaDiffPreview', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the rename card payload (snapshot)', () => {
    const { asFragment } = render(<TableSchemaDiffPreview diff={RENAME_DIFF} />);
    expect(asFragment()).toMatchSnapshot();
  });

  it('renders added / dropped / moved / order changed (snapshot)', () => {
    const { asFragment } = render(<TableSchemaDiffPreview diff={MIXED_DIFF} />);
    expect(asFragment()).toMatchSnapshot();
  });

  it('shows the rename as one chip and the rewrite counts', () => {
    render(<TableSchemaDiffPreview diff={RENAME_DIFF} />);
    expect(screen.getByText('Done → Closed')).toBeTruthy();
    expect(screen.queryByText('+ Closed')).toBeNull();
    expect(screen.queryByText('- Done')).toBeNull();
    expect(screen.getByText('4 cells rewritten')).toBeTruthy();
    // The total is the card's own "Cells rewritten" field, not repeated here.
    expect(screen.queryByText(/in total/)).toBeNull();
    expect(screen.getByText('renamed')).toBeTruthy();
  });

  it('labels and scopes the header cells', () => {
    const { container } = render(<TableSchemaDiffPreview diff={RENAME_DIFF} />);
    const heads = Array.from(container.querySelectorAll('thead th'));
    expect(heads.map((th) => th.textContent)).toEqual([STATUS_HEADER_LABEL, 'Before', 'After']);
    expect(heads[0].querySelector('.table-schema-diff-sr-only')?.textContent).toBe(STATUS_HEADER_LABEL);
    expect(heads.map((th) => th.getAttribute('scope'))).toEqual(['col', 'col', 'col']);
  });

  it('shows only "moved" for a column that just moved', () => {
    const { container } = render(<TableSchemaDiffPreview diff={MIXED_DIFF} />);
    const badgeCells = container.querySelectorAll('.table-schema-diff-badges');
    // Owner: unchanged but moved.
    expect(badgeCells[0].textContent).toBe('moved');
    // Name: unchanged, unmoved.
    expect(badgeCells[1].textContent).toBe('unchanged');
    expect(screen.getAllByText('unchanged')).toHaveLength(1);
  });

  it('shows the order note, the moved badge and the field lines', () => {
    render(<TableSchemaDiffPreview diff={MIXED_DIFF} />);
    expect(screen.getByText('Column order changed: Owner, Name, Due, Priority')).toBeTruthy();
    expect(screen.getByText('moved')).toBeTruthy();
    expect(screen.getByText('Decimal places: 2 → 0')).toBeTruthy();
    expect(screen.getByText('Required: no → yes')).toBeTruthy();
    expect(screen.getByText('Default: none → 1')).toBeTruthy();
    expect(screen.getByText('Description added')).toBeTruthy();
    expect(screen.getByText('Choice (several)')).toBeTruthy();
    expect(screen.queryByText(/rewritten/)).toBeNull();
  });

  it('never renders a column key (History-style diff with keys)', () => {
    const withKeys: TableSchemaDiff = {
      ...MIXED_DIFF,
      columns: MIXED_DIFF.columns.map((e, i) => {
        const key = `c_abcdef0${i}`;
        const { cells_rewritten: _c, options_renamed: _o, ...rest } = e;
        void _c; void _o;
        return {
          ...rest,
          key,
          before: e.before ? { ...e.before, key } : null,
          after: e.after ? { ...e.after, key } : null,
        };
      }),
    };
    delete withKeys.cells_rewritten_total;
    const { container } = render(<TableSchemaDiffPreview diff={withKeys} />);
    expect(container.textContent).not.toMatch(/c_abcdef/);
    expect(screen.getByText('Decimal places: 2 → 0')).toBeTruthy();
  });

  it('folds many unchanged columns behind a toggle', () => {
    const quiet = Array.from({ length: UNCHANGED_COLLAPSE_AT + 1 }, (_, i) => ({
      status: 'unchanged' as const, moved: false, before: col(`Col${i}`), after: col(`Col${i}`),
      changes: {}, options_added: [], options_removed: [],
    }));
    const diff: TableSchemaDiff = {
      columns: [...quiet, RENAME_DIFF.columns[0]],
      order_changed: false, before_order: [], after_order: [], changed: true,
    };
    render(<TableSchemaDiffPreview diff={diff} />);
    expect(screen.queryAllByText('Col0')).toHaveLength(0);
    const toggle = screen.getByRole('button', { name: `Show ${UNCHANGED_COLLAPSE_AT + 1} unchanged columns` });
    fireEvent.click(toggle);
    expect(screen.getAllByText('Col0')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Hide unchanged columns' })).toBeTruthy();
  });

  it('keeps a few unchanged columns inline and notes a diff without changes', () => {
    const diff: TableSchemaDiff = {
      columns: [{
        status: 'unchanged', moved: false, before: col('Name'), after: col('Name'),
        changes: {}, options_added: [], options_removed: [],
      }],
      order_changed: false, before_order: ['Name'], after_order: ['Name'], changed: false,
    };
    render(<TableSchemaDiffPreview diff={diff} />);
    expect(screen.getAllByText('Name')).toHaveLength(2);
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByText('No column changes')).toBeTruthy();
  });
});

describe('schemaChangeLines', () => {
  it('formats field changes in a fixed order and skips name and options', () => {
    expect(schemaChangeLines({
      changes: {
        description: { before: 'a', after: 'b' },
        default: { before: 'Open', after: 'Done' },
        name: { before: 'A', after: 'B' },
        options: { before: ['x'], after: ['y'] },
        unique: { before: true, after: false },
        type: { before: 'text', after: 'enum' },
        multiple: { before: false, after: true },
      },
    })).toEqual([
      'Type: Text → Choice',
      'Several options: no → yes',
      'Unique: yes → no',
      'Default: Open → Done',
      'Description changed',
    ]);
  });

  it('cuts a long value at TABLE_CELL_MAX_CHARS', () => {
    const long = 'd'.repeat(TABLE_CELL_MAX_CHARS + 10);
    expect(schemaChangeLines({ changes: { default: { before: null, after: long } } }))
      .toEqual([`Default: none → ${'d'.repeat(TABLE_CELL_MAX_CHARS)}…`]);
  });

  it('formats null, empty, boolean and list values', () => {
    expect(schemaChangeLines({
      changes: {
        precision: { before: null, after: 3 },
        default: { before: ['a', 'b'], after: '' },
      },
    })).toEqual(['Decimal places: any → 3', 'Default: a, b → ""']);
    expect(schemaChangeLines({ changes: { default: { before: true, after: false } } }))
      .toEqual(['Default: true → false']);
    expect(schemaChangeLines({ changes: { description: { before: 'x', after: '' } } }))
      .toEqual(['Description removed']);
    expect(schemaChangeLines({ changes: { width: { before: 1, after: 2 } } }))
      .toEqual(['Width changed']);
    expect(schemaChangeLines({ changes: {} })).toEqual([]);
  });
});

describe('schemaOptionChips', () => {
  it('turns a covered add/remove pair into a rename', () => {
    expect(schemaOptionChips({
      options_added: ['Closed', 'Later'],
      options_removed: ['Done', 'Never'],
      options_renamed: [{ option: 'Done', new_option: 'Closed' }],
    })).toEqual([
      { kind: 'renamed', label: 'Done → Closed' },
      { kind: 'added', label: '+ Later' },
      { kind: 'removed', label: '- Never' },
    ]);
  });

  it('collapses chained renames and drops a round trip', () => {
    expect(schemaOptionChips({
      options_added: ['C'],
      options_removed: ['A'],
      options_renamed: [{ option: 'A', new_option: 'B' }, { option: 'B', new_option: 'C' }],
    })).toEqual([{ kind: 'renamed', label: 'A → C' }]);
    expect(schemaOptionChips({
      options_added: [],
      options_removed: [],
      options_renamed: [{ option: 'A', new_option: 'B' }, { option: 'B', new_option: 'A' }],
    })).toEqual([]);
  });

  it('does not credit a re-added name with a later rename (A → B, add A, A → C)', () => {
    expect(schemaOptionChips({
      before: { options: ['A'] },
      after: { options: ['B', 'C'] },
      options_added: ['B', 'C'],
      options_removed: ['A'],
      options_renamed: [{ option: 'A', new_option: 'B' }, { option: 'A', new_option: 'C' }],
    })).toEqual([
      { kind: 'renamed', label: 'A → B' },
      { kind: 'added', label: '+ C' },
    ]);
  });

  it('still shows a rename whose old name was re-added', () => {
    // rename A -> B, then add A: A is in neither added nor removed.
    expect(schemaOptionChips({
      before: { options: ['A', 'X'] },
      after: { options: ['B', 'X', 'A'] },
      options_added: ['B'],
      options_removed: [],
      options_renamed: [{ option: 'A', new_option: 'B' }],
    })).toEqual([
      { kind: 'renamed', label: 'A → B' },
      { kind: 'added', label: '+ A' },
    ]);
  });

  it('shows a swap through a temporary name as two renames', () => {
    expect(schemaOptionChips({
      before: { options: ['A', 'B'] },
      after: { options: ['B', 'A'] },
      options_added: [],
      options_removed: [],
      options_renamed: [
        { option: 'A', new_option: 'T' },
        { option: 'B', new_option: 'A' },
        { option: 'T', new_option: 'B' },
      ],
    })).toEqual([
      { kind: 'renamed', label: 'A → B' },
      { kind: 'renamed', label: 'B → A' },
    ]);
  });

  it('notes an original option whose name a rename took over', () => {
    // remove B, then rename A -> B.
    expect(schemaOptionChips({
      before: { options: ['A', 'B'] },
      after: { options: ['B'] },
      options_added: [],
      options_removed: ['A'],
      options_renamed: [{ option: 'A', new_option: 'B' }],
    })).toEqual([
      { kind: 'renamed', label: 'A → B' },
      { kind: 'removed', label: '- B' },
    ]);
  });

  it('skips a rename whose target is gone after (renamed, then removed)', () => {
    expect(schemaOptionChips({
      before: { options: ['A', 'Z'] },
      after: { options: ['Z'] },
      options_added: [],
      options_removed: ['A'],
      options_renamed: [{ option: 'A', new_option: 'B' }],
    })).toEqual([{ kind: 'removed', label: '- A' }]);
  });

  it('shows plain adds and removes without renames (History diff)', () => {
    expect(schemaOptionChips({ options_added: ['X'], options_removed: ['Y'] })).toEqual([
      { kind: 'added', label: '+ X' },
      { kind: 'removed', label: '- Y' },
    ]);
  });
});
