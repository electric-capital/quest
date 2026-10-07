/**
 * Project Settings > Docs Access: which of the user's public projects this
 * private project's conversations may read Quest Docs from (read-only;
 * GET/PUT /projects/{id}/doc-sources). The candidate list is the user's own
 * public projects from ProjectsContext (archived ones included, flagged);
 * the saved list is a full replacement.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  fetchProjectDocSources,
  updateProjectDocSources,
  ApiClientError,
} from '../api/client';
import { useProjects } from '../contexts/ProjectsContext';

interface ProjectDocSourcesSectionProps {
  projectId: string;
  /** Whether the Quest Docs feature is on for this user (enabled_features). */
  docsEnabled: boolean;
}

function sameSet(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const set = new Set(a);
  return b.every((id) => set.has(id));
}

export function ProjectDocSourcesSection({ projectId, docsEnabled }: ProjectDocSourcesSectionProps) {
  const { projects } = useProjects();
  const [saved, setSaved] = useState<string[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  // Sources the server reports that are not in the context's project list
  // (e.g. the public_projects gate closed since): shown by id so the user
  // can still clear them.
  const [unknownSources, setUnknownSources] = useState<{ id: string; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saved'>('idle');

  const candidates = useMemo(
    () => projects.filter((p) => p.public && p.id !== projectId),
    [projects, projectId],
  );

  useEffect(() => {
    let cancelled = false;
    setSaved(null);
    setError(null);
    setSaveStatus('idle');
    fetchProjectDocSources(projectId)
      .then((res) => {
        if (cancelled) return;
        const ids = res.sources.map((s) => s.id);
        setSaved(ids);
        setSelected(ids);
        const known = new Set(projects.map((p) => p.id));
        setUnknownSources(res.sources.filter((s) => !known.has(s.id)).map((s) => ({ id: s.id, name: s.name })));
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof ApiClientError ? err.message : 'Failed to load docs access');
        setSaved([]);
      });
    return () => {
      cancelled = true;
    };
    // The candidate list only labels rows; re-fetching on every project
    // list refresh would discard unsaved checkbox changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const toggle = useCallback((id: string, checked: boolean) => {
    setSaveStatus('idle');
    setSelected((prev) => (checked ? [...prev.filter((x) => x !== id), id] : prev.filter((x) => x !== id)));
  }, []);

  const handleSave = useCallback(async () => {
    if (isSaving) return;
    setIsSaving(true);
    setError(null);
    try {
      const res = await updateProjectDocSources(projectId, selected);
      const ids = res.sources.map((s) => s.id);
      setSaved(ids);
      setSelected(ids);
      const known = new Set(projects.map((p) => p.id));
      setUnknownSources(res.sources.filter((s) => !known.has(s.id)).map((s) => ({ id: s.id, name: s.name })));
      setSaveStatus('saved');
    } catch (err) {
      setError(err instanceof ApiClientError ? err.message : 'Failed to save docs access');
    } finally {
      setIsSaving(false);
    }
  }, [projectId, selected, isSaving, projects]);

  const dirty = saved !== null && !sameSet(saved, selected);

  return (
    <div className="project-doc-sources">
      <h3>Docs Access</h3>
      <p className="project-doc-sources-intro">
        Let conversations in this project read the Quest Docs of your public
        projects. Access is read-only: conversations here can look things up
        in those docs but never change them, and public projects never see
        this project's docs.
      </p>
      {!docsEnabled && (
        <div className="project-settings-public-note">
          Quest Docs is turned off for your account, so these settings have
          no effect until it is enabled.
        </div>
      )}
      {error && <div className="project-settings-error">{error}</div>}
      {saved === null && !error ? (
        <div className="project-settings-loading">Loading docs access...</div>
      ) : candidates.length === 0 && unknownSources.length === 0 ? (
        <div className="project-doc-sources-empty">
          You have no public projects. Create one to give this project access to its docs.
        </div>
      ) : (
        <div className="project-doc-sources-list" role="group" aria-label="Public projects">
          {candidates.map((p) => (
            <label key={p.id} className="project-doc-source-row">
              <input
                type="checkbox"
                checked={selected.includes(p.id)}
                disabled={isSaving}
                onChange={(e) => toggle(p.id, e.target.checked)}
              />
              <span className="project-doc-source-name">{p.name}</span>
              {p.archived && <span className="project-doc-source-chip">Archived</span>}
            </label>
          ))}
          {unknownSources.map((s) => (
            <label key={s.id} className="project-doc-source-row">
              <input
                type="checkbox"
                checked={selected.includes(s.id)}
                disabled={isSaving}
                onChange={(e) => toggle(s.id, e.target.checked)}
              />
              <span className="project-doc-source-name">{s.name}</span>
              <span className="project-doc-source-chip">Not available</span>
            </label>
          ))}
        </div>
      )}
      <div className="project-settings-actions">
        <button
          className="project-settings-save-button"
          onClick={handleSave}
          disabled={isSaving || saved === null || !dirty}
        >
          {isSaving ? 'Saving...' : saveStatus === 'saved' && !dirty ? 'Saved!' : 'Save Changes'}
        </button>
      </div>
    </div>
  );
}
