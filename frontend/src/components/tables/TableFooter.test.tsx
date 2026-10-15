// TableFooter: the "Last written by" label per source, the updated time,
// row count (with matches under a filter) and the schema version.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { TableDetail } from '../../api/types';
import { TableFooter } from './TableFooter';

function detail(overrides: Partial<TableDetail> = {}): TableDetail {
  return {
    id: 't1', owner_id: 1, project_id: null, title: 'Leads', description: '', mode: 'private',
    schema_version: 4, row_count: 350, last_write_source: null, require_approval: false,
    created_at: '2026-10-01T00:00:00', updated_at: new Date().toISOString().replace('Z', ''),
    scope: 'user', column_count: 2, shared: false, shared_with_me: false, permission: null,
    owner: null, shares: [], last_write_user: null,
    access: {
      can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
      can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
      can_require_approval: true, can_download_history: true, deny_reason: null,
      schema_deny_reason: null,
    },
    schema: [], last_write_conversation: null,
    ...overrides,
  };
}

function footerText(table: TableDetail, matchingCount?: number | null): string {
  render(
    <MemoryRouter>
      <TableFooter table={table} matchingCount={matchingCount} />
    </MemoryRouter>,
  );
  const text = document.querySelector('footer')?.textContent ?? '';
  cleanup();
  return text;
}

afterEach(() => cleanup());

describe('TableFooter', () => {
  it('omits the writer for a blank source (non-owners) and shows count and schema', () => {
    const text = footerText(detail());
    expect(text).not.toContain('Last written by');
    expect(text).toMatch(/^Updated .+ · 350 rows · Schema v4$/);
  });

  it('labels each source kind', () => {
    expect(footerText(detail({ last_write_source: 'ui:1' }))).toContain('Last written by you');
    expect(
      footerText(detail({ last_write_source: 'ui:7', last_write_user: { id: 7, name: 'Ana', email: null } })),
    ).toContain('Last written by Ana');
    expect(footerText(detail({ last_write_source: 'ui:7' }))).toContain('Last written by a deleted user');
    expect(footerText(detail({ last_write_source: 'action_request:9' }))).toContain(
      'Last written by an approved card',
    );
    expect(
      footerText(detail({ last_write_source: 'action_request:9', last_write_user: { id: 7, name: 'Ana', email: null } })),
    ).toContain('Last written by an approved card from Ana');
    expect(footerText(detail({ last_write_source: 'revert:12' }))).toContain('Last written by a restore');
    expect(footerText(detail({ last_write_source: 'conversation:c1' }))).toContain(
      'Last written by a conversation',
    );
    expect(footerText(detail({ last_write_source: 'something:else' }))).not.toContain('Last written by');
  });

  it('links a known conversation, under its project when it has one', () => {
    render(
      <MemoryRouter>
        <TableFooter
          table={detail({
            last_write_source: 'conversation:c1',
            last_write_conversation: { id: 'c1', title: 'Import leads', project_id: 'p1' },
          })}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: 'Import leads' }).getAttribute('href')).toBe('/projects/p1/c1');
  });

  it('adds the matching count while a filter is applied; one row reads singular', () => {
    expect(footerText(detail(), 12)).toContain('350 rows (12 matching)');
    expect(footerText(detail({ row_count: 1 }))).toContain('1 row ·');
  });
});
