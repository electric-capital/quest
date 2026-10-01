/**
 * Project Tables panel component for browsing tables in a project's SQLite database
 */

import { useState, useEffect, useCallback } from 'react';
import { errorMessage } from '../utils/errorMessage';
import { fetchProjectTables, deleteProjectTable } from '../api/projectDbApi';
import { webSocketManager } from '../services/WebSocketManager';
import { TableViewerModal } from './TableViewerModal';
import type { ProjectTable } from '../api/types';
import './ProjectTables.css';

interface ProjectTablesProps {
  projectId: string | null;
}

export function ProjectTables({ projectId }: ProjectTablesProps) {
  const [tables, setTables] = useState<ProjectTable[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewerTable, setViewerTable] = useState<string | null>(null);
  const [openMenuTableName, setOpenMenuTableName] = useState<string | null>(null);

  const loadTables = useCallback(async (silent = false) => {
    if (!projectId) return;

    if (!silent) {
      setLoading(true);
    }
    setError(null);

    try {
      const data = await fetchProjectTables(projectId);
      setTables(data.tables);
    } catch (err) {
      setError(errorMessage(err, 'Failed to load tables'));
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [projectId]);

  // Load tables on mount and when projectId changes
  useEffect(() => {
    if (projectId) {
      loadTables();
    } else {
      setTables([]);
    }
  }, [projectId, loadTables]);

  // Auto-refresh when the assistant turn ends. The companion ``onToolResult``
  // hook was always dead (no BE caller invoked ``publishToolResult``) and was
  // dropped along with the file-browser migration to ``file_list_changed``;
  // a dedicated project-DB event is tracked separately.
  useEffect(() => {
    if (!projectId) return;
    const unsubStream = webSocketManager.onStreamComplete(() => {
      loadTables(true);
    });
    return () => {
      unsubStream();
    };
  }, [projectId, loadTables]);

  // Close meatball menu on click outside
  useEffect(() => {
    if (!openMenuTableName) return;
    const handleClickOutside = () => setOpenMenuTableName(null);
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, [openMenuTableName]);

  if (!projectId) {
    return null;
  }

  return (
    <div className="project-tables">
      <div className="project-tables-header">
        <h3>Project Tables</h3>
        <button
          className="project-tables-refresh-button"
          onClick={() => loadTables()}
          disabled={loading && tables.length === 0}
          title="Refresh"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M23 4v6h-6" />
            <path d="M1 20v-6h6" />
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
          </svg>
        </button>
      </div>

      {error && (
        <div className="project-tables-error">{error}</div>
      )}

      <div className="project-tables-list">
        {loading && tables.length === 0 ? (
          <div className="project-tables-loading">Loading...</div>
        ) : tables.length === 0 ? (
          <div className="project-tables-empty">
            <p>No tables yet</p>
          </div>
        ) : (
          tables.map((table) => (
            <div
              key={table.name}
              className="project-tables-item"
              onClick={() => setViewerTable(table.name)}
            >
              <div className="project-tables-icon">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
                  <line x1="3" y1="9" x2="21" y2="9" />
                  <line x1="3" y1="15" x2="21" y2="15" />
                  <line x1="9" y1="3" x2="9" y2="21" />
                </svg>
              </div>
              <span className="project-tables-name" title={table.name}>
                {table.name}
              </span>
              <div className="project-tables-menu-container">
                <button
                  className="project-tables-menu-button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setOpenMenuTableName(
                      openMenuTableName === table.name ? null : table.name
                    );
                  }}
                  title="More options"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <circle cx="12" cy="5" r="2"></circle>
                    <circle cx="12" cy="12" r="2"></circle>
                    <circle cx="12" cy="19" r="2"></circle>
                  </svg>
                </button>
                {openMenuTableName === table.name && (
                  <div className="project-tables-menu-dropdown">
                    <button
                      className="project-tables-menu-item project-tables-menu-item-danger"
                      onClick={async (e) => {
                        e.stopPropagation();
                        setOpenMenuTableName(null);
                        if (!window.confirm(`Delete table "${table.name}"? This cannot be undone.`)) return;
                        try {
                          await deleteProjectTable(projectId, table.name);
                          if (viewerTable === table.name) {
                            setViewerTable(null);
                          }
                          await loadTables();
                        } catch (err) {
                          setError(errorMessage(err, 'Failed to delete table'));
                        }
                      }}
                    >
                      Delete table
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))
        )}
      </div>

      {projectId && viewerTable && (
        <TableViewerModal
          isOpen={!!viewerTable}
          projectId={projectId}
          tableName={viewerTable}
          onClose={() => setViewerTable(null)}
        />
      )}
    </div>
  );
}
