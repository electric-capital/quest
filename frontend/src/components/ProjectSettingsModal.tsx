/**
 * Modal for editing project settings.
 *
 * Sidebar navigation with three sections: General, Skills, and Danger Zone
 * (archive / unarchive, then the two-step delete).
 * Follows the same pattern as SettingsModal.tsx, including the mobile
 * full-screen two-tier takeover (section list first, content slides over).
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import {
  fetchProject,
  updateProject,
  deleteProject,
  archiveProject,
  unarchiveProject,
  ApiClientError,
} from '../api/client';
import type { Project } from '../api/types';
import { useIsMobile } from '../hooks/useIsMobile';
import { ProjectSkillsSection } from './ProjectSkillsSection';
import { ModalShell } from './ModalShell';
import './ProjectSettingsModal.css';

interface ProjectSettingsModalProps {
  isOpen: boolean;
  projectId: string | null;
  onClose: () => void;
  onProjectUpdated: () => void;
  onProjectDeleted: () => void;
}

type ProjectSettingsSection = 'general' | 'skills' | 'danger-zone';

interface SectionEntry {
  id: ProjectSettingsSection;
  label: string;
}

const MAIN_SECTIONS: SectionEntry[] = [
  { id: 'general', label: 'General' },
  { id: 'skills', label: 'Skills' },
];

/** Public projects cannot have project skills, so the Skills section is hidden. */
const PUBLIC_MAIN_SECTIONS: SectionEntry[] = MAIN_SECTIONS.filter(
  (s) => s.id !== 'skills'
);

const DANGER_ZONE_SECTION: SectionEntry = { id: 'danger-zone', label: 'Danger Zone' };

const SECTION_LABELS = Object.fromEntries(
  [...MAIN_SECTIONS, DANGER_ZONE_SECTION].map((s) => [s.id, s.label])
) as Record<ProjectSettingsSection, string>;

export function ProjectSettingsModal({
  isOpen,
  projectId,
  onClose,
  onProjectUpdated,
  onProjectDeleted,
}: ProjectSettingsModalProps) {
  const isMobile = useIsMobile();
  // Sidebar navigation state
  const [activeSection, setActiveSection] = useState<ProjectSettingsSection>('general');
  // Mobile two-tier nav: false shows the section list, true slides the active
  // section's content over it. Ignored on desktop (side-by-side layout).
  const [mobileSectionOpen, setMobileSectionOpen] = useState(false);

  // General section state
  const [project, setProject] = useState<Project | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [name, setName] = useState('');
  const [guide, setGuide] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saved' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isArchiving, setIsArchiving] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const saveTimeoutRef = useRef<number | null>(null);

  // Reset activeSection when modal opens or projectId changes
  useEffect(() => {
    if (isOpen) {
      setActiveSection('general');
      setMobileSectionOpen(false);
    }
  }, [isOpen, projectId]);

  // Escape on mobile first steps back to the section list
  const handleEscape = useCallback(() => {
    if (isMobile && mobileSectionOpen) {
      setMobileSectionOpen(false);
    } else {
      onClose();
    }
  }, [isMobile, mobileSectionOpen, onClose]);

  // Load project data when modal opens
  useEffect(() => {
    if (isOpen && projectId) {
      setIsLoading(true);
      setError(null);
      setSaveStatus('idle');
      setShowDeleteConfirm(false);
      fetchProject(projectId)
        .then((p) => {
          setProject(p);
          setName(p.name);
          setGuide(p.guide);
        })
        .catch((err) => {
          setError(err instanceof ApiClientError ? err.message : 'Failed to load project');
        })
        .finally(() => setIsLoading(false));
    }
  }, [isOpen, projectId]);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (saveTimeoutRef.current) clearTimeout(saveTimeoutRef.current);
    };
  }, []);

  const handleSave = useCallback(async () => {
    if (!projectId || isSaving) return;

    const trimmedName = name.trim();
    if (!trimmedName) {
      setError('Project name cannot be empty');
      return;
    }

    setIsSaving(true);
    setError(null);
    setSaveStatus('idle');

    try {
      const updated = await updateProject(projectId, { name: trimmedName, guide });
      setProject(updated);
      setName(updated.name);
      setGuide(updated.guide);
      setSaveStatus('saved');
      onProjectUpdated();

      if (saveTimeoutRef.current) clearTimeout(saveTimeoutRef.current);
      saveTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 2000);
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to update project');
      }
      setSaveStatus('error');
    } finally {
      setIsSaving(false);
    }
  }, [projectId, name, guide, isSaving, onProjectUpdated]);

  const handleToggleArchived = useCallback(async () => {
    if (!projectId || !project || isArchiving) return;

    setIsArchiving(true);
    setError(null);

    try {
      const updated = project.archived
        ? await unarchiveProject(projectId)
        : await archiveProject(projectId);
      // The archive endpoints return the bare row; keep the list-only count.
      setProject({ ...updated, conversation_count: project.conversation_count });
      onProjectUpdated();
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError(project.archived ? 'Failed to unarchive project' : 'Failed to archive project');
      }
    } finally {
      setIsArchiving(false);
    }
  }, [projectId, project, isArchiving, onProjectUpdated]);

  const handleDelete = useCallback(async () => {
    if (!projectId || isDeleting) return;

    setIsDeleting(true);
    setError(null);

    try {
      await deleteProject(projectId);
      onProjectDeleted();
      onClose();
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to delete project');
      }
    } finally {
      setIsDeleting(false);
      setShowDeleteConfirm(false);
    }
  }, [projectId, isDeleting, onProjectDeleted, onClose]);

  const renderActiveSection = () => {
    if (isLoading) {
      return <div className="project-settings-loading">Loading project...</div>;
    }

    switch (activeSection) {
      case 'general':
        return (
          <>
            {project?.public && (
              <div className="project-settings-public-note">
                Public project — conversations have internet access from the
                code sandbox and no access to your internal data or connected
                services. This cannot be changed.
              </div>
            )}
            <div className="project-settings-field">
              <label htmlFor="project-settings-name" className="project-settings-label">
                Project Name
              </label>
              <input
                id="project-settings-name"
                type="text"
                className="project-settings-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={100}
                disabled={isSaving}
              />
            </div>

            <div className="project-settings-field">
              <label htmlFor="project-settings-guide" className="project-settings-label">
                Project Instructions
                <span className="project-settings-label-hint">
                  Custom instructions for all conversations in this project
                </span>
              </label>
              <textarea
                id="project-settings-guide"
                className="project-settings-textarea"
                value={guide}
                onChange={(e) => setGuide(e.target.value)}
                placeholder="Enter project-specific instructions..."
                rows={8}
                disabled={isSaving}
              />
              <div className="project-settings-char-count">
                {guide.length.toLocaleString()} / 16,384
              </div>
            </div>

            {error && <div className="project-settings-error">{error}</div>}

            <div className="project-settings-actions">
              <button
                className="project-settings-save-button"
                onClick={handleSave}
                disabled={isSaving || !name.trim()}
              >
                {isSaving ? 'Saving...' : saveStatus === 'saved' ? 'Saved!' : 'Save Changes'}
              </button>
            </div>
          </>
        );

      case 'skills':
        return projectId ? <ProjectSkillsSection projectId={projectId} /> : null;

      case 'danger-zone':
        return (
          <div className="project-settings-danger-zone">
            <h3>Danger Zone</h3>
            {error && <div className="project-settings-error">{error}</div>}

            <div className="project-settings-danger-action">
              <h4>{project?.archived ? 'Unarchive Project' : 'Archive Project'}</h4>
              <p>
                {project?.archived
                  ? 'This project is archived: hidden from the projects list and its scheduled routines are paused. Unarchiving puts it back and resumes its schedules.'
                  : 'Hides the project from the projects list and pauses its scheduled routines. Nothing is deleted; use "Show Archived" in the projects list to find it again.'}
              </p>
              <button
                className="project-settings-archive-button"
                onClick={handleToggleArchived}
                disabled={isArchiving || !project}
              >
                {isArchiving
                  ? (project?.archived ? 'Unarchiving...' : 'Archiving...')
                  : (project?.archived ? 'Unarchive Project' : 'Archive Project')}
              </button>
            </div>

            <div className="project-settings-danger-action">
              <h4>Delete Project</h4>
              <p>
                Deleting this project will permanently remove all conversations and files in this project.
                {project && project.conversation_count > 0 && (
                  <strong> This project has {project.conversation_count} conversation{project.conversation_count !== 1 ? 's' : ''}.</strong>
                )}
              </p>
              {showDeleteConfirm ? (
                <div className="project-settings-delete-confirm">
                  <span>Are you sure? This cannot be undone.</span>
                  <button
                    className="project-settings-delete-confirm-button"
                    onClick={handleDelete}
                    disabled={isDeleting}
                  >
                    {isDeleting ? 'Deleting...' : 'Yes, Delete Project'}
                  </button>
                  <button
                    className="project-settings-delete-cancel-button"
                    onClick={() => setShowDeleteConfirm(false)}
                    disabled={isDeleting}
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  className="project-settings-delete-button"
                  onClick={() => setShowDeleteConfirm(true)}
                >
                  Delete Project
                </button>
              )}
            </div>
          </div>
        );

      default:
        return null;
    }
  };

  if (!isOpen || !projectId) return null;

  const openSection = (section: ProjectSettingsSection) => {
    setActiveSection(section);
    setMobileSectionOpen(true);
  };

  const renderNavItem = ({ id, label }: SectionEntry, extraClass = '') => (
    <button
      key={id}
      className={`settings-nav-item ${extraClass} ${!isMobile && activeSection === id ? 'active' : ''}`}
      onClick={() => openSection(id)}
    >
      <span>{label}</span>
      {isMobile && (
        <svg className="settings-nav-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="9 18 15 12 9 6"></polyline>
        </svg>
      )}
    </button>
  );

  const showMobileSection = isMobile && mobileSectionOpen;

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      onEscape={handleEscape}
      overlayClassName={`project-settings-overlay ${isMobile ? 'project-settings-overlay-mobile' : ''}`}
      modalClassName={`project-settings-modal ${isMobile ? 'project-settings-modal-mobile' : ''} ${showMobileSection ? 'project-settings-mobile-section-open' : ''}`}
    >
      <div className="project-settings-header">
        {showMobileSection && (
          <button className="settings-back-button" onClick={() => setMobileSectionOpen(false)} aria-label="Back to project settings sections">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="15 18 9 12 15 6"></polyline>
            </svg>
          </button>
        )}
        <h2>{showMobileSection ? SECTION_LABELS[activeSection] : 'Project Settings'}</h2>
        <button className="project-settings-close-button" onClick={onClose} aria-label="Close project settings">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>

      <div className="project-settings-body">
        {/* Left sidebar (desktop) / first-tier section list (mobile) */}
        <nav className="settings-nav">
          <div className="settings-nav-top">
            {(project?.public ? PUBLIC_MAIN_SECTIONS : MAIN_SECTIONS).map((section) => renderNavItem(section))}
          </div>
          <div className="settings-nav-bottom">
            {renderNavItem(DANGER_ZONE_SECTION, 'settings-nav-signout')}
          </div>
        </nav>

        {/* Right content (desktop) / slide-over second tier (mobile) */}
        <div className="project-settings-content">
          {renderActiveSection()}
        </div>
      </div>
    </ModalShell>
  );
}
