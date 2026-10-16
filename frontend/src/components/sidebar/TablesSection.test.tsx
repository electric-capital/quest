// TablesSection: the 5 newest tables, the active row, the header link, the
// empty / failed states (a public project's empty hint) and the public-only
// mode badge (a copy of DocsSection's rules).
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { TableListRow } from '../../api/types';
import {
  EMPTY_HINT,
  PUBLIC_PROJECT_EMPTY_HINT,
  TablesSection,
  type TablesSectionProps,
} from './TablesSection';

function tableRow(id: string, day: number, overrides: Partial<TableListRow> = {}): TableListRow {
  return {
    id,
    owner_id: 1,
    project_id: null,
    title: `Table ${id}`,
    description: '',
    mode: 'private',
    schema_version: 1,
    row_count: 3,
    last_write_source: null,
    require_approval: false,
    created_at: '2026-10-01T00:00:00',
    updated_at: `2026-10-${String(day).padStart(2, '0')}T00:00:00`,
    scope: 'user',
    column_count: 2,
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    shares: [],
    last_write_user: null,
    access: {
      can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
      can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
      can_require_approval: true, can_download_history: true, deny_reason: null,
      schema_deny_reason: null,
    },
    ...overrides,
  };
}

function renderSection(props: Partial<TablesSectionProps> = {}) {
  const onOpenTable = vi.fn();
  const onOpenAll = vi.fn();
  const utils = render(
    <TablesSection
      tables={[]}
      loading={false}
      activeTableId={null}
      onOpenTable={onOpenTable}
      onOpenAll={onOpenAll}
      {...props}
    />,
  );
  return { ...utils, onOpenTable, onOpenAll };
}

const rowTitles = (container: HTMLElement) =>
  [...container.querySelectorAll('.table-row .doc-row-title')].map((el) => el.textContent);

describe('TablesSection', () => {
  afterEach(() => {
    cleanup();
  });

  it('lists only the 5 newest tables, newest first', () => {
    const tables = [3, 9, 1, 7, 5, 8, 2].map((day) => tableRow(`t${day}`, day));
    const { container } = renderSection({ tables });
    expect(rowTitles(container)).toEqual(['Table t9', 'Table t8', 'Table t7', 'Table t5', 'Table t3']);
  });

  it('opens a table from its row and All Tables from the header', () => {
    const { onOpenTable, onOpenAll } = renderSection({ tables: [tableRow('t1', 1)] });
    fireEvent.click(screen.getByRole('button', { name: /Table t1/ }));
    expect(onOpenTable).toHaveBeenCalledWith('t1');
    fireEvent.click(screen.getByTitle('All tables'));
    expect(onOpenAll).toHaveBeenCalledTimes(1);
  });

  it('highlights the open table and the active header', () => {
    const { container } = renderSection({
      tables: [tableRow('t1', 1), tableRow('t2', 2)],
      activeTableId: 't1',
      headerActive: true,
    });
    const active = container.querySelectorAll('.table-row.active');
    expect(active).toHaveLength(1);
    expect(active[0].getAttribute('aria-current')).toBe('page');
    expect(active[0].textContent).toContain('Table t1');
    expect(screen.getByTitle('All tables').getAttribute('aria-current')).toBe('page');
  });

  it('shows the create hint when empty, nothing while loading, and a failure line', () => {
    renderSection();
    expect(screen.getByText(EMPTY_HINT)).toBeTruthy();
    expect(EMPTY_HINT).toBe('Ask Quest to create a table');
    cleanup();

    const { container } = renderSection({ loading: true });
    expect(container.querySelector('.sidebar-empty')).toBeNull();
    cleanup();

    renderSection({ error: 'boom' });
    expect(screen.getByText("Couldn't load tables")).toBeTruthy();
    expect(screen.queryByText(EMPTY_HINT)).toBeNull();
  });

  it("a public project's empty block does not send the user to ask Quest", () => {
    renderSection({ publicProject: true });
    expect(screen.getByText(PUBLIC_PROJECT_EMPTY_HINT)).toBeTruthy();
    expect(screen.queryByText(/Ask Quest/)).toBeNull();
    cleanup();
    // With tables, or failed: no hint either way.
    renderSection({ publicProject: true, tables: [tableRow('t1', 1, { project_id: 'p1', scope: 'project', mode: 'public' })] });
    expect(screen.queryByText(PUBLIC_PROJECT_EMPTY_HINT)).toBeNull();
    cleanup();
    renderSection({ publicProject: true, error: 'boom' });
    expect(screen.getByText("Couldn't load tables")).toBeTruthy();
    expect(screen.queryByText(PUBLIC_PROJECT_EMPTY_HINT)).toBeNull();
  });

  it('badges only a public table', () => {
    const { container } = renderSection({
      tables: [
        tableRow('t1', 2),
        tableRow('t2', 1, { project_id: 'p1', scope: 'project', mode: 'public' }),
      ],
    });
    const badges = [...container.querySelectorAll('.table-row')].map(
      (row) => row.querySelector('.doc-mode-badge')?.textContent ?? null,
    );
    expect(badges).toEqual([null, 'Public']);
  });
});
