import { useState, useEffect, useCallback, useRef } from 'react';
import { errorMessage } from '../../utils/errorMessage';
import {
  fetchSkills, createSkill, updateSkill, deleteSkill,
  fetchSkillShares, addSkillShares, removeSkillShare, searchUsers,
  fetchAutoloadedSkillIds, setSkillAutoload, fetchSharedWithMeSkills,
} from '../../api/client';
import type { Skill, SkillShare, UserSearchResult } from '../../api/types';
import './SkillsSection.css';

export function SkillsSection() {
  // Tab state
  const [activeTab, setActiveTab] = useState<'my-skills' | 'shared-with-me'>('my-skills');

  // My skills state
  const [skillsList, setSkillsList] = useState<Skill[]>([]);
  const [isLoadingSkills, setIsLoadingSkills] = useState(false);
  const [editingSkillId, setEditingSkillId] = useState<string | null>(null);
  const [editingSkillName, setEditingSkillName] = useState('');
  const [editingSkillDescription, setEditingSkillDescription] = useState('');
  const [editingSkillContent, setEditingSkillContent] = useState('');
  const [editingSkillVisibility, setEditingSkillVisibility] = useState('private');
  const [isCreatingSkill, setIsCreatingSkill] = useState(false);
  const [newSkillName, setNewSkillName] = useState('');
  const [newSkillDescription, setNewSkillDescription] = useState('');
  const [newSkillContent, setNewSkillContent] = useState('');
  const [newSkillVisibility, setNewSkillVisibility] = useState('private');
  const [skillSaveStatus, setSkillSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [skillSaveError, setSkillSaveError] = useState('');
  const skillSaveTimeoutRef = useRef<number | null>(null);

  // Shared with me state
  const [sharedSkillsList, setSharedSkillsList] = useState<Skill[]>([]);
  const [isLoadingSharedSkills, setIsLoadingSharedSkills] = useState(false);

  // Sharing UI state
  const [managingSharesSkillId, setManagingSharesSkillId] = useState<string | null>(null);
  const [skillShares, setSkillShares] = useState<SkillShare[]>([]);
  const [isLoadingShares, setIsLoadingShares] = useState(false);
  const [newShareEmail, setNewShareEmail] = useState('');
  const [shareSearchResults, setShareSearchResults] = useState<UserSearchResult[]>([]);
  const [showShareDropdown, setShowShareDropdown] = useState(false);
  const shareSearchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shareDropdownBlurTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Auto-load state (server-backed)
  const [autoloadedSkillIds, setAutoloadedSkillIds] = useState<Set<string>>(new Set());

  // Skill cards with their full content expanded
  const [expandedSkillIds, setExpandedSkillIds] = useState<Set<string>>(new Set());

  // Load my skills and autoloaded IDs on mount
  useEffect(() => {
    const loadData = async () => {
      setIsLoadingSkills(true);
      try {
        const [skillsResponse, autoloadResponse] = await Promise.all([
          fetchSkills(true), // owned=true
          fetchAutoloadedSkillIds(),
        ]);
        setSkillsList(skillsResponse.skills);
        setAutoloadedSkillIds(new Set(autoloadResponse.skill_ids));
      } catch (error) {
        console.error('Failed to load skills:', error);
      } finally {
        setIsLoadingSkills(false);
      }
    };

    loadData();
  }, []);

  // Load shared skills when switching to the shared tab
  useEffect(() => {
    if (activeTab !== 'shared-with-me') return;
    if (sharedSkillsList.length > 0) return; // Already loaded

    const loadSharedSkills = async () => {
      setIsLoadingSharedSkills(true);
      try {
        const response = await fetchSharedWithMeSkills();
        setSharedSkillsList(response.skills);
      } catch (error) {
        console.error('Failed to load shared skills:', error);
      } finally {
        setIsLoadingSharedSkills(false);
      }
    };

    loadSharedSkills();
  }, [activeTab, sharedSkillsList.length]);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      if (shareSearchTimerRef.current) clearTimeout(shareSearchTimerRef.current);
      if (shareDropdownBlurTimerRef.current) clearTimeout(shareDropdownBlurTimerRef.current);
    };
  }, []);

  const handleCreateSkill = useCallback(async () => {
    if (!newSkillName.trim() || !newSkillContent.trim()) return;
    setSkillSaveStatus('saving');
    setSkillSaveError('');
    try {
      const skill = await createSkill({
        name: newSkillName.trim(),
        description: newSkillDescription.trim(),
        content: newSkillContent,
        visibility: newSkillVisibility,
      });
      setSkillsList(prev => [...prev, skill]);
      setNewSkillName('');
      setNewSkillDescription('');
      setNewSkillContent('');
      setNewSkillVisibility('private');
      setIsCreatingSkill(false);
      setSkillSaveStatus('saved');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => setSkillSaveStatus('idle'), 2000);
    } catch (error) {
      console.error('Failed to create skill:', error);
      setSkillSaveError(errorMessage(error, 'Failed to create skill'));
      setSkillSaveStatus('error');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => { setSkillSaveStatus('idle'); setSkillSaveError(''); }, 3000);
    }
  }, [newSkillName, newSkillDescription, newSkillContent, newSkillVisibility]);

  const handleUpdateSkill = useCallback(async (skillId: string) => {
    setSkillSaveStatus('saving');
    setSkillSaveError('');
    try {
      const updates: { name?: string; description?: string; content?: string; visibility?: string } = {};
      if (editingSkillName.trim()) updates.name = editingSkillName.trim();
      updates.description = editingSkillDescription.trim();
      updates.content = editingSkillContent;
      updates.visibility = editingSkillVisibility;
      const updated = await updateSkill(skillId, updates);
      setSkillsList(prev => prev.map(s => s.id === skillId ? updated : s));
      setEditingSkillId(null);
      setEditingSkillName('');
      setEditingSkillDescription('');
      setEditingSkillContent('');
      setEditingSkillVisibility('private');
      setSkillSaveStatus('saved');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => setSkillSaveStatus('idle'), 2000);
    } catch (error) {
      console.error('Failed to update skill:', error);
      setSkillSaveError(errorMessage(error, 'Failed to update skill'));
      setSkillSaveStatus('error');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => { setSkillSaveStatus('idle'); setSkillSaveError(''); }, 3000);
    }
  }, [editingSkillName, editingSkillDescription, editingSkillContent, editingSkillVisibility]);

  const handleDeleteSkill = useCallback(async (skillId: string) => {
    if (!confirm('Are you sure you want to delete this skill? This cannot be undone.')) {
      return;
    }
    try {
      await deleteSkill(skillId);
      setSkillsList(prev => prev.filter(s => s.id !== skillId));
    } catch (error) {
      console.error('Failed to delete skill:', error);
      setSkillSaveError(errorMessage(error, 'Failed to delete skill'));
      setSkillSaveStatus('error');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => { setSkillSaveStatus('idle'); setSkillSaveError(''); }, 3000);
    }
  }, []);

  const startEditingSkill = useCallback((skill: Skill) => {
    setEditingSkillId(skill.id);
    setEditingSkillName(skill.name);
    setEditingSkillDescription(skill.description);
    setEditingSkillContent(skill.content);
    setEditingSkillVisibility(skill.visibility);
  }, []);

  const cancelEditingSkill = useCallback(() => {
    setEditingSkillId(null);
    setEditingSkillName('');
    setEditingSkillDescription('');
    setEditingSkillContent('');
    setEditingSkillVisibility('private');
  }, []);

  const startCreatingSkill = useCallback(() => {
    setIsCreatingSkill(true);
    setNewSkillName('');
    setNewSkillDescription('');
    setNewSkillContent('');
    setNewSkillVisibility('private');
  }, []);

  const cancelCreatingSkill = useCallback(() => {
    setIsCreatingSkill(false);
    setNewSkillName('');
    setNewSkillDescription('');
    setNewSkillContent('');
    setNewSkillVisibility('private');
  }, []);

  // Sharing handlers
  const handleOpenShares = useCallback(async (skillId: string) => {
    setManagingSharesSkillId(skillId);
    setIsLoadingShares(true);
    setNewShareEmail('');
    try {
      const response = await fetchSkillShares(skillId);
      setSkillShares(response.shares);
    } catch (error) {
      console.error('Failed to load shares:', error);
      setSkillShares([]);
    } finally {
      setIsLoadingShares(false);
    }
  }, []);

  const handleCloseShares = useCallback(() => {
    setManagingSharesSkillId(null);
    setSkillShares([]);
    setNewShareEmail('');
    setShareSearchResults([]);
    setShowShareDropdown(false);
    if (shareSearchTimerRef.current) {
      clearTimeout(shareSearchTimerRef.current);
      shareSearchTimerRef.current = null;
    }
  }, []);

  const handleAddShare = useCallback(async (skillId: string, email?: string) => {
    const shareEmail = email || newShareEmail.trim();
    if (!shareEmail) return;
    try {
      const response = await addSkillShares(skillId, [shareEmail]);
      setSkillShares(response.shares);
      setNewShareEmail('');
      setShareSearchResults([]);
      setShowShareDropdown(false);
    } catch (error) {
      console.error('Failed to add share:', error);
    }
  }, [newShareEmail]);

  const handleRemoveShare = useCallback(async (skillId: string, userId: number) => {
    try {
      await removeSkillShare(skillId, userId);
      setSkillShares(prev => prev.filter(s => s.user_id !== userId));
    } catch (error) {
      console.error('Failed to remove share:', error);
    }
  }, []);

  const handleShareInputChange = useCallback((value: string) => {
    setNewShareEmail(value);
    if (shareSearchTimerRef.current) {
      clearTimeout(shareSearchTimerRef.current);
    }
    if (value.trim().length < 2) {
      setShareSearchResults([]);
      setShowShareDropdown(false);
      return;
    }
    shareSearchTimerRef.current = setTimeout(async () => {
      try {
        const response = await searchUsers(value.trim());
        setShareSearchResults(response.users);
        setShowShareDropdown(response.users.length > 0);
      } catch (error) {
        console.error('Failed to search users:', error);
        setShareSearchResults([]);
        setShowShareDropdown(false);
      }
    }, 300);
  }, []);

  // Auto-load toggle handler (server-backed)
  const handleToggleAutoload = useCallback((skillId: string) => {
    setAutoloadedSkillIds(prev => {
      const next = new Set(prev);
      const newEnabled = !next.has(skillId);
      if (newEnabled) {
        next.add(skillId);
      } else {
        next.delete(skillId);
      }
      // Persist to server (fire-and-forget for instant UI feel)
      setSkillAutoload(skillId, newEnabled).catch(() => {});
      return next;
    });
  }, []);

  const toggleExpanded = useCallback((skillId: string) => {
    setExpandedSkillIds(prev => {
      const next = new Set(prev);
      if (next.has(skillId)) {
        next.delete(skillId);
      } else {
        next.add(skillId);
      }
      return next;
    });
  }, []);

  const SKILL_PREVIEW_LENGTH = 150;

  // Render skill content, truncated with a Show more/Show less toggle when long
  const renderSkillPreview = (skill: Skill) => {
    if (!skill.content) {
      return (
        <div className="guide-card-preview">
          <span className="guide-empty-hint">No content</span>
        </div>
      );
    }
    const isLong = skill.content.length > SKILL_PREVIEW_LENGTH;
    const isExpanded = expandedSkillIds.has(skill.id);
    return (
      <div className="guide-card-preview">
        {isLong && !isExpanded ? skill.content.substring(0, SKILL_PREVIEW_LENGTH) + '...' : skill.content}
        {isLong && (
          <button
            className="skill-preview-toggle"
            onClick={() => toggleExpanded(skill.id)}
          >
            {isExpanded ? 'Show less' : 'Show more'}
          </button>
        )}
      </div>
    );
  };

  // Render a skill card for the "My Skills" tab
  const renderOwnedSkillCard = (skill: Skill) => (
    <div key={skill.id} className="memory-card skill-card">
      {editingSkillId === skill.id ? (
        <>
          <input
            type="text"
            className="guide-name-input"
            value={editingSkillName}
            onChange={(e) => setEditingSkillName(e.target.value)}
            placeholder="Skill name..."
            maxLength={100}
            autoFocus
          />
          <input
            type="text"
            className="guide-name-input skill-description-input"
            value={editingSkillDescription}
            onChange={(e) => setEditingSkillDescription(e.target.value)}
            placeholder="Description (optional)..."
            maxLength={500}
          />
          <textarea
            className="memory-edit-textarea"
            value={editingSkillContent}
            onChange={(e) => setEditingSkillContent(e.target.value)}
            rows={8}
          />
          <div className="skill-visibility-selector">
            <label>Visibility:</label>
            <select value={editingSkillVisibility} onChange={(e) => setEditingSkillVisibility(e.target.value)}>
              <option value="private">Private</option>
              <option value="shared">Shared</option>
              <option value="public">Public</option>
            </select>
          </div>
          <div className="memory-card-actions">
            <button
              className="memory-btn memory-btn-save"
              onClick={() => handleUpdateSkill(skill.id)}
              disabled={skillSaveStatus === 'saving'}
            >
              {skillSaveStatus === 'saving' ? 'Saving...' : 'Save'}
            </button>
            <button
              className="memory-btn memory-btn-cancel"
              onClick={cancelEditingSkill}
            >
              Cancel
            </button>
          </div>
        </>
      ) : managingSharesSkillId === skill.id ? (
        <div className="skill-shares-section">
          <div className="skill-card-header">
            <span className="skill-card-name">Shares for: {skill.name}</span>
          </div>
          {isLoadingShares ? (
            <div className="settings-loading">Loading shares...</div>
          ) : (
            <>
              {skillShares.length === 0 ? (
                <p className="settings-description">No shares yet. Add an email below to share this skill.</p>
              ) : (
                <div className="skill-shares-list">
                  {skillShares.map((share) => (
                    <div key={share.user_id} className="skill-share-row">
                      <div>
                        <span className="skill-share-email">{share.email}</span>
                        {share.name && <span className="skill-share-name">({share.name})</span>}
                      </div>
                      <button
                        className="memory-btn memory-btn-delete"
                        onClick={() => handleRemoveShare(skill.id, share.user_id)}
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div className="skill-add-share-row" style={{ position: 'relative' }}>
                <div style={{ position: 'relative', flex: 1 }}>
                  <input
                    type="text"
                    className="guide-name-input skill-add-share-input"
                    value={newShareEmail}
                    onChange={(e) => handleShareInputChange(e.target.value)}
                    placeholder="Search by name or email..."
                    onKeyDown={(e) => { if (e.key === 'Enter') handleAddShare(skill.id); }}
                    onFocus={() => { if (shareSearchResults.length > 0) setShowShareDropdown(true); }}
                    onBlur={() => {
                      shareDropdownBlurTimerRef.current = setTimeout(() => setShowShareDropdown(false), 150);
                    }}
                  />
                  {showShareDropdown && shareSearchResults.length > 0 && (
                    <div className="share-typeahead-dropdown">
                      {shareSearchResults.map(user => (
                        <div
                          key={user.id}
                          className="share-typeahead-option"
                          onMouseDown={() => {
                            if (shareDropdownBlurTimerRef.current) {
                              clearTimeout(shareDropdownBlurTimerRef.current);
                            }
                            handleAddShare(skill.id, user.email);
                          }}
                        >
                          <span className="share-typeahead-name">{user.name}</span>
                          <span className="share-typeahead-email">{user.email}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                <button
                  className="memory-btn memory-btn-save"
                  onClick={() => handleAddShare(skill.id)}
                  disabled={!newShareEmail.trim()}
                >
                  Add
                </button>
              </div>
            </>
          )}
          <div className="memory-card-actions">
            <button
              className="memory-btn memory-btn-cancel"
              onClick={handleCloseShares}
            >
              Done
            </button>
          </div>
        </div>
      ) : (
        <>
          <div className="skill-card-header">
            <span className="skill-card-name">{skill.name}</span>
            <span className={`skill-badge-${skill.visibility}`}>
              {skill.visibility}
            </span>
            <div className="skill-enabled-toggle">
              <label title="Auto-loaded skills are automatically included in every conversation">
                <input
                  type="checkbox"
                  checked={autoloadedSkillIds.has(skill.id)}
                  onChange={() => handleToggleAutoload(skill.id)}
                />
                {' '}Auto-load
              </label>
            </div>
          </div>
          {skill.description && (
            <div className="skill-card-description">{skill.description}</div>
          )}
          {renderSkillPreview(skill)}
          <div className="memory-card-actions">
            <button
              className="memory-btn memory-btn-edit"
              onClick={() => startEditingSkill(skill)}
            >
              Edit
            </button>
            {skill.visibility === 'shared' && (
              <button
                className="memory-btn memory-btn-edit"
                onClick={() => handleOpenShares(skill.id)}
              >
                Shares
              </button>
            )}
            <button
              className="memory-btn memory-btn-delete"
              onClick={() => handleDeleteSkill(skill.id)}
            >
              Delete
            </button>
          </div>
        </>
      )}
    </div>
  );

  // Render a skill card for the "Shared with Me" tab (read-only)
  const renderSharedSkillCard = (skill: Skill) => (
    <div key={skill.id} className="memory-card skill-card">
      <div className="skill-card-header">
        <span className="skill-card-name">{skill.name}</span>
        <span className={`skill-badge-${skill.visibility}`}>
          {skill.visibility}
        </span>
        <div className="skill-enabled-toggle">
          <label title="Auto-loaded skills are automatically included in every conversation">
            <input
              type="checkbox"
              checked={autoloadedSkillIds.has(skill.id)}
              onChange={() => handleToggleAutoload(skill.id)}
            />
            {' '}Auto-load
          </label>
        </div>
      </div>
      <div className="skill-card-creator">
        By {skill.creator_name || skill.creator_email || 'Unknown'}
      </div>
      {skill.description && (
        <div className="skill-card-description">{skill.description}</div>
      )}
      {renderSkillPreview(skill)}
    </div>
  );

  return (
    <div className="settings-section">
      <h3>Skills</h3>
      <p className="settings-description">
        Skills are reusable instruction sets that can be shared with other users.
        Auto-load skills to automatically include them in your conversations.
      </p>

      <div className="skills-tab-bar">
        <button
          className={`skills-tab-btn${activeTab === 'my-skills' ? ' active' : ''}`}
          onClick={() => setActiveTab('my-skills')}
        >
          My Skills
        </button>
        <button
          className={`skills-tab-btn${activeTab === 'shared-with-me' ? ' active' : ''}`}
          onClick={() => setActiveTab('shared-with-me')}
        >
          Shared with Me
        </button>
      </div>

      {activeTab === 'my-skills' && (
        <>
          <div className="skills-toolbar">
            <button
              className="memories-create-btn"
              onClick={startCreatingSkill}
              disabled={isCreatingSkill}
            >
              + New Skill
            </button>
          </div>

          {isCreatingSkill && (
            <div className="memory-card memory-card-editing skill-card-editing">
              <input
                type="text"
                className="guide-name-input"
                value={newSkillName}
                onChange={(e) => setNewSkillName(e.target.value)}
                placeholder="Skill name..."
                maxLength={100}
                autoFocus
              />
              <input
                type="text"
                className="guide-name-input skill-description-input"
                value={newSkillDescription}
                onChange={(e) => setNewSkillDescription(e.target.value)}
                placeholder="Description (optional)..."
                maxLength={500}
              />
              <textarea
                className="memory-edit-textarea"
                value={newSkillContent}
                onChange={(e) => setNewSkillContent(e.target.value)}
                placeholder="Enter skill instructions (max 64KB)..."
                rows={6}
              />
              <div className="skill-visibility-selector">
                <label>Visibility:</label>
                <select value={newSkillVisibility} onChange={(e) => setNewSkillVisibility(e.target.value)}>
                  <option value="private">Private</option>
                  <option value="shared">Shared</option>
                  <option value="public">Public</option>
                </select>
              </div>
              <div className="memory-card-actions">
                <button
                  className="memory-btn memory-btn-save"
                  onClick={handleCreateSkill}
                  disabled={!newSkillName.trim() || !newSkillContent.trim() || skillSaveStatus === 'saving'}
                >
                  {skillSaveStatus === 'saving' ? 'Saving...' : 'Save'}
                </button>
                <button
                  className="memory-btn memory-btn-cancel"
                  onClick={cancelCreatingSkill}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {isLoadingSkills ? (
            <div className="settings-loading">Loading skills...</div>
          ) : skillsList.length === 0 ? (
            <div className="memories-empty">
              No skills yet. Click &quot;+ New Skill&quot; to create one.
            </div>
          ) : (
            <div className="skills-list">
              {skillsList.map(renderOwnedSkillCard)}
            </div>
          )}
        </>
      )}

      {activeTab === 'shared-with-me' && (
        <>
          {isLoadingSharedSkills ? (
            <div className="settings-loading">Loading shared skills...</div>
          ) : sharedSkillsList.length === 0 ? (
            <div className="memories-empty">
              No skills have been shared with you yet.
            </div>
          ) : (
            <div className="skills-list">
              {sharedSkillsList.map(renderSharedSkillCard)}
            </div>
          )}
        </>
      )}

      {skillSaveStatus === 'saved' && (
        <span className="settings-save-status saved">Skill saved</span>
      )}
      {skillSaveStatus === 'error' && (
        <span className="settings-save-status error">{skillSaveError || 'Failed to save'}</span>
      )}
    </div>
  );
}
