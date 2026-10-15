// TableGrid: the sort cycle, the pager math (last page, empty table, a total
// shrinking below the offset), header aria-sort and clicks, empty states.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { SortKey, TableColumn, TableRowsPage } from '../../api/types';
import { TableGrid, clampOffset, nextSort, pageRange } from './TableGrid';

const COLUMNS: TableColumn[] = [
  { key: 'c_a', name: 'Name', type: 'text', required: false, unique: false, default: null, options: null, multiple: false, precision: null, description: '' },
  { key: 'c_b', name: 'Amount', type: 'number', required: false, unique: false, default: null, options: null, multiple: false, precision: 2, description: '' },
];

function page(overrides: Partial<TableRowsPage> = {}): TableRowsPage {
  return {
    rows: [
      { _id: 1, _updated_at: '2026-10-01T00:00:00', c_a: 'Alpha', c_b: '1.5' },
      { _id: 2, _updated_at: '2026-10-01T00:00:00', c_a: null, c_b: null },
    ],
    total: 2,
    limit: 200,
    offset: 0,
    truncated: false,
    schema_version: 1,
    ...overrides,
  };
}

afterEach(() => cleanup());

describe('nextSort', () => {
  it('cycles none -> asc -> desc -> none on one column', () => {
    const asc = nextSort(null, 'Name');
    expect(asc).toEqual({ column: 'Name', direction: 'asc' });
    const desc = nextSort(asc, 'Name');
    expect(desc).toEqual({ column: 'Name', direction: 'desc' });
    expect(nextSort(desc, 'Name')).toBeNull();
  });

  it('starts ascending on another column', () => {
    expect(nextSort({ column: 'Name', direction: 'desc' }, 'Amount')).toEqual({ column: 'Amount', direction: 'asc' });
  });
});

describe('pageRange', () => {
  it('first page of many', () => {
    expect(pageRange(0, 200, 350)).toEqual({
      from: 1, to: 200, total: 350, hasPrev: false, hasNext: true, prevOffset: 0, nextOffset: 200,
    });
  });

  it('last page', () => {
    const r = pageRange(200, 200, 350);
    expect([r.from, r.to, r.hasPrev, r.hasNext, r.prevOffset]).toEqual([201, 350, true, false, 0]);
  });

  it('exactly one full page', () => {
    const r = pageRange(0, 200, 200);
    expect([r.from, r.to, r.hasPrev, r.hasNext]).toEqual([1, 200, false, false]);
  });

  it('empty table', () => {
    const r = pageRange(0, 200, 0);
    expect([r.from, r.to, r.hasPrev, r.hasNext]).toEqual([0, 0, false, false]);
  });

  it('an offset past the total shows no range', () => {
    const r = pageRange(400, 200, 350);
    expect([r.from, r.to, r.hasPrev, r.hasNext]).toEqual([0, 0, true, false]);
  });
});

describe('clampOffset', () => {
  it('keeps an offset inside the rows', () => {
    expect(clampOffset(200, 200, 350)).toBe(200);
    expect(clampOffset(0, 200, 1)).toBe(0);
  });

  it('steps back to the last page when the total shrinks below the offset', () => {
    expect(clampOffset(400, 200, 350)).toBe(200);
    expect(clampOffset(200, 200, 200)).toBe(0);
    expect(clampOffset(600, 200, 401)).toBe(400);
  });

  it('is 0 for an empty result', () => {
    expect(clampOffset(400, 200, 0)).toBe(0);
  });
});

function renderGrid(props: Partial<React.ComponentProps<typeof TableGrid>> = {}) {
  const handlers = { onSortChange: vi.fn(), onOffsetChange: vi.fn() };
  render(
    <TableGrid
      columns={COLUMNS}
      page={page()}
      loading={false}
      sort={null}
      offset={0}
      {...handlers}
      {...props}
    />,
  );
  return handlers;
}

describe('TableGrid', () => {
  it('renders the row id label, column names and typed cells', () => {
    renderGrid();
    const headers = screen.getAllByRole('columnheader').map((th) => th.textContent);
    expect(headers).toEqual(['#', 'Name', 'Amount']);
    expect(screen.getAllByRole('rowheader').map((th) => th.textContent)).toEqual(['1', '2']);
    expect(screen.getByText('1.50')).toBeTruthy();
    expect(screen.getAllByText('NULL')).toHaveLength(2);
  });

  it('marks the sorted column with aria-sort and reports the next sort on click', () => {
    const sort: SortKey = { column: 'Name', direction: 'asc' };
    const { onSortChange } = renderGrid({ sort });
    const [idTh, nameTh, amountTh] = screen.getAllByRole('columnheader');
    // Only the sorted column carries aria-sort.
    expect(idTh.hasAttribute('aria-sort')).toBe(false);
    expect(nameTh.getAttribute('aria-sort')).toBe('ascending');
    expect(amountTh.hasAttribute('aria-sort')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Name' }));
    expect(onSortChange).toHaveBeenCalledWith({ column: 'Name', direction: 'desc' });
    fireEvent.click(screen.getByRole('button', { name: 'Sort by row number' }));
    expect(onSortChange).toHaveBeenLastCalledWith({ column: '_id', direction: 'asc' });
  });

  it('shows the pager with boundary-disabled buttons', () => {
    const { onOffsetChange } = renderGrid({ page: page({ total: 350 }), offset: 0 });
    expect(screen.getByText('Rows 1-200 of 350')).toBeTruthy();
    const prev = screen.getByRole('button', { name: 'Prev' }) as HTMLButtonElement;
    const next = screen.getByRole('button', { name: 'Next' }) as HTMLButtonElement;
    expect(prev.disabled).toBe(true);
    expect(next.disabled).toBe(false);
    fireEvent.click(next);
    expect(onOffsetChange).toHaveBeenCalledWith(200);
  });

  it('says the table is empty, or that nothing matches a filter', () => {
    renderGrid({ page: page({ rows: [], total: 0 }) });
    expect(screen.getByText('This table has no rows yet.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull();
    cleanup();
    renderGrid({ page: page({ rows: [], total: 0 }), filterActive: true });
    expect(screen.getByText('No rows match this filter.')).toBeTruthy();
  });

  it('dims the previous page while a refetch is in flight', () => {
    renderGrid({ loading: true });
    expect(document.querySelector('.table-grid-scroll.is-refetching')).not.toBeNull();
    expect(screen.getByText('Alpha')).toBeTruthy();
  });
});
