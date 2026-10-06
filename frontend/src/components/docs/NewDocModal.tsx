/**
 * Modal for creating an empty Quest Doc from the All Docs view (POST /docs).
 * Mirrors NewProjectModal. A user doc ("Your docs") picks its mode; a
 * project doc always takes its project's mode, so the radio is replaced by
 * a read-only line and no `mode` is sent (a disagreeing one would 400
 * `project_doc_mode_inherited`). While the `public_projects` gate is closed
 * for the user, public docs are unavailable too: the radio is hidden, a
 * user doc is created with no `mode` (the server makes it private), and the
 * read-only line is left out for a private project (a public project's
 * line stays: a public state is never hidden).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createDoc } from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type { CreateDocRequest, Doc, DocMode, Project } from '../../api/types';
import { useAuth } from '../../contexts/AuthContext';
import { isPublicProjectsEnabled, shouldShowDocModeBadge } from '../../utils/docMode';
import { ModalShell } from '../ModalShell';
import './NewDocModal.css';

/** Server-side caps (POST /docs 400s invalid_title / invalid_description). */
const TITLE_MAX_LENGTH = 200;
const DESCRIPTION_MAX_LENGTH = 500;

/** Location select value for "Your docs" (a user doc, no project). */
const USER_DOCS_LOCATION = '';

interface NewDocModalProps {
  isOpen: boolean;
  onClose: () => void;
  projects: Project[];
  // Preselected location (the filtered project), or null for "Your docs".
  initialProjectId: string | null;
  onCreated: (doc: Doc) => void;
}

export function NewDocModal({
  isOpen,
  onClose,
  projects,
  initialProjectId,
  onCreated,
}: NewDocModalProps) {
  const { enabledFeatures } = useAuth();
  const publicDocsAvailable = isPublicProjectsEnabled(enabledFeatures);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [location, setLocation] = useState(USER_DOCS_LOCATION);
  const [mode, setMode] = useState<DocMode>('private');
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  // Non-archived projects by name. The preselected project stays offered
  // even when archived, so a filtered view of it still defaults to it.
  const locationProjects = useMemo(
    () =>
      projects
        .filter((p) => !p.archived || p.id === initialProjectId)
        .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase())),
    [projects, initialProjectId],
  );
  const initialLocation =
    initialProjectId && locationProjects.some((p) => p.id === initialProjectId)
      ? initialProjectId
      : USER_DOCS_LOCATION;

  // Reset the form and focus the title each time the modal opens.
  useEffect(() => {
    if (!isOpen) return;
    setTitle('');
    setDescription('');
    setLocation(initialLocation);
    setMode('private');
    setError(null);
    const timer = setTimeout(() => titleRef.current?.focus(), 50);
    return () => clearTimeout(timer);
    // initialLocation is read on open only; a later project-list reload
    // must not wipe what the user is typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  const selectedProject =
    location === USER_DOCS_LOCATION ? null : (projects.find((p) => p.id === location) ?? null);
  // A project doc's mode, shown read-only -- and, like every mode badge, not
  // at all for a private project while public docs are unavailable.
  const inheritedMode: DocMode = selectedProject?.public ? 'public' : 'private';

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const trimmedTitle = title.trim();
      if (!trimmedTitle || isCreating) return;

      const body: CreateDocRequest = { title: trimmedTitle };
      const trimmedDescription = description.trim();
      if (trimmedDescription) body.description = trimmedDescription;
      if (location !== USER_DOCS_LOCATION) {
        body.project_id = location;
      } else if (publicDocsAvailable) {
        body.mode = mode;
      }

      setIsCreating(true);
      setError(null);
      try {
        const doc = await createDoc(body);
        onCreated(doc);
        onClose();
      } catch (err) {
        setError(err instanceof ApiClientError ? err.message : 'Failed to create doc');
      } finally {
        setIsCreating(false);
      }
    },
    [title, description, location, mode, publicDocsAvailable, isCreating, onCreated, onClose],
  );

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      overlayClassName="new-doc-overlay"
      modalClassName="new-doc-modal"
    >
      <div className="new-doc-header">
        <h2>New Doc</h2>
        <button type="button" className="new-doc-close-button" onClick={onClose} aria-label="Close">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <form className="new-doc-body" onSubmit={handleSubmit}>
        <label htmlFor="new-doc-title-input" className="new-doc-label">
          Title
        </label>
        <input
          ref={titleRef}
          id="new-doc-title-input"
          type="text"
          className="new-doc-input"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="e.g. Meeting Notes, Launch Plan..."
          maxLength={TITLE_MAX_LENGTH}
          required
          disabled={isCreating}
        />

        <label htmlFor="new-doc-description-input" className="new-doc-label new-doc-label-spaced">
          Description <span className="new-doc-optional">(optional)</span>
        </label>
        <textarea
          id="new-doc-description-input"
          className="new-doc-input new-doc-textarea"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What this doc is for"
          maxLength={DESCRIPTION_MAX_LENGTH}
          rows={3}
          disabled={isCreating}
        />

        <label htmlFor="new-doc-location-select" className="new-doc-label new-doc-label-spaced">
          Location
        </label>
        <select
          id="new-doc-location-select"
          className="new-doc-input new-doc-select"
          value={location}
          onChange={(e) => setLocation(e.target.value)}
          disabled={isCreating}
        >
          <option value={USER_DOCS_LOCATION}>Your docs</option>
          {locationProjects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.archived ? `${p.name} (archived)` : p.name}
            </option>
          ))}
        </select>

        {location !== USER_DOCS_LOCATION ? (
          shouldShowDocModeBadge(inheritedMode, publicDocsAvailable) && (
            <p className="new-doc-mode-inherited">
              Mode: <strong>{inheritedMode === 'public' ? 'Public' : 'Private'}</strong> — inherited
              from the project
            </p>
          )
        ) : publicDocsAvailable ? (
          <fieldset className="new-doc-mode" disabled={isCreating}>
            <legend className="new-doc-label new-doc-label-spaced">Mode</legend>
            <label className="new-doc-mode-option">
              <input
                type="radio"
                name="new-doc-mode"
                value="private"
                checked={mode === 'private'}
                onChange={() => setMode('private')}
              />
              <span className="new-doc-mode-label">
                Private
                <span className="new-doc-mode-hint">
                  Only private conversations can read or change it.
                </span>
              </span>
            </label>
            <label className="new-doc-mode-option">
              <input
                type="radio"
                name="new-doc-mode"
                value="public"
                checked={mode === 'public'}
                onChange={() => setMode('public')}
              />
              <span className="new-doc-mode-label">
                Public
                <span className="new-doc-mode-hint">
                  Public conversations can read and change it; private conversations can only
                  read it.
                </span>
              </span>
            </label>
          </fieldset>
        ) : null}

        {error && (
          <div className="new-doc-error" role="alert">
            {error}
          </div>
        )}
        <div className="new-doc-actions">
          <button
            type="button"
            className="new-doc-cancel-button"
            onClick={onClose}
            disabled={isCreating}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="new-doc-create-button"
            disabled={!title.trim() || isCreating}
          >
            {isCreating ? 'Creating...' : 'Create Doc'}
          </button>
        </div>
      </form>
    </ModalShell>
  );
}
