/**
 * Modal for creating a new project
 */

import { useState, useCallback, useRef, useEffect } from 'react';
import { createProject, ApiClientError } from '../api/client';
import { ModalShell } from './ModalShell';
import './NewProjectModal.css';

interface NewProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  onProjectCreated: (projectId: string) => void;
}

export function NewProjectModal({ isOpen, onClose, onProjectCreated }: NewProjectModalProps) {
  const [name, setName] = useState('');
  const [isPublic, setIsPublic] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus input when modal opens
  useEffect(() => {
    if (isOpen) {
      setName('');
      setIsPublic(false);
      setError(null);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [isOpen]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || isCreating) return;

    setIsCreating(true);
    setError(null);

    try {
      const project = await createProject(trimmed, isPublic);
      onProjectCreated(project.id);
      onClose();
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to create project');
      }
    } finally {
      setIsCreating(false);
    }
  }, [name, isPublic, isCreating, onProjectCreated, onClose]);

  return (
    <ModalShell isOpen={isOpen} onClose={onClose} overlayClassName="new-project-overlay" modalClassName="new-project-modal">
      <div className="new-project-header">
        <h2>New Project</h2>
        <button className="new-project-close-button" onClick={onClose}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <form className="new-project-body" onSubmit={handleSubmit}>
        <label htmlFor="project-name-input" className="new-project-label">
          Project Name
        </label>
        <input
          ref={inputRef}
          id="project-name-input"
          type="text"
          className="new-project-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Research Notes, Client Work..."
          maxLength={100}
          disabled={isCreating}
        />
        <label className="new-project-public-row">
          <input
            type="checkbox"
            checked={isPublic}
            onChange={(e) => setIsPublic(e.target.checked)}
            disabled={isCreating}
          />
          <span className="new-project-public-label">
            Public project
            <span className="new-project-public-hint">
              Conversations get internet access from the code sandbox, but no
              access to your internal data or connected services (no email,
              Slack, memories, or skills). Cannot be changed later.
            </span>
          </span>
        </label>
        {error && <div className="new-project-error">{error}</div>}
        <div className="new-project-actions">
          <button
            type="button"
            className="new-project-cancel-button"
            onClick={onClose}
            disabled={isCreating}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="new-project-create-button"
            disabled={!name.trim() || isCreating}
          >
            {isCreating ? 'Creating...' : 'Create Project'}
          </button>
        </div>
      </form>
    </ModalShell>
  );
}
