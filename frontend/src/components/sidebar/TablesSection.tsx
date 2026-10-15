/**
 * A Sidebar "Tables" block: the user's most recent tables on the main panel
 * (right under Docs), or one project's tables inside the drill-down
 * (ProjectPanel, under its Docs block). A copy of DocsSection: the whole
 * header is a button that opens the All Tables view (filtered to the project
 * when drilled); a row opens that table's viewer. Data comes from useTables
 * (owned by the Sidebar, which renders this block only while the `tables`
 * feature gate is open) and row order from utils/allTablesGrouping; this
 * component only renders it. Only a public table's row carries a mode badge
 * (utils/docMode's rule). Styling reuses the docs block's classes
 * (Sidebar.css), so the two blocks read alike; the `tables-section` /
 * `table-row` classes identify this one.
 */

import { useMemo } from 'react';
import { Table2 } from 'lucide-react';
import type { TableListRow } from '../../api/types';
import { DocModeBadge } from '../docs/DocModeBadge';
import { shouldShowDocModeBadge } from '../../utils/docMode';
import { deriveSidebarTableItems } from '../../utils/allTablesGrouping';
import { formatSidebarDocTime, formatSidebarDocTimeTitle } from '../../utils/sidebarDocs';
import { ChevronRightIcon } from './icons';

export interface TablesSectionProps {
  tables: TableListRow[];
  // The first page has not landed yet.
  loading: boolean;
  // useTables().error: the first page failed to load (no tables to show).
  error?: string | null;
  // The table open in the viewer, so its row is highlighted.
  activeTableId: string | null;
  onOpenTable: (id: string) => void;
  onOpenAll: () => void;
  // The All Tables view this header opens is the one showing.
  headerActive?: boolean;
}

export function TablesSection({
  tables,
  loading,
  error = null,
  activeTableId,
  onOpenTable,
  onOpenAll,
  headerActive = false,
}: TablesSectionProps) {
  const items = useMemo(() => deriveSidebarTableItems(tables), [tables]);
  // A failed load is not "no tables": no create hint.
  const failed = !loading && Boolean(error) && tables.length === 0;

  return (
    <div className="docs-section tables-section">
      <div className="section-header docs-section-header">
        <button
          type="button"
          className={`docs-section-header-button${headerActive ? ' active' : ''}`}
          onClick={onOpenAll}
          title="All tables"
          aria-current={headerActive ? 'page' : undefined}
        >
          <span className="section-label">Tables</span>
          <ChevronRightIcon className="docs-section-chevron" />
        </button>
      </div>

      {items.map((table) => {
        const active = activeTableId === table.id;
        return (
          <button
            type="button"
            key={table.id}
            className={`conversation-item doc-row table-row${active ? ' active' : ''}`}
            onClick={() => onOpenTable(table.id)}
            title={table.title}
            aria-current={active ? 'page' : undefined}
          >
            <Table2 size={14} className="doc-row-icon" aria-hidden="true" />
            <span className="doc-row-title">{table.title}</span>
            {shouldShowDocModeBadge(table.mode) && (
              <DocModeBadge mode={table.mode} size="sm" noun="table" />
            )}
            <span className="doc-row-time" title={formatSidebarDocTimeTitle(table.updated_at)}>
              {formatSidebarDocTime(table.updated_at)}
            </span>
          </button>
        );
      })}

      {/* Quiet while the first page loads: no "Loading..." line that would
          flash before the rows or the empty state. */}
      {failed ? (
        <div className="sidebar-empty">Couldn't load tables</div>
      ) : (
        !loading && tables.length === 0 && (
          <div className="sidebar-empty">Ask Quest to create a table</div>
        )
      )}
    </div>
  );
}
