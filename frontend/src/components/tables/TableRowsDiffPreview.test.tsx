// TableRowsDiffPreview: snapshots of the write_table_rows card payloads
// (docs/api/tables-api.md upsert: one changed, one added row; a delete),
// the rowsDiffToGrid mapping, null vs empty, row status markers and the
// legend, long values cut, the truncation note, and no column key in the
// text.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { TableRowsDiff } from '../../api/types';
import { TABLE_CELL_MAX_CHARS } from '../../utils/tableCells';
import {
  NEW_ROW_LABEL,
  TableRowsDiffPreview,
  rowsDiffToGrid,
  rowsDiffTruncationNote,
} from './TableRowsDiffPreview';

const UPSERT_DIFF: TableRowsDiff = {
  columns: ['Name', 'Status'],
  rows: [
    {
      _id: 7, status: 'changed',
      before: { Name: 'Ada', Status: 'Open' },
      after: { Name: 'Ada', Status: 'Done' },
      changed_columns: ['Status'],
    },
    {
      _id: null, status: 'added', before: null,
      after: { Name: 'Lin', Status: 'Open' },
      changed_columns: [],
    },
  ],
  total: 2,
  truncated: false,
};

const DELETE_DIFF: TableRowsDiff = {
  columns: ['Name', 'Done', 'Tags'],
  rows: [
    {
      _id: 3, status: 'deleted',
      before: { Name: 'Old task', Done: true, Tags: ['a', 'b'] },
      after: null, changed_columns: [],
    },
    {
      _id: 9, status: 'deleted',
      before: { Name: '', Done: false, Tags: null },
      after: null, changed_columns: [],
    },
  ],
  total: 2,
  truncated: false,
};

describe('TableRowsDiffPreview', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the upsert card payload (snapshot)', () => {
    const { asFragment } = render(<TableRowsDiffPreview diff={UPSERT_DIFF} />);
    expect(asFragment()).toMatchSnapshot();
  });

  it('renders a delete (snapshot)', () => {
    const { asFragment } = render(<TableRowsDiffPreview diff={DELETE_DIFF} />);
    expect(asFragment()).toMatchSnapshot();
  });

  it('labels columns by name and rows by _id, with row status classes', () => {
    const { container } = render(<TableRowsDiffPreview diff={UPSERT_DIFF} />);
    expect(screen.getByText('Name')).toBeTruthy();
    expect(screen.getByText('7')).toBeTruthy();
    expect(screen.getByText(NEW_ROW_LABEL)).toBeTruthy();
    expect(container.querySelector('.sheet-diff-table.rows-variant')).toBeTruthy();
    expect(container.querySelector('tr.sheet-diff-row-changed')).toBeTruthy();
    expect(container.querySelector('tr.sheet-diff-row-added')).toBeTruthy();
    const changed = container.querySelector('.sheet-diff-cell.changed');
    expect(changed?.querySelector('.sheet-diff-old')?.textContent).toBe('Open');
    expect(changed?.querySelector('.sheet-diff-new')?.textContent).toBe('Done');
    expect(container.querySelectorAll('.sheet-diff-cell.changed')).toHaveLength(1);
  });

  it('marks each row label with its status and lists only the kinds present', () => {
    const { container } = render(<TableRowsDiffPreview diff={UPSERT_DIFF} />);
    const labels = Array.from(container.querySelectorAll('tbody th')).map((th) => th.textContent);
    expect(labels).toEqual(['~ 7 changed', '+ new added']);
    const legend = Array.from(container.querySelectorAll('.sheet-diff-legend-item'))
      .map((item) => item.textContent?.trim());
    expect(legend).toEqual(['added', 'changed']);
    cleanup();
    const { container: deleted } = render(<TableRowsDiffPreview diff={DELETE_DIFF} />);
    expect(Array.from(deleted.querySelectorAll('tbody th')).map((th) => th.textContent))
      .toEqual(['− 3 deleted', '− 9 deleted']);
    expect(Array.from(deleted.querySelectorAll('.sheet-diff-legend-item')).map((i) => i.textContent?.trim()))
      .toEqual(['deleted']);
  });

  it('cuts long values in the cell and its title, after the change test', () => {
    const prefix = 'p'.repeat(TABLE_CELL_MAX_CHARS);
    const diff: TableRowsDiff = {
      columns: ['Body'],
      rows: [{
        _id: 4, status: 'changed',
        // Equal for the first TABLE_CELL_MAX_CHARS code points, then differ.
        before: { Body: `${prefix}old tail` }, after: { Body: `${prefix}new tail` },
        changed_columns: [],
      }],
      total: 1,
      truncated: false,
    };
    const mapped = rowsDiffToGrid(diff);
    expect(mapped.grid.changed).toEqual([[true]]);
    expect(mapped.grid.current).toEqual([[`${prefix}…`]]);
    expect(mapped.grid.new).toEqual([[`${prefix}…`]]);
    const { container } = render(<TableRowsDiffPreview diff={diff} />);
    const oldSpan = container.querySelector('.sheet-diff-old');
    expect(oldSpan?.textContent).toBe(`${prefix}…`);
    expect(oldSpan?.getAttribute('title')).toBe(`${prefix}…`);
    expect(container.querySelector('.sheet-diff-new')?.getAttribute('title')).toBe(`${prefix}…`);
  });

  it('shows an empty multi-enum list as the NULL marker', () => {
    const { container } = render(<TableRowsDiffPreview diff={{
      columns: ['Tags'],
      rows: [{ _id: 2, status: 'deleted', before: { Tags: [] }, after: null, changed_columns: [] }],
      total: 1,
      truncated: false,
    }} />);
    expect(container.querySelector('td .sheet-diff-null')?.textContent).toBe('NULL');
  });

  it('shows a null as the NULL marker and an empty string as empty', () => {
    const { container } = render(<TableRowsDiffPreview diff={DELETE_DIFF} />);
    const cells = container.querySelectorAll('tr.sheet-diff-row-deleted')[1].querySelectorAll('td');
    expect(cells[0].textContent).toBe('');
    expect(cells[1].textContent).toBe('false');
    expect(cells[2].querySelector('.sheet-diff-null')?.textContent).toBe('NULL');
  });

  it('notes a truncated diff and an empty one', () => {
    render(<TableRowsDiffPreview diff={{ ...UPSERT_DIFF, total: 120, truncated: true }} />);
    expect(screen.getByText('Showing 2 of 120 rows')).toBeTruthy();
    cleanup();
    render(<TableRowsDiffPreview diff={{ columns: ['Name'], rows: [], total: 0, truncated: false }} />);
    expect(screen.getByText('No rows')).toBeTruthy();
  });

  it('never renders a column key', () => {
    // A History-mapped diff has names, but a stray key in a value map must
    // not surface: only `columns` entries are read.
    const diff: TableRowsDiff = {
      ...UPSERT_DIFF,
      rows: UPSERT_DIFF.rows.map((r) => ({
        ...r,
        after: r.after ? { ...r.after, c_12345678: 'x' } : null,
      })),
    };
    const { container } = render(<TableRowsDiffPreview diff={diff} />);
    expect(container.textContent).not.toMatch(/c_1234/);
  });
});

describe('rowsDiffToGrid', () => {
  it('maps changed and added rows', () => {
    const mapped = rowsDiffToGrid(UPSERT_DIFF);
    expect(mapped.colLabels).toEqual(['Name', 'Status']);
    expect(mapped.rowLabels).toEqual(['7', 'new']);
    expect(mapped.rowStatus).toEqual(['changed', 'added']);
    expect(mapped.grid).toEqual({
      start_row: 1,
      start_col: 1,
      target_start_row: 1,
      target_start_col: 1,
      target_end_row: 2,
      target_end_col: 2,
      current: [['Ada', 'Open'], ['Lin', 'Open']],
      new: [['Ada', 'Done'], ['Lin', 'Open']],
      changed: [[false, true], [false, false]],
    });
    expect(mapped.nullCells).toEqual({
      current: [[false, false], [false, false]],
      new: [[false, false], [false, false]],
    });
  });

  it('maps deleted rows to their before values, with text and null masks', () => {
    const mapped = rowsDiffToGrid(DELETE_DIFF);
    expect(mapped.rowLabels).toEqual(['3', '9']);
    expect(mapped.grid.current).toEqual([['Old task', 'true', 'a, b'], ['', 'false', '']]);
    expect(mapped.grid.new).toEqual(mapped.grid.current);
    expect(mapped.grid.changed).toEqual([[false, false, false], [false, false, false]]);
    expect(mapped.nullCells.new).toEqual([[false, false, false], [false, false, true]]);
  });

  it('marks a cell changed when its value differs even if changed_columns omits it', () => {
    const mapped = rowsDiffToGrid({
      columns: ['A', 'B'],
      rows: [{
        _id: 1, status: 'changed',
        before: { A: null, B: 'x' }, after: { A: '', B: 'x' }, changed_columns: [],
      }],
      total: 1,
      truncated: false,
    });
    expect(mapped.grid.changed).toEqual([[true, false]]);
    expect(mapped.nullCells.current).toEqual([[true, false]]);
    expect(mapped.nullCells.new).toEqual([[false, false]]);
  });
});

describe('rowsDiffTruncationNote', () => {
  it('names the shown and total counts only when truncated', () => {
    expect(rowsDiffTruncationNote(UPSERT_DIFF)).toBeNull();
    expect(rowsDiffTruncationNote({ ...UPSERT_DIFF, total: 75, truncated: true }))
      .toBe('Showing 2 of 75 rows');
  });
});
