// Typed cell renderers of the tables grid: decimal formatting at a column
// precision (pure string arithmetic, half-even), text truncation, url links,
// boolean labels, enum chips and NULL; the shared column type labels.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { TableCellValue, TableColumn } from '../api/types';
import {
  COLUMN_TYPE_LABELS,
  TABLE_CELL_MAX_CHARS,
  TableCell,
  columnTypeLabel,
  formatDecimal,
  isNullCell,
  truncateCellText,
  urlHost,
} from './tableCells';

function col(overrides: Partial<TableColumn> = {}): TableColumn {
  return {
    key: 'c_00000001',
    name: 'Col',
    type: 'text',
    required: false,
    unique: false,
    default: null,
    options: null,
    multiple: false,
    precision: null,
    description: '',
    ...overrides,
  };
}

function renderCell(column: TableColumn, value: TableCellValue | undefined) {
  render(
    <table>
      <tbody>
        <tr>
          <TableCell column={column} value={value} />
        </tr>
      </tbody>
    </table>,
  );
  return document.querySelector('td') as HTMLTableCellElement;
}

afterEach(() => cleanup());

describe('formatDecimal', () => {
  it.each([
    ['1.5', 2, '1.50'],
    ['2.345', 2, '2.34'],
    ['2.355', 2, '2.36'],
    ['2.3451', 2, '2.35'],
    ['2.344', 2, '2.34'],
    ['-0.005', 2, '0.00'],
    ['-0.015', 2, '-0.02'],
    ['-1.5', 0, '-2'],
    ['0.5', 0, '0'],
    ['1.5', 0, '2'],
    ['2.5', 0, '2'],
    ['9.995', 2, '10.00'],
    ['99.96', 1, '100.0'],
    ['0', 3, '0.000'],
    ['42', 2, '42.00'],
    ['-3', 1, '-3.0'],
    ['12345678901234567890.125', 2, '12345678901234567890.12'],
    ['0.0001', 10, '0.0001000000'],
  ])('%s at precision %i -> %s', (value, precision, expected) => {
    expect(formatDecimal(value, precision)).toBe(expected);
  });

  it('returns the canonical string unchanged without a precision', () => {
    expect(formatDecimal('1.25', null)).toBe('1.25');
    expect(formatDecimal('-7', undefined)).toBe('-7');
  });

  it('returns a non-decimal string unchanged', () => {
    expect(formatDecimal('1e5', 2)).toBe('1e5');
    expect(formatDecimal('abc', 2)).toBe('abc');
  });
});

describe('helpers', () => {
  it('truncateCellText cuts at the cap with an ellipsis, by code point', () => {
    const long = 'x'.repeat(TABLE_CELL_MAX_CHARS + 5);
    const cut = truncateCellText(long);
    expect(cut.truncated).toBe(true);
    expect(cut.display).toBe(`${'x'.repeat(TABLE_CELL_MAX_CHARS)}…`);
    expect(truncateCellText('short')).toEqual({ display: 'short', truncated: false });
    // Emoji are two UTF-16 units each: never split one.
    expect(truncateCellText('\u{1F600}\u{1F600}\u{1F600}', 2).display).toBe('\u{1F600}\u{1F600}…');
    expect(truncateCellText('\u{1F600}\u{1F600}', 2).truncated).toBe(false);
  });

  it('urlHost reads http(s) hosts only', () => {
    expect(urlHost('https://example.com:8080/a?b=1')).toBe('example.com:8080');
    expect(urlHost('javascript:alert(1)')).toBeNull();
    expect(urlHost('not a url')).toBeNull();
  });

  it('columnTypeLabel names every type in the one UI vocabulary', () => {
    expect(columnTypeLabel({ type: 'text' })).toBe('Text');
    expect(columnTypeLabel({ type: 'url', multiple: false, precision: null })).toBe('URL');
    expect(columnTypeLabel({ type: 'boolean' })).toBe('Checkbox');
    expect(columnTypeLabel({ type: 'enum', multiple: false })).toBe('Choice');
    expect(columnTypeLabel({ type: 'enum', multiple: true })).toBe('Choice (several)');
    expect(columnTypeLabel({ type: 'number', precision: null })).toBe('Number');
    expect(columnTypeLabel({ type: 'number', precision: 2 })).toBe('Number (2 decimal places)');
    expect(columnTypeLabel({ type: 'number', precision: 1 })).toBe('Number (1 decimal place)');
    expect(columnTypeLabel({ type: 'number', precision: 0 })).toBe('Number (0 decimal places)');
    // An unknown type shows as sent.
    expect(columnTypeLabel({ type: 'date' })).toBe('date');
    expect(Object.keys(COLUMN_TYPE_LABELS).sort()).toEqual(['boolean', 'enum', 'number', 'text', 'url']);
  });

  it('isNullCell treats null, missing and an empty multi-enum as no value', () => {
    expect(isNullCell(null)).toBe(true);
    expect(isNullCell(undefined)).toBe(true);
    expect(isNullCell([])).toBe(true);
    expect(isNullCell('')).toBe(false);
    expect(isNullCell(false)).toBe(false);
  });
});

describe('TableCell', () => {
  it('renders null as a muted NULL', () => {
    const td = renderCell(col(), null);
    expect(td.textContent).toBe('NULL');
    expect(td.className).toContain('table-cell--null');
  });

  it('renders text truncated with the full value in title', () => {
    const long = 'a'.repeat(TABLE_CELL_MAX_CHARS + 10);
    const td = renderCell(col(), long);
    expect(td.textContent).toBe(`${'a'.repeat(TABLE_CELL_MAX_CHARS)}…`);
    expect(td.getAttribute('title')).toBe(long);
    cleanup();
    const short = renderCell(col(), 'hi');
    expect(short.textContent).toBe('hi');
    expect(short.getAttribute('title')).toBeNull();
  });

  it('renders numbers right-aligned at the column precision', () => {
    const td = renderCell(col({ type: 'number', precision: 2 }), '1.5');
    expect(td.textContent).toBe('1.50');
    expect(td.className).toContain('table-cell--number');
    cleanup();
    expect(renderCell(col({ type: 'number' }), '1234567.5').textContent).toBe('1234567.5');
  });

  it('renders a url as a new-tab link showing the host', () => {
    renderCell(col({ type: 'url' }), 'https://example.com/path/to/page');
    const link = screen.getByRole('link', { name: 'example.com' });
    expect(link.getAttribute('href')).toBe('https://example.com/path/to/page');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(link.getAttribute('title')).toBe('https://example.com/path/to/page');
  });

  it('renders an unparseable url as text, never an href', () => {
    const td = renderCell(col({ type: 'url' }), 'javascript:alert(1)');
    expect(td.querySelector('a')).toBeNull();
    expect(td.textContent).toBe('javascript:alert(1)');
  });

  it('renders booleans as a glyph with a true / false label for screen readers', () => {
    expect(renderCell(col({ type: 'boolean' }), true).textContent).toBe('true');
    expect(document.querySelector('.table-cell-check--on')).not.toBeNull();
    cleanup();
    expect(renderCell(col({ type: 'boolean' }), false).textContent).toBe('false');
    expect(document.querySelector('.table-cell-check--on')).toBeNull();
  });

  it('renders enums as chips; an empty multi list is NULL', () => {
    renderCell(col({ type: 'enum', options: ['a', 'b'] }), 'a');
    expect(Array.from(document.querySelectorAll('.table-cell-chip')).map((c) => c.textContent)).toEqual(['a']);
    cleanup();
    const multi = renderCell(col({ type: 'enum', options: ['a', 'b'], multiple: true }), ['a', 'b']);
    expect(Array.from(document.querySelectorAll('.table-cell-chip')).map((c) => c.textContent)).toEqual(['a', 'b']);
    expect(multi.getAttribute('title')).toBe('a, b');
    cleanup();
    const empty = renderCell(col({ type: 'enum', options: ['a'], multiple: true }), []);
    expect(empty.textContent).toBe('NULL');
  });
});
