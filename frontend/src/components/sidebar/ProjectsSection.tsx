/**
 * The Sidebar's "Projects" section: one drill-down row per project, or a
 * "Create Project" call-to-action while the user has none.
 *
 * `projects` is the full list incl. archived ones (see ProjectsContext);
 * archived rows are hidden here unless the header's options menu has
 * "Show Archived" ticked, mirroring the Conversations section.
 */

import { useState } from 'react';
import type { Project } from '../../api/types';
import { ConversationFilterMenu } from './ConversationFilterMenu';
import { ChevronRightIcon, FolderIcon, FolderPlusIcon, GlobeIcon, PlusIcon } from './icons';

interface ProjectsSectionProps {
  projects: Project[];
  showArchived: boolean;
  onShowArchivedChange: (show: boolean) => void;
  onOpenProject: (projectId: string) => void;
  onCreateProject: () => void;
}

export function ProjectsSection({
  projects,
  showArchived,
  onShowArchivedChange,
  onOpenProject,
  onCreateProject,
}: ProjectsSectionProps) {
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const visible = showArchived ? projects : projects.filter((p) => !p.archived);

  return (
    <div className="projects-section">
      <div className="section-header">
        <div className="section-label">Projects</div>
        <div className="section-header-actions">
          {projects.length > 0 && (
            <button
              className="section-add-button"
              onClick={onCreateProject}
              title="New Project"
              aria-label="New Project"
            >
              <PlusIcon />
            </button>
          )}
          <ConversationFilterMenu
            label="Project options"
            open={filterMenuOpen}
            onOpenChange={setFilterMenuOpen}
            options={[
              {
                label: 'Show Archived',
                checked: showArchived,
                onChange: onShowArchivedChange,
              },
            ]}
          />
        </div>
      </div>
      {projects.length === 0 ? (
        <button
          className="create-project-button"
          onClick={onCreateProject}
        >
          <FolderPlusIcon />
          Create Project
        </button>
      ) : (
        visible.map((project) => (
          <div key={project.id} className="project-group">
            <div
              className={`project-header${project.archived ? ' archived' : ''}`}
              onClick={() => onOpenProject(project.id)}
            >
              <FolderIcon className="project-folder-icon" />
              <span className="project-name">{project.name}</span>
              {project.public && (
                <GlobeIcon
                  className="project-public-badge"
                  title="Public project — internet access, no internal data"
                />
              )}
              {/* Right chevron indicating navigation */}
              <ChevronRightIcon className="project-nav-chevron" />
            </div>
          </div>
        ))
      )}
    </div>
  );
}
