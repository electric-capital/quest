/**
 * Data layer for the Sidebar's per-project routine lists (keyed by project
 * id): loading for the drilled project, on-demand reloads, plus realtime
 * refresh when the server advertises a routine-list mutation.
 */

import { useCallback, useEffect, useState } from 'react';
import { fetchProjectRoutines } from '../api/client';
import type { Routine } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

export interface ProjectRoutines {
  byProject: Record<string, Routine[]>;
  load: (projectId: string) => Promise<void>;
}

export function useProjectRoutines(drilledProjectId: string | null): ProjectRoutines {
  const [byProject, setByProject] = useState<Record<string, Routine[]>>({});

  const load = useCallback(async (projectId: string) => {
    try {
      const response = await fetchProjectRoutines(projectId);
      setByProject((prev) => ({ ...prev, [projectId]: response.routines }));
    } catch (err) {
      console.error('Failed to load project routines:', err);
    }
  }, []);

  // Fetch the drilled project's routines whenever they aren't cached. Keyed
  // on the drilled id rather than the drill-down click so a remount (the
  // Sidebar is re-created when the viewport crosses the phone breakpoint,
  // while drilledProjectId survives in ProjectsContext) reloads them too --
  // without routines the project's routine runs render ungrouped.
  const loaded = Boolean(drilledProjectId && byProject[drilledProjectId]);
  useEffect(() => {
    if (drilledProjectId && !loaded) {
      void load(drilledProjectId);
    }
  }, [drilledProjectId, loaded, load]);

  // Persistent WS: re-fetch a project's routines when the server advertises
  // a routine-list mutation (agent-driven create_routine / edit_routine
  // action request approved). The modal flows reload directly via the
  // Sidebar's routine-created/updated handlers; this covers writes the
  // sidebar didn't initiate, in this tab or any other.
  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'routine_list_changed') return;
      const projectId = event.project_id as string | undefined;
      if (projectId) {
        void load(projectId);
      }
    });
  }, [load]);

  return { byProject, load };
}
