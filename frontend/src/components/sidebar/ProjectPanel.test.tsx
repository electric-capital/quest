// ProjectPanel: the drill-down's section order is Routines, Docs, Tables,
// then the Conversations header, and each block follows its show flag.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { Project, TableListRow } from '../../api/types';
import { ProjectPanel } from './ProjectPanel';

// The FLIP animation measures layout and reads matchMedia, which jsdom lacks.
vi.mock('../../hooks/useFlipListAnimation', () => ({
  useFlipListAnimation: () => ({ current: null }),
}));

const PROJECT: Project = {
  id: 'p1',
  user_id: 1,
  name: 'Launch Plan',
  guide: '',
  public: false,
  archived: false,
  created_at: '2026-01-01T00:00:00',
  updated_at: null,
  conversation_count: 0,
};

function tableRow(id: string, day: number): TableListRow {
  return {
    id, owner_id: 1, project_id: 'p1', title: `Table ${id}`, description: '', mode: 'private',
    schema_version: 1, row_count: 0, last_write_source: null, require_approval: false,
    created_at: '2026-10-01T00:00:00', updated_at: `2026-10-0${day}T00:00:00`, scope: 'project',
    column_count: 1, shared: false, shared_with_me: false, permission: null, owner: null,
    shares: [], last_write_user: null,
    access: {
      can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
      can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
      can_require_approval: true, can_download_history: true, deny_reason: null,
      schema_deny_reason: null,
    },
  };
}

interface PanelProps {
  showDocs: boolean;
  showTables?: boolean;
  tables?: TableListRow[];
  activeTableId?: string | null;
  onOpenTable?: (id: string) => void;
  onOpenAllTables?: () => void;
  tablesHeaderActive?: boolean;
}

function renderPanel({ showDocs, ...tableProps }: PanelProps) {
  const noop = vi.fn();
  return render(
    <ProjectPanel
      project={PROJECT}
      conversations={[]}
      loading={false}
      routines={[]}
      showArchived={false}
      onShowArchivedChange={noop}
      showSlack={false}
      onShowSlackChange={noop}
      expandedRoutineGroups={{}}
      onToggleRoutineGroup={noop}
      activeConversationId={null}
      busy={false}
      onBack={noop}
      onOpenProjectSettings={noop}
      onNewRoutine={noop}
      onRoutineSettings={noop}
      onRunRoutine={noop}
      onNewChat={noop}
      onSelect={noop}
      updateConversations={noop}
      onArchived={noop}
      showDocs={showDocs}
      docs={[]}
      {...tableProps}
    />,
  );
}

/** True when `a` comes before `b` in document order. */
function precedes(a: Element, b: Element): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

describe('ProjectPanel', () => {
  afterEach(() => {
    cleanup();
  });

  it('orders Routines, then Docs, then Tables, then the Conversations header', () => {
    const { container } = renderPanel({ showDocs: true, showTables: true });

    const routines = container.querySelector('.routines-section');
    const docs = container.querySelector('.docs-section:not(.tables-section)');
    const tables = container.querySelector('.tables-section');
    const conversations = screen.getByText('Conversations');
    expect(routines).not.toBeNull();
    expect(docs).not.toBeNull();
    expect(tables).not.toBeNull();
    expect(precedes(routines!, docs!)).toBe(true);
    expect(precedes(docs!, tables!)).toBe(true);
    expect(precedes(tables!, conversations)).toBe(true);

    // The section labels read in the same order.
    expect(
      [...container.querySelectorAll('.section-label')].map((label) => label.textContent),
    ).toEqual(['Routines', 'Docs', 'Tables', 'Conversations']);
  });

  it('always shows Routines; leaves out the Docs and Tables blocks when their flags are off', () => {
    const { container } = renderPanel({ showDocs: false, showTables: false });
    expect(container.querySelector('.routines-section')).not.toBeNull();
    expect(container.querySelector('.docs-section')).toBeNull();
    expect(container.querySelector('.tables-section')).toBeNull();
    expect(screen.getByText('Conversations')).toBeTruthy();
  });

  it("lists the project's tables and wires their row and header callbacks", () => {
    const onOpenTable = vi.fn();
    const onOpenAllTables = vi.fn();
    const { container } = renderPanel({
      showDocs: false,
      showTables: true,
      tables: [tableRow('t1', 1), tableRow('t2', 2)],
      activeTableId: 't1',
      onOpenTable,
      onOpenAllTables,
      tablesHeaderActive: true,
    });
    expect(
      [...container.querySelectorAll('.table-row .doc-row-title')].map((el) => el.textContent),
    ).toEqual(['Table t2', 'Table t1']);
    expect(container.querySelector('.table-row.active')?.textContent).toContain('Table t1');

    fireEvent.click(screen.getByRole('button', { name: /Table t2/ }));
    expect(onOpenTable).toHaveBeenCalledWith('t2');
    const header = screen.getByTitle('All tables');
    expect(header.getAttribute('aria-current')).toBe('page');
    fireEvent.click(header);
    expect(onOpenAllTables).toHaveBeenCalledTimes(1);
  });
});
