/**
 * Modal for creating a new routine within a project.
 */

import { useState, useCallback, useRef, useEffect } from 'react';
import { createRoutine, ApiClientError } from '../api/client';
import type { Guide } from '../api/types';
import { useConversationContext } from '../contexts/ConversationContext';
import { getSelectableModels } from '../constants/models';
import type { ModelInfo } from '../constants/models';
import { ModalShell } from './ModalShell';
import './NewRoutineModal.css';

interface NewRoutineModalProps {
  isOpen: boolean;
  projectId: string | null;
  /**
   * The routine is being created in a PUBLIC project: its runs are
   * public-project conversations, so only models the admin allows there
   * are offered and the guide override (never applied in public
   * conversations) is hidden.
   */
  isPublicProject?: boolean;
  onClose: () => void;
  onRoutineCreated: () => void;
}

const DEFAULT_ROUTINE_MODEL = 'gemini-3.5-flash-lite';

/** The usual routine default when offered, else the first offered model. */
function defaultRoutineModel(models: ModelInfo[]): string {
  if (models.some((m) => m.id === DEFAULT_ROUTINE_MODEL) || models.length === 0) {
    return DEFAULT_ROUTINE_MODEL;
  }
  return models[0].id;
}

export function NewRoutineModal({ isOpen, projectId, isPublicProject = false, onClose, onRoutineCreated }: NewRoutineModalProps) {
  const { guides, enabledFeatures } = useConversationContext();
  // Guide overrides exist only while the admin `guides` feature gate is on
  // for this user (POST /routines 403s a guide_id otherwise).
  const guidesEnabled = enabledFeatures.includes('guides') && !isPublicProject;
  const selectableModels = getSelectableModels(isPublicProject ? 'public' : 'private');

  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [guideId, setGuideId] = useState<string | null>(null);
  const [model, setModel] = useState<string>(DEFAULT_ROUTINE_MODEL);
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset form and focus input when modal opens
  useEffect(() => {
    if (isOpen) {
      setName('');
      setPrompt('');
      setGuideId(null);
      setModel(defaultRoutineModel(getSelectableModels(isPublicProject ? 'public' : 'private')));
      setError(null);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [isOpen, isPublicProject]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedPrompt = prompt.trim();
    if (!trimmedName || !trimmedPrompt || !projectId || isCreating) return;

    setIsCreating(true);
    setError(null);

    try {
      await createRoutine(projectId, {
        name: trimmedName,
        prompt: trimmedPrompt,
        guide_id: guidesEnabled ? guideId : null,
        model,
      });
      onRoutineCreated();
      onClose();
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to create routine');
      }
    } finally {
      setIsCreating(false);
    }
  }, [name, prompt, guideId, guidesEnabled, model, projectId, isCreating, onRoutineCreated, onClose]);

  if (!isOpen || !projectId) return null;

  return (
    <ModalShell isOpen={isOpen} onClose={onClose} overlayClassName="new-routine-overlay" modalClassName="new-routine-modal">
      <div className="new-routine-header">
        <h2>New Routine</h2>
        <button className="new-routine-close-button" onClick={onClose}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <form className="new-routine-body" onSubmit={handleSubmit}>
        <label htmlFor="new-routine-name-input" className="new-routine-label">
          Name
        </label>
        <input
          ref={inputRef}
          id="new-routine-name-input"
          type="text"
          className="new-routine-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g., Daily Standup Summary"
          maxLength={100}
          disabled={isCreating}
        />

        <label htmlFor="new-routine-prompt-input" className="new-routine-label">
          Prompt
        </label>
        <textarea
          id="new-routine-prompt-input"
          className="new-routine-textarea"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Enter the prompt to send when this routine runs..."
          rows={4}
          disabled={isCreating}
        />

        {guidesEnabled && (
          <>
            <label htmlFor="new-routine-guide-input" className="new-routine-label">
              Guide Override
              <span className="new-routine-label-hint">
                Optionally apply a guide (deprecated) to conversations this routine creates
              </span>
            </label>
            <select
              id="new-routine-guide-input"
              className="new-routine-select"
              value={guideId || ''}
              onChange={(e) => setGuideId(e.target.value || null)}
              disabled={isCreating}
            >
              <option value="">None</option>
              {guides
                .filter((g: Guide) => !g.is_default)
                .map((g: Guide) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
            </select>
          </>
        )}

        <label htmlFor="new-routine-model-input" className="new-routine-label">
          Model
        </label>
        <select
          id="new-routine-model-input"
          className="new-routine-select"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          disabled={isCreating}
        >
          {selectableModels.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>

        {error && <div className="new-routine-error">{error}</div>}

        <div className="new-routine-actions">
          <button
            type="button"
            className="new-routine-cancel-button"
            onClick={onClose}
            disabled={isCreating}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="new-routine-create-button"
            disabled={!name.trim() || !prompt.trim() || isCreating}
          >
            {isCreating ? 'Creating...' : 'Create Routine'}
          </button>
        </div>
      </form>
    </ModalShell>
  );
}
