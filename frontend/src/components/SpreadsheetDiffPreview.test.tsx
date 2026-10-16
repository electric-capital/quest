// SpreadsheetDiffPreview: the edit_google_spreadsheet card's grid-only
// rendering is pinned by a snapshot captured before the optional table
// props (labels, row status, null masks, rows variant) existed, so it must
// stay byte-identical; the optional props are covered after it.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import type { SpreadsheetDiffGrid } from '../api/types';
import {
  ROWS_CORNER_LABEL,
  SPREADSHEET_DIFF_ROW_MARKS,
  SpreadsheetDiffPreview,
} from './SpreadsheetDiffPreview';

// B2:C3 replaced, one row / column of context around it (A1:D4).
const GRID: SpreadsheetDiffGrid = {
  start_row: 1,
  start_col: 1,
  target_start_row: 2,
  target_start_col: 2,
  target_end_row: 3,
  target_end_col: 3,
  current: [
    ['a1', 'b1', 'c1', 'd1'],
    ['a2', 'old', 'same', 'd2'],
    ['a3', '', 'gone', 'd3'],
    ['a4', 'b4', 'c4', 'd4'],
  ],
  new: [
    ['new', 'same'],
    ['added', ''],
  ],
  changed: [
    [true, false],
    [true, true],
  ],
};

describe('SpreadsheetDiffPreview', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders a grid-only (spreadsheet) diff byte-identically', () => {
    const { container } = render(<SpreadsheetDiffPreview grid={GRID} />);
    expect(container.innerHTML).toMatchSnapshot();
  });

  it('renders nothing for an empty grid', () => {
    const { container } = render(
      <SpreadsheetDiffPreview grid={{ ...GRID, current: [], new: [], changed: [] }} />,
    );
    expect(container.innerHTML).toBe('');
  });

  it('uses optional labels, row status, null masks and the rows variant', () => {
    const grid: SpreadsheetDiffGrid = {
      start_row: 1, start_col: 1,
      target_start_row: 1, target_start_col: 1, target_end_row: 2, target_end_col: 2,
      current: [['a', ''], ['x', '']],
      new: [['b', ''], ['x', '']],
      changed: [[true, true], [false, false]],
    };
    const { container } = render(
      <SpreadsheetDiffPreview
        grid={grid}
        colLabels={['Name', 'Note']}
        rowLabels={['7', 'new']}
        rowStatus={['changed', 'added']}
        nullCells={{ current: [[false, true], [false, false]], new: [[false, false], [false, true]] }}
        variant="rows"
      />,
    );
    expect(container.querySelector('table')?.className).toBe('sheet-diff-table rows-variant');
    const heads = Array.from(container.querySelectorAll('thead th'));
    expect(heads.map((th) => th.textContent)).toEqual([ROWS_CORNER_LABEL, 'Name', 'Note']);
    expect(heads[0].querySelector('.sheet-diff-sr-only')?.textContent).toBe(ROWS_CORNER_LABEL);
    expect(heads.map((th) => th.getAttribute('scope'))).toEqual(['col', 'col', 'col']);
    // The whole grid is the target: no in-target header tint.
    expect(container.querySelector('.in-target')).toBeNull();
    const rows = container.querySelectorAll('tbody tr');
    expect(rows[0].className).toBe('sheet-diff-row-changed');
    expect(rows[1].className).toBe('sheet-diff-row-added');
    // Row labels: visible marker, label, status word for screen readers.
    const label0 = rows[0].querySelector('th');
    expect(label0?.getAttribute('scope')).toBe('row');
    expect(label0?.textContent).toBe('~ 7 changed');
    expect(label0?.querySelector('.sheet-diff-row-mark')?.getAttribute('aria-hidden')).toBe('true');
    expect(label0?.querySelector('.sheet-diff-sr-only')?.textContent).toBe(' changed');
    expect(rows[1].querySelector('th')?.textContent).toBe('+ new added');
    // Changed cell: old and new as two spans (stacked by the stylesheet).
    const changed = rows[0].querySelectorAll('td')[0];
    expect(changed.querySelector('.sheet-diff-old')?.textContent).toBe('a');
    expect(changed.querySelector('.sheet-diff-new')?.textContent).toBe('b');
    // null -> '' change: the old side is the NULL marker, the new side empty.
    const nullToEmpty = rows[0].querySelectorAll('td')[1];
    expect(nullToEmpty.querySelector('.sheet-diff-old .sheet-diff-null')).toBeTruthy();
    expect(nullToEmpty.querySelector('.sheet-diff-new')?.textContent).toBe('(empty)');
    // Unchanged null cell.
    expect(rows[1].querySelectorAll('td')[1].textContent).toBe('NULL');
    // The legend lists only the row kinds present, in a fixed order.
    const legend = Array.from(container.querySelectorAll('.sheet-diff-legend-item'))
      .map((item) => item.textContent?.trim());
    expect(legend).toEqual(['added', 'changed']);
    expect(container.querySelector('.sheet-diff-swatch.row-changed')).toBeTruthy();
  });

  it('marks deleted rows with the minus marker', () => {
    const grid: SpreadsheetDiffGrid = {
      start_row: 1, start_col: 1,
      target_start_row: 1, target_start_col: 1, target_end_row: 1, target_end_col: 1,
      current: [['gone']], new: [['gone']], changed: [[false]],
    };
    const { container } = render(
      <SpreadsheetDiffPreview grid={grid} rowLabels={['3']} rowStatus={['deleted']} variant="rows" />,
    );
    expect(container.querySelector('tbody th')?.textContent)
      .toBe(`${SPREADSHEET_DIFF_ROW_MARKS.deleted} 3 deleted`);
    expect(container.querySelector('tr.sheet-diff-row-deleted')).toBeTruthy();
    expect(Array.from(container.querySelectorAll('.sheet-diff-legend-item')).map((i) => i.textContent?.trim()))
      .toEqual(['deleted']);
  });

  it('omits the legend of a rows grid without row status', () => {
    const grid: SpreadsheetDiffGrid = {
      start_row: 1, start_col: 1,
      target_start_row: 1, target_start_col: 1, target_end_row: 1, target_end_col: 1,
      current: [['x']], new: [['x']], changed: [[false]],
    };
    const { container } = render(<SpreadsheetDiffPreview grid={grid} variant="rows" />);
    expect(container.querySelector('.sheet-diff-legend')).toBeNull();
    expect(container.querySelector('tbody th')?.textContent).toBe('1');
  });
});
