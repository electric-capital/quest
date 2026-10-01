/**
 * Project Skills section for the Project Settings Modal.
 *
 * Three tabs:
 * - "Project Skills" -- full CRUD for project-specific skills + auto-load toggle
 * - "My Skills" -- read-only view of user's own skills + project auto-load toggle
 * - "Shared with Me" -- read-only view of shared/public skills + project auto-load toggle
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { errorMessage } from '../utils/errorMessage';
import {
  fetchProjectSkills,
  createProjectSkill,
  updateProjectSkill,
  deleteProjectSkill,
  fetchProjectAutoloadedSkillIds,
  setProjectSkillAutoload,
  fetchSkills,
  fetchSharedWithMeSkills,
} from '../api/client';
import type { Skill } from '../api/types';
import './settings/SkillsSection.css';

interface ProjectSkillsSectionProps {
  projectId: string;
}

type ProjectSkillsTab = 'project-skills' | 'my-skills' | 'shared-with-me';

export function ProjectSkillsSection({ projectId }: ProjectSkillsSectionProps) {
  // Tab state
  const [activeTab, setActiveTab] = useState<ProjectSkillsTab>('project-skills');

  // Project skills state
  const [projectSkillsList, setProjectSkillsList] = useState<Skill[]>([]);
  const [isLoadingProjectSkills, setIsLoadingProjectSkills] = useState(false);
  const [editingSkillId, setEditingSkillId] = useState<string | null>(null);
  const [editingSkillName, setEditingSkillName] = useState('');
  const [editingSkillDescription, setEditingSkillDescription] = useState('');
  const [editingSkillContent, setEditingSkillContent] = useState('');
  const [isCreatingSkill, setIsCreatingSkill] = useState(false);
  const [newSkillName, setNewSkillName] = useState('');
  const [newSkillDescription, setNewSkillDescription] = useState('');
  const [newSkillContent, setNewSkillContent] = useState('');
  const [skillSaveStatus, setSkillSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [skillSaveError, setSkillSaveError] = useState('');
  const skillSaveTimeoutRef = useRef<number | null>(null);

  // My skills state
  const [mySkillsList, setMySkillsList] = useState<Skill[]>([]);
  const [isLoadingMySkills, setIsLoadingMySkills] = useState(false);
  const [mySkillsLoaded, setMySkillsLoaded] = useState(false);

  // Shared with me state
  const [sharedSkillsList, setSharedSkillsList] = useState<Skill[]>([]);
  const [isLoadingSharedSkills, setIsLoadingSharedSkills] = useState(false);
  const [sharedSkillsLoaded, setSharedSkillsLoaded] = useState(false);

  // Project auto-load state (shared across all tabs)
  const [projectAutoloadedSkillIds, setProjectAutoloadedSkillIds] = useState<Set<string>>(new Set());

  // Load project skills and autoloaded IDs on mount
  useEffect(() => {
    const loadData = async () => {
      setIsLoadingProjectSkills(true);
      try {
        const [skillsResponse, autoloadResponse] = await Promise.all([
          fetchProjectSkills(projectId),
          fetchProjectAutoloadedSkillIds(projectId),
        ]);
        setProjectSkillsList(skillsResponse.skills);
        setProjectAutoloadedSkillIds(new Set(autoloadResponse.skill_ids));
      } catch (error) {
        console.error('Failed to load project skills:', error);
      } finally {
        setIsLoadingProjectSkills(false);
      }
    };

    loadData();
  }, [projectId]);

  // Load my skills when switching to that tab (lazy load)
  useEffect(() => {
    if (activeTab !== 'my-skills') return;
    if (mySkillsLoaded) return;

    const loadMySkills = async () => {
      setIsLoadingMySkills(true);
      try {
        const response = await fetchSkills(true); // owned=true
        setMySkillsList(response.skills);
        setMySkillsLoaded(true);
      } catch (error) {
        console.error('Failed to load my skills:', error);
      } finally {
        setIsLoadingMySkills(false);
      }
    };

    loadMySkills();
  }, [activeTab, mySkillsLoaded]);

  // Load shared skills when switching to that tab (lazy load)
  useEffect(() => {
    if (activeTab !== 'shared-with-me') return;
    if (sharedSkillsLoaded) return;

    const loadSharedSkills = async () => {
      setIsLoadingSharedSkills(true);
      try {
        const response = await fetchSharedWithMeSkills();
        setSharedSkillsList(response.skills);
        setSharedSkillsLoaded(true);
      } catch (error) {
        console.error('Failed to load shared skills:', error);
      } finally {
        setIsLoadingSharedSkills(false);
      }
    };

    loadSharedSkills();
  }, [activeTab, sharedSkillsLoaded]);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
    };
  }, []);

  // Project auto-load toggle handler (shared across all tabs)
  const handleToggleProjectAutoload = useCallback((skillId: string) => {
    setProjectAutoloadedSkillIds(prev => {
      const next = new Set(prev);
      const newEnabled = !next.has(skillId);
      if (newEnabled) {
        next.add(skillId);
      } else {
        next.delete(skillId);
      }
      // Persist to server (fire-and-forget for instant UI feel)
      setProjectSkillAutoload(projectId, skillId, newEnabled).catch(() => {});
      return next;
    });
  }, [projectId]);

  // Project skill CRUD handlers
  const handleCreateProjectSkill = useCallback(async () => {
    if (!newSkillName.trim() || !newSkillContent.trim()) return;
    setSkillSaveStatus('saving');
    setSkillSaveError('');
    try {
      const skill = await createProjectSkill(projectId, {
        name: newSkillName.trim(),
        description: newSkillDescription.trim(),
        content: newSkillContent,
      });
      setProjectSkillsList(prev => [skill, ...prev]);
      setNewSkillName('');
      setNewSkillDescription('');
      setNewSkillContent('');
      setIsCreatingSkill(false);
      setSkillSaveStatus('saved');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => setSkillSaveStatus('idle'), 2000);
    } catch (error) {
      console.error('Failed to create project skill:', error);
      setSkillSaveError(errorMessage(error, 'Failed to create skill'));
      setSkillSaveStatus('error');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => { setSkillSaveStatus('idle'); setSkillSaveError(''); }, 3000);
    }
  }, [projectId, newSkillName, newSkillDescription, newSkillContent]);

  const handleUpdateProjectSkill = useCallback(async (skillId: string) => {
    setSkillSaveStatus('saving');
    setSkillSaveError('');
    try {
      const updates: { name?: string; description?: string; content?: string } = {};
      if (editingSkillName.trim()) updates.name = editingSkillName.trim();
      updates.description = editingSkillDescription.trim();
      updates.content = editingSkillContent;
      const updated = await updateProjectSkill(projectId, skillId, updates);
      setProjectSkillsList(prev => prev.map(s => s.id === skillId ? updated : s));
      setEditingSkillId(null);
      setEditingSkillName('');
      setEditingSkillDescription('');
      setEditingSkillContent('');
      setSkillSaveStatus('saved');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => setSkillSaveStatus('idle'), 2000);
    } catch (error) {
      console.error('Failed to update project skill:', error);
      setSkillSaveError(errorMessage(error, 'Failed to update skill'));
      setSkillSaveStatus('error');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => { setSkillSaveStatus('idle'); setSkillSaveError(''); }, 3000);
    }
  }, [projectId, editingSkillName, editingSkillDescription, editingSkillContent]);

  const handleDeleteProjectSkill = useCallback(async (skillId: string) => {
    if (!confirm('Are you sure you want to delete this skill? This cannot be undone.')) {
      return;
    }
    try {
      await deleteProjectSkill(projectId, skillId);
      setProjectSkillsList(prev => prev.filter(s => s.id !== skillId));
    } catch (error) {
      console.error('Failed to delete project skill:', error);
      setSkillSaveError(errorMessage(error, 'Failed to delete skill'));
      setSkillSaveStatus('error');
      if (skillSaveTimeoutRef.current) clearTimeout(skillSaveTimeoutRef.current);
      skillSaveTimeoutRef.current = window.setTimeout(() => { setSkillSaveStatus('idle'); setSkillSaveError(''); }, 3000);
    }
  }, [projectId]);

  const startEditingSkill = useCallback((skill: Skill) => {
    setEditingSkillId(skill.id);
    setEditingSkillName(skill.name);
    setEditingSkillDescription(skill.description);
    setEditingSkillContent(skill.content);
  }, []);

  const cancelEditingSkill = useCallback(() => {
    setEditingSkillId(null);
    setEditingSkillName('');
    setEditingSkillDescription('');
    setEditingSkillContent('');
  }, []);

  const startCreatingSkill = useCallback(() => {
    setIsCreatingSkill(true);
    setNewSkillName('');
    setNewSkillDescription('');
    setNewSkillContent('');
  }, []);

  const cancelCreatingSkill = useCallback(() => {
    setIsCreatingSkill(false);
    setNewSkillName('');
    setNewSkillDescription('');
    setNewSkillContent('');
  }, []);

  // Render a project skill card (full CRUD + auto-load toggle)
  const renderProjectSkillCard = (skill: Skill) => (
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
          <div className="memory-card-actions">
            <button
              className="memory-btn memory-btn-save"
              onClick={() => handleUpdateProjectSkill(skill.id)}
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
      ) : (
        <>
          <div className="skill-card-header">
            <span className="skill-card-name">{skill.name}</span>
            <div className="skill-enabled-toggle">
              <label title="Auto-loaded skills are automatically included in every conversation in this project">
                <input
                  type="checkbox"
                  checked={projectAutoloadedSkillIds.has(skill.id)}
                  onChange={() => handleToggleProjectAutoload(skill.id)}
                />
                {' '}Auto-load
              </label>
            </div>
          </div>
          {skill.description && (
            <div className="skill-card-description">{skill.description}</div>
          )}
          <div className="guide-card-preview">
            {skill.content
              ? (skill.content.length > 150 ? skill.content.substring(0, 150) + '...' : skill.content)
              : <span className="guide-empty-hint">No content</span>}
          </div>
          <div className="memory-card-actions">
            <button
              className="memory-btn memory-btn-edit"
              onClick={() => startEditingSkill(skill)}
            >
              Edit
            </button>
            <button
              className="memory-btn memory-btn-delete"
              onClick={() => handleDeleteProjectSkill(skill.id)}
            >
              Delete
            </button>
          </div>
        </>
      )}
    </div>
  );

  // Render a read-only skill card with project auto-load toggle (for My Skills and Shared tabs)
  const renderReadOnlySkillCard = (skill: Skill, showCreator: boolean) => (
    <div key={skill.id} className="memory-card skill-card">
      <div className="skill-card-header">
        <span className="skill-card-name">{skill.name}</span>
        <span className={`skill-badge-${skill.visibility}`}>
          {skill.visibility}
        </span>
        <div className="skill-enabled-toggle">
          <label title="Auto-loaded skills are automatically included in every conversation in this project">
            <input
              type="checkbox"
              checked={projectAutoloadedSkillIds.has(skill.id)}
              onChange={() => handleToggleProjectAutoload(skill.id)}
            />
            {' '}Auto-load
          </label>
        </div>
      </div>
      {showCreator && (
        <div className="skill-card-creator">
          By {skill.creator_name || skill.creator_email || 'Unknown'}
        </div>
      )}
      {skill.description && (
        <div className="skill-card-description">{skill.description}</div>
      )}
      <div className="guide-card-preview">
        {skill.content
          ? (skill.content.length > 150 ? skill.content.substring(0, 150) + '...' : skill.content)
          : <span className="guide-empty-hint">No content</span>}
      </div>
    </div>
  );

  return (
    <div className="settings-section">
      <h3>Skills</h3>
      <p className="settings-description">
        Manage skills for this project. Auto-loaded skills are automatically included in every conversation.
      </p>

      <div className="skills-tab-bar">
        <button
          className={`skills-tab-btn${activeTab === 'project-skills' ? ' active' : ''}`}
          onClick={() => setActiveTab('project-skills')}
        >
          Project Skills
        </button>
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

      {activeTab === 'project-skills' && (
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
              <div className="memory-card-actions">
                <button
                  className="memory-btn memory-btn-save"
                  onClick={handleCreateProjectSkill}
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

          {isLoadingProjectSkills ? (
            <div className="settings-loading">Loading skills...</div>
          ) : projectSkillsList.length === 0 ? (
            <div className="memories-empty">
              No project skills yet. Click &quot;+ New Skill&quot; to create one.
            </div>
          ) : (
            <div className="skills-list">
              {projectSkillsList.map(renderProjectSkillCard)}
            </div>
          )}
        </>
      )}

      {activeTab === 'my-skills' && (
        <>
          <p className="settings-description" style={{ fontSize: '0.8125rem', marginBottom: '0.75rem' }}>
            Toggle auto-load to include your personal skills in this project. Manage your skills in Settings.
          </p>
          {isLoadingMySkills ? (
            <div className="settings-loading">Loading skills...</div>
          ) : mySkillsList.length === 0 ? (
            <div className="memories-empty">
              No personal skills. Create skills in Settings.
            </div>
          ) : (
            <div className="skills-list">
              {mySkillsList.map(skill => renderReadOnlySkillCard(skill, false))}
            </div>
          )}
        </>
      )}

      {activeTab === 'shared-with-me' && (
        <>
          <p className="settings-description" style={{ fontSize: '0.8125rem', marginBottom: '0.75rem' }}>
            Toggle auto-load to include shared skills in this project. Manage your skills in Settings.
          </p>
          {isLoadingSharedSkills ? (
            <div className="settings-loading">Loading shared skills...</div>
          ) : sharedSkillsList.length === 0 ? (
            <div className="memories-empty">
              No skills have been shared with you yet.
            </div>
          ) : (
            <div className="skills-list">
              {sharedSkillsList.map(skill => renderReadOnlySkillCard(skill, true))}
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
