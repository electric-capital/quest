/**
 * Modal for creating a new project out of an existing standalone conversation.
 * Reuses the NewProjectModal styles so the two dialogs stay visually in sync.
 */

import { useState, useCallback, useRef, useEffect } from 'react';
import { createProjectFromConversation, ApiClientError } from '../api/client';
import { ModalShell } from './ModalShell';
import './NewProjectModal.css';

interface ConvertToProjectModalProps {
  isOpen: boolean;
  conversationId: string;
  conversationTitle: string;
  onClose: () => void;
  onConverted: (projectId: string, conversationId: string) => void;
}

export function ConvertToProjectModal({
  isOpen,
  conversationId,
  conversationTitle,
  onClose,
  onConverted,
}: ConvertToProjectModalProps) {
  const [name, setName] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Prefill with the conversation title and focus when the modal opens
  useEffect(() => {
    if (isOpen) {
      setName(conversationTitle === 'New Chat' ? '' : conversationTitle.slice(0, 100));
      setError(null);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [isOpen, conversationTitle]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || isCreating) return;

    setIsCreating(true);
    setError(null);

    try {
      const project = await createProjectFromConversation(conversationId, trimmed);
      onConverted(project.id, conversationId);
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
  }, [name, isCreating, conversationId, onConverted, onClose]);

  return (
    <ModalShell isOpen={isOpen} onClose={onClose} overlayClassName="new-project-overlay" modalClassName="new-project-modal">
      <div className="new-project-header">
        <h2>Create Project from Chat</h2>
        <button className="new-project-close-button" onClick={onClose}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <form className="new-project-body" onSubmit={handleSubmit}>
        <p className="new-project-description">
          This creates a new project from this chat, and the chat becomes
          the project's first conversation. Your files stay with this chat;
          the project starts with an empty Project Files space. Ask the chat,
          or use Move to project, to share files with later chats.
        </p>
        <label htmlFor="convert-project-name-input" className="new-project-label">
          Project Name
        </label>
        <input
          ref={inputRef}
          id="convert-project-name-input"
          type="text"
          className="new-project-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Research Notes, Client Work..."
          maxLength={100}
          disabled={isCreating}
        />
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
