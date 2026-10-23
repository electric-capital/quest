/**
 * The tables of a project's legacy project database, for the right panel.
 *
 * The project database is deprecated: a project keeps it only while it
 * already holds tables (the backend gates the tool and the prompt on the
 * same rule). The panel therefore shows the Tables card only when this list
 * is non-empty, so the fetch lives here, above the card, rather than inside
 * it. The list reloads when the project changes and at the end of every
 * assistant turn (`webSocketManager.onStreamComplete`); there is no
 * project-database mutation event to refresh on mid-turn.
 */

import { useCallback, useEffect, useState } from 'react';
import { fetchProjectTables } from '../api/projectDbApi';
import type { ProjectTable } from '../api/types';
import { errorMessage } from '../utils/errorMessage';
import { webSocketManager } from '../services/WebSocketManager';

export interface LegacyProjectTablesState {
  /** The tables of `projectId`; empty until the first load answers. */
  tables: ProjectTable[];
  /** A non-silent load is in flight. */
  loading: boolean;
  error: string | null;
  /** Reload the list; `silent` keeps the current rows on screen meanwhile. */
  reload: (silent?: boolean) => Promise<void>;
  /** Surface (or clear) an error from an action on a table, e.g. a failed delete. */
  setError: (message: string | null) => void;
}

interface Loaded {
  projectId: string;
  tables: ProjectTable[];
}

export function useLegacyProjectTables(projectId: string | null): LegacyProjectTablesState {
  // Keyed by project so a project switch never shows the previous project's
  // tables while the new list loads.
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async (silent = false) => {
    if (!projectId) return;
    if (!silent) setLoading(true);
    setError(null);
    try {
      const data = await fetchProjectTables(projectId);
      setLoaded({ projectId, tables: data.tables });
    } catch (err) {
      setError(errorMessage(err, 'Failed to load tables'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (!projectId) {
      setLoaded(null);
      setError(null);
      return;
    }
    void reload();
    const unsubscribe = webSocketManager.onStreamComplete(() => {
      void reload(true);
    });
    return unsubscribe;
  }, [projectId, reload]);

  const tables = loaded && loaded.projectId === projectId ? loaded.tables : [];
  return { tables, loading, error, reload, setError };
}
