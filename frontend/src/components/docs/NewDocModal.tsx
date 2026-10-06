/**
 * Modal for creating an empty Quest Doc from the All Docs view (POST /docs).
 * Mirrors NewProjectModal. There is no mode picker and no `mode` is ever
 * sent: a user doc ("Your docs") is always private, and a project doc takes
 * its project's mode (a disagreeing one would 400
 * `project_doc_mode_inherited`). A public project's doc says so in a
 * read-only line; a private one gets none, like every private doc has no
 * mode badge (utils/docMode).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createDoc } from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type { CreateDocRequest, Doc, DocMode, Project } from '../../api/types';
import { shouldShowDocModeBadge } from '../../utils/docMode';
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
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [location, setLocation] = useState(USER_DOCS_LOCATION);
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
    setError(null);
    const timer = setTimeout(() => titleRef.current?.focus(), 50);
    return () => clearTimeout(timer);
    // initialLocation is read on open only; a later project-list reload
    // must not wipe what the user is typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  const selectedProject =
    location === USER_DOCS_LOCATION ? null : (projects.find((p) => p.id === location) ?? null);
  // The mode a project doc inherits; only a public one is shown.
  const inheritedMode: DocMode = selectedProject?.public ? 'public' : 'private';

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const trimmedTitle = title.trim();
      if (!trimmedTitle || isCreating) return;

      const body: CreateDocRequest = { title: trimmedTitle };
      const trimmedDescription = description.trim();
      if (trimmedDescription) body.description = trimmedDescription;
      if (location !== USER_DOCS_LOCATION) body.project_id = location;

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
    [title, description, location, isCreating, onCreated, onClose],
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

        {selectedProject && shouldShowDocModeBadge(inheritedMode) && (
          <p className="new-doc-mode-inherited">
            Mode: <strong>Public</strong> — inherited from the project
          </p>
        )}

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
