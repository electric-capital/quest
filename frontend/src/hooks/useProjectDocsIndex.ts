/**
 * Per-project doc lists for the unfiltered All Docs view. The backend has no
 * cross-project list (GET /docs without project_id returns user docs only),
 * so this fans out one `fetchDocs({projectId, limit: 200})` per non-archived
 * project in parallel. A project whose fetch fails is reported in
 * `failedProjectIds` (its group is simply missing) instead of failing the
 * whole view.
 *
 * Kept current by the `doc_list_changed` realtime global, the same signal
 * useDocs listens to. Refreshes are silent: the index already on screen
 * stays until the new one lands, and a project whose refresh fails keeps
 * the docs it had.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DOCS_MAX_PAGE_SIZE, fetchDocs } from '../api/docsApi';
import type { Doc, Project } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

export interface ProjectDocsIndex {
  byProject: Record<string, Doc[]>;
  // True only until the first index for the enabled hook lands; a project
  // set change or a refresh keeps the previous index on screen.
  loading: boolean;
  failedProjectIds: string[];
}

interface IndexState {
  // Project-id set (sorted, joined) the index was loaded for; null = never.
  key: string | null;
  byProject: Record<string, Doc[]>;
  failedProjectIds: string[];
}

const EMPTY_STATE: IndexState = { key: null, byProject: {}, failedProjectIds: [] };
const EMPTY_INDEX: ProjectDocsIndex = { byProject: {}, loading: false, failedProjectIds: [] };

// Project ids never contain a newline, so it is a safe join separator.
const SEP = '\n';

export function useProjectDocsIndex(projects: Project[], enabled: boolean): ProjectDocsIndex {
  // Keyed on the sorted id set, not the array identity: ProjectsContext
  // hands out a new array on every reload (and on renames), which must not
  // trigger a re-fetch unless a project was added, removed or (un)archived.
  const idsKey = enabled
    ? projects
        .filter((p) => !p.archived)
        .map((p) => p.id)
        .sort()
        .join(SEP)
    : null;

  const [state, setState] = useState<IndexState>(EMPTY_STATE);
  // Written in step with setState so a refresh can merge onto the latest
  // index even before a re-render.
  const stateRef = useRef<IndexState>(EMPTY_STATE);
  const commit = useCallback((next: IndexState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  // Bumped by every load and by disabling; a response whose number is no
  // longer current is dropped, so an older fan-out never overwrites a newer.
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    if (idsKey === null) return;
    const seq = ++seqRef.current;
    const projectIds = idsKey ? idsKey.split(SEP) : [];
    const results = await Promise.allSettled(
      projectIds.map((projectId) => fetchDocs({ projectId, limit: DOCS_MAX_PAGE_SIZE })),
    );
    if (seq !== seqRef.current) return;

    const previous = stateRef.current.byProject;
    const byProject: Record<string, Doc[]> = {};
    const failedProjectIds: string[] = [];
    results.forEach((result, i) => {
      const projectId = projectIds[i];
      if (result.status === 'fulfilled') {
        byProject[projectId] = result.value.docs;
      } else if (previous[projectId]) {
        // A failed refresh keeps what is on screen for this project.
        byProject[projectId] = previous[projectId];
      } else {
        failedProjectIds.push(projectId);
      }
    });
    commit({ key: idsKey, byProject, failedProjectIds });
  }, [idsKey, commit]);

  // First load on mount / enable, and again whenever the project set changes.
  useEffect(() => {
    if (idsKey === null) {
      // Drop any in-flight fan-out from while the hook was enabled.
      seqRef.current += 1;
      commit(EMPTY_STATE);
      return;
    }
    void load();
  }, [idsKey, load, commit]);

  // Realtime: any doc create / rename / mode switch / delete / model write.
  useEffect(() => {
    if (idsKey === null) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'doc_list_changed') return;
      void load();
    });
  }, [idsKey, load]);

  return useMemo(() => {
    if (idsKey === null) return EMPTY_INDEX;
    // While a new project set loads, hide groups of projects no longer in
    // it (deleted or archived since the last load).
    const current = new Set(idsKey ? idsKey.split(SEP) : []);
    const byProject: Record<string, Doc[]> = {};
    for (const [projectId, docs] of Object.entries(state.byProject)) {
      if (current.has(projectId)) byProject[projectId] = docs;
    }
    return {
      byProject,
      loading: state.key === null,
      failedProjectIds: state.failedProjectIds.filter((id) => current.has(id)),
    };
  }, [idsKey, state]);
}
