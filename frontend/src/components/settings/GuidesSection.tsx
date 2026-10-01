import { useState, useEffect, useCallback, useRef } from 'react';
import { errorMessage } from '../../utils/errorMessage';
import { fetchGuides, updateGuide, deleteGuide, convertGuideToSkill } from '../../api/client';
import type { Guide } from '../../api/types';
import './GuidesSection.css';

interface GuidesSectionProps {
  onGuidesChanged: () => void;
  onNavigateToSkills: () => void;
}

export function GuidesSection({ onGuidesChanged, onNavigateToSkills }: GuidesSectionProps) {
  const [guidesList, setGuidesList] = useState<Guide[]>([]);
  const [isLoadingGuides, setIsLoadingGuides] = useState(false);
  const [editingGuideId, setEditingGuideId] = useState<string | null>(null);
  const [editingGuideName, setEditingGuideName] = useState('');
  const [editingGuideContent, setEditingGuideContent] = useState('');
  const [convertingGuideId, setConvertingGuideId] = useState<string | null>(null);
  const [guideSaveStatus, setGuideSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [guideSaveMessage, setGuideSaveMessage] = useState('');
  const guideSaveTimeoutRef = useRef<number | null>(null);

  // Load guides on mount
  useEffect(() => {
    const loadGuidesData = async () => {
      setIsLoadingGuides(true);
      try {
        const response = await fetchGuides();
        setGuidesList(response.guides);
      } catch (error) {
        console.error('Failed to load guides:', error);
      } finally {
        setIsLoadingGuides(false);
      }
    };

    loadGuidesData();
  }, []);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (guideSaveTimeoutRef.current) clearTimeout(guideSaveTimeoutRef.current);
    };
  }, []);

  const showStatus = useCallback((status: 'saved' | 'error', message: string, durationMs: number) => {
    setGuideSaveStatus(status);
    setGuideSaveMessage(message);
    if (guideSaveTimeoutRef.current) clearTimeout(guideSaveTimeoutRef.current);
    guideSaveTimeoutRef.current = window.setTimeout(() => {
      setGuideSaveStatus('idle');
      setGuideSaveMessage('');
    }, durationMs);
  }, []);

  const handleUpdateGuide = useCallback(async (guideId: string) => {
    setGuideSaveStatus('saving');
    setGuideSaveMessage('');
    try {
      const updates: { name?: string; content?: string } = { content: editingGuideContent };
      // Only send name if it's not the default guide
      const guide = guidesList.find(g => g.id === guideId);
      if (guide && !guide.is_default && editingGuideName.trim()) {
        updates.name = editingGuideName.trim();
      }
      const updated = await updateGuide(guideId, updates);
      setGuidesList(prev => prev.map(g => g.id === guideId ? updated : g));
      setEditingGuideId(null);
      setEditingGuideName('');
      setEditingGuideContent('');
      onGuidesChanged();
      showStatus('saved', 'Guide saved', 2000);
    } catch (error) {
      console.error('Failed to update guide:', error);
      showStatus('error', errorMessage(error, 'Failed to update guide'), 3000);
    }
  }, [editingGuideName, editingGuideContent, guidesList, onGuidesChanged, showStatus]);

  const handleDeleteGuide = useCallback(async (guideId: string) => {
    if (!confirm('Are you sure you want to delete this guide? Existing conversations using it will not be affected.')) {
      return;
    }
    try {
      await deleteGuide(guideId);
      setGuidesList(prev => prev.filter(g => g.id !== guideId));
      onGuidesChanged();
    } catch (error) {
      console.error('Failed to delete guide:', error);
      showStatus('error', errorMessage(error, 'Failed to delete guide'), 3000);
    }
  }, [onGuidesChanged, showStatus]);

  const handleConvertGuide = useCallback(async (guide: Guide) => {
    const autoloadNote = guide.is_default
      ? ' The new skill will be auto-loaded into every conversation, matching how the default guide worked.'
      : '';
    if (!confirm(`Convert "${guide.name}" into a skill? The guide will be deleted afterwards; existing conversations using it are not affected.${autoloadNote}`)) {
      return;
    }
    setConvertingGuideId(guide.id);
    try {
      const result = await convertGuideToSkill(guide.id);
      setGuidesList(prev => prev.filter(g => g.id !== guide.id));
      onGuidesChanged();
      showStatus(
        'saved',
        `Converted to skill "${result.skill.name}"${result.autoload_enabled ? ' (auto-load enabled)' : ''}`,
        4000,
      );
    } catch (error) {
      console.error('Failed to convert guide:', error);
      showStatus('error', errorMessage(error, 'Failed to convert guide'), 3000);
    } finally {
      setConvertingGuideId(null);
    }
  }, [onGuidesChanged, showStatus]);

  const startEditingGuide = useCallback((guide: Guide) => {
    setEditingGuideId(guide.id);
    setEditingGuideName(guide.name);
    setEditingGuideContent(guide.content);
  }, []);

  const cancelEditingGuide = useCallback(() => {
    setEditingGuideId(null);
    setEditingGuideName('');
    setEditingGuideContent('');
  }, []);

  return (
    <div className="settings-section">
      <h3>Guides</h3>
      <div className="guides-deprecation-notice">
        <strong>Skills now replace guides.</strong> Guides can no longer be created or
        selected for new conversations (existing conversations keep theirs).
        Convert your existing guides to skills below, or create new instructions in the{' '}
        <button className="guides-skills-link" onClick={onNavigateToSkills}>Skills section</button>.
      </div>
      <p className="settings-description">
        Guides are custom instructions included in the system prompt for your conversations.
        Converting a guide creates a skill with the same content and then deletes the guide.
        Converting the default guide enables auto-load on the new skill, so its instructions
        keep applying to every conversation.
      </p>

      {isLoadingGuides ? (
        <div className="settings-loading">Loading guides...</div>
      ) : guidesList.length === 0 ? (
        <div className="memories-empty">
          No guides. Skills now replace guides — manage them in the Skills section.
        </div>
      ) : (
        <div className="guides-list">
          {guidesList.map((guide) => (
            <div
              key={guide.id}
              className={`memory-card guide-card ${guide.is_default ? 'guide-card-default' : ''}`}
            >
              {editingGuideId === guide.id ? (
                <>
                  {guide.is_default ? (
                    <div className="guide-name-display">
                      <span className="guide-name-text">{guide.name}</span>
                      <span className="guide-badge-default">Default</span>
                    </div>
                  ) : (
                    <input
                      type="text"
                      className="guide-name-input"
                      value={editingGuideName}
                      onChange={(e) => setEditingGuideName(e.target.value)}
                      placeholder="Guide name..."
                      maxLength={100}
                      autoFocus
                    />
                  )}
                  <textarea
                    className="memory-edit-textarea"
                    value={editingGuideContent}
                    onChange={(e) => setEditingGuideContent(e.target.value)}
                    rows={8}
                    autoFocus={guide.is_default}
                  />
                  <div className="memory-card-actions">
                    <button
                      className="memory-btn memory-btn-save"
                      onClick={() => handleUpdateGuide(guide.id)}
                      disabled={guideSaveStatus === 'saving'}
                    >
                      {guideSaveStatus === 'saving' ? 'Saving...' : 'Save'}
                    </button>
                    <button
                      className="memory-btn memory-btn-cancel"
                      onClick={cancelEditingGuide}
                    >
                      Cancel
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div className="guide-card-header">
                    <span className="guide-card-name">{guide.name}</span>
                    {guide.is_default && (
                      <span className="guide-badge-default">Default</span>
                    )}
                  </div>
                  <div className="guide-card-preview">
                    {guide.content
                      ? (guide.content.length > 150 ? guide.content.substring(0, 150) + '...' : guide.content)
                      : <span className="guide-empty-hint">No instructions set</span>}
                  </div>
                  <div className="memory-card-actions">
                    {guide.content.trim() && (
                      <button
                        className="memory-btn memory-btn-save"
                        onClick={() => handleConvertGuide(guide)}
                        disabled={convertingGuideId !== null}
                      >
                        {convertingGuideId === guide.id ? 'Converting...' : 'Convert to Skill'}
                      </button>
                    )}
                    <button
                      className="memory-btn memory-btn-edit"
                      onClick={() => startEditingGuide(guide)}
                    >
                      Edit
                    </button>
                    <button
                      className="memory-btn memory-btn-delete"
                      onClick={() => handleDeleteGuide(guide.id)}
                    >
                      Delete
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {guideSaveStatus === 'saved' && (
        <span className="settings-save-status saved">{guideSaveMessage || 'Guide saved'}</span>
      )}
      {guideSaveStatus === 'error' && (
        <span className="settings-save-status error">{guideSaveMessage || 'Failed to save'}</span>
      )}
    </div>
  );
}
