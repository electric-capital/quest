/**
 * A Sidebar "Docs" block: the user's most recent docs on the main panel, or
 * one project's docs inside the drill-down (ProjectPanel). The whole header
 * is a button that opens the All Docs view (filtered to the project when
 * drilled); a row opens that doc's viewer. Data comes from useDocs (owned by
 * the Sidebar) and row order from utils/sidebarDocs; this component only
 * renders it. Only a public doc's row carries a mode badge (utils/docMode).
 */

import { useMemo } from 'react';
import { FileText } from 'lucide-react';
import type { Doc } from '../../api/types';
import { DocModeBadge } from '../docs/DocModeBadge';
import { shouldShowDocModeBadge } from '../../utils/docMode';
import {
  deriveSidebarDocItems,
  docCountLabel,
  formatSidebarDocTime,
  formatSidebarDocTimeTitle,
} from '../../utils/sidebarDocs';
import { ChevronRightIcon } from './icons';

export interface DocsSectionProps {
  docs: Doc[];
  // The server has more docs than `docs` holds (count shows as "5+").
  hasMore: boolean;
  // The first page has not landed yet.
  loading: boolean;
  // useDocs().error: the first page failed to load (no docs to show).
  error?: string | null;
  // The doc open in the viewer, so its row is highlighted.
  activeDocId: string | null;
  onOpenDoc: (id: string) => void;
  onOpenAll: () => void;
  // The All Docs view this header opens is the one showing.
  headerActive?: boolean;
}

export function DocsSection({
  docs,
  hasMore,
  loading,
  error = null,
  activeDocId,
  onOpenDoc,
  onOpenAll,
  headerActive = false,
}: DocsSectionProps) {
  const items = useMemo(() => deriveSidebarDocItems(docs), [docs]);
  // A failed load is not "no docs": no "0" count and no create hint.
  const failed = !loading && Boolean(error) && docs.length === 0;

  return (
    <div className="docs-section">
      <div className="section-header docs-section-header">
        <button
          type="button"
          className={`docs-section-header-button${headerActive ? ' active' : ''}`}
          onClick={onOpenAll}
          title="All docs"
          aria-current={headerActive ? 'page' : undefined}
        >
          <span className="section-label">Docs</span>
          {/* No count until the first page lands, so it never flashes "0". */}
          {!loading && !failed && (
            <span className="docs-count">{docCountLabel(docs.length, hasMore)}</span>
          )}
          <ChevronRightIcon className="docs-section-chevron" />
        </button>
      </div>

      {items.map((doc) => {
        const active = activeDocId === doc.id;
        return (
          <button
            type="button"
            key={doc.id}
            className={`conversation-item doc-row${active ? ' active' : ''}`}
            onClick={() => onOpenDoc(doc.id)}
            title={doc.title}
            aria-current={active ? 'page' : undefined}
          >
            <FileText size={14} className="doc-row-icon" aria-hidden="true" />
            <span className="doc-row-title">{doc.title}</span>
            {shouldShowDocModeBadge(doc.mode) && (
              <DocModeBadge mode={doc.mode} size="sm" />
            )}
            <span className="doc-row-time" title={formatSidebarDocTimeTitle(doc.updated_at)}>
              {formatSidebarDocTime(doc.updated_at)}
            </span>
          </button>
        );
      })}

      {/* Quiet while the first page loads: no "Loading..." line that would
          flash before the rows or the empty state. */}
      {failed ? (
        <div className="sidebar-empty">Couldn't load docs</div>
      ) : (
        !loading && docs.length === 0 && (
          <div className="sidebar-empty">Ask Quest to create a doc</div>
        )
      )}
    </div>
  );
}
