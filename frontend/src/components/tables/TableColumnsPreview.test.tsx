// TableColumnsPreview: the create_table card's column table (snapshot from
// the docs/api/tables-api.md example payload) and its shared helpers.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { TableSchemaDiffColumn } from '../../api/types';
import { TABLE_CELL_MAX_CHARS } from '../../utils/tableCells';
import { TableColumnsPreview, previewText, tableValueText } from './TableColumnsPreview';

// create_table Preview `table_columns` payload: Column.to_dict() without key.
const COLUMNS: TableSchemaDiffColumn[] = [
  {
    name: 'Name', type: 'text', required: true, unique: true, default: null,
    options: null, multiple: false, precision: null, description: 'Who to contact',
  },
  {
    name: 'Status', type: 'enum', required: false, unique: false, default: 'Open',
    options: ['Open', 'Done'], multiple: false, precision: null, description: '',
  },
  {
    name: 'Budget', type: 'number', required: false, unique: false, default: '0',
    options: null, multiple: false, precision: 2, description: '',
  },
  {
    name: 'Tags', type: 'enum', required: false, unique: false, default: ['a', 'b'],
    options: ['a', 'b', 'c'], multiple: true, precision: null, description: '',
  },
];

describe('TableColumnsPreview', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the column table (snapshot)', () => {
    const { asFragment } = render(<TableColumnsPreview columns={COLUMNS} />);
    expect(asFragment()).toMatchSnapshot();
  });

  it('shows type labels (the editor vocabulary), flags, defaults, options and the description', () => {
    render(<TableColumnsPreview columns={COLUMNS} />);
    expect(screen.getByText('Text')).toBeTruthy();
    expect(screen.getByText('Choice')).toBeTruthy();
    expect(screen.getByText('Number (2 decimal places)')).toBeTruthy();
    expect(screen.getByText('Choice (several)')).toBeTruthy();
    expect(screen.getByText('a, b')).toBeTruthy();
    expect(screen.getByText('Who to contact')).toBeTruthy();
    expect(screen.getAllByText('yes')).toHaveLength(2);
    expect(screen.getByText('Done')).toBeTruthy();
  });

  it('scopes the header cells to their columns', () => {
    const { container } = render(<TableColumnsPreview columns={COLUMNS} />);
    const scopes = Array.from(container.querySelectorAll('thead th')).map((th) => th.getAttribute('scope'));
    expect(scopes).toEqual(['col', 'col', 'col', 'col', 'col', 'col']);
  });

  it('cuts a long default in the cell and its title', () => {
    const long = 'x'.repeat(TABLE_CELL_MAX_CHARS + 50);
    const { container } = render(
      <TableColumnsPreview columns={[{ ...COLUMNS[0], default: long, required: false, unique: false }]} />,
    );
    const cell = container.querySelector('.table-columns-default');
    const cut = `${'x'.repeat(TABLE_CELL_MAX_CHARS)}…`;
    expect(cell?.textContent).toBe(cut);
    expect(cell?.getAttribute('title')).toBe(cut);
  });

  it('never renders a column key', () => {
    const withKeys = COLUMNS.map((c, i) => ({ ...c, key: `c_0000000${i}` }));
    const { container } = render(<TableColumnsPreview columns={withKeys} />);
    expect(container.textContent).not.toMatch(/c_0/);
  });

  it('renders nothing for no columns', () => {
    const { container } = render(<TableColumnsPreview columns={[]} />);
    expect(container.innerHTML).toBe('');
  });
});

describe('tableValueText', () => {
  it('formats logical values and keeps null distinct from an empty string', () => {
    expect(tableValueText(null)).toBeNull();
    expect(tableValueText(undefined)).toBeNull();
    expect(tableValueText('')).toBe('');
    expect(tableValueText('x')).toBe('x');
    expect(tableValueText(true)).toBe('true');
    expect(tableValueText(false)).toBe('false');
    expect(tableValueText(['a', 'b'])).toBe('a, b');
    // An empty multi-enum list is no value, as in the viewer grid.
    expect(tableValueText([])).toBeNull();
    expect(tableValueText(3)).toBe('3');
  });
});

describe('previewText', () => {
  it('keeps short text and cuts long text at TABLE_CELL_MAX_CHARS code points', () => {
    expect(previewText('short')).toBe('short');
    expect(previewText('')).toBe('');
    expect(previewText('y'.repeat(TABLE_CELL_MAX_CHARS))).toBe('y'.repeat(TABLE_CELL_MAX_CHARS));
    expect(previewText('y'.repeat(TABLE_CELL_MAX_CHARS + 1))).toBe(`${'y'.repeat(TABLE_CELL_MAX_CHARS)}…`);
  });
});
