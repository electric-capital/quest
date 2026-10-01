/**
 * Data layer for the Sidebar's per-project routine lists (keyed by project
 * id): on-demand loading plus realtime refresh when the server advertises a
 * routine-list mutation.
 */

import { useCallback, useEffect, useState } from 'react';
import { fetchProjectRoutines } from '../api/client';
import type { Routine } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

export interface ProjectRoutines {
  byProject: Record<string, Routine[]>;
  load: (projectId: string) => Promise<void>;
}

export function useProjectRoutines(): ProjectRoutines {
  const [byProject, setByProject] = useState<Record<string, Routine[]>>({});

  const load = useCallback(async (projectId: string) => {
    try {
      const response = await fetchProjectRoutines(projectId);
      setByProject((prev) => ({ ...prev, [projectId]: response.routines }));
    } catch (err) {
      console.error('Failed to load project routines:', err);
    }
  }, []);

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
