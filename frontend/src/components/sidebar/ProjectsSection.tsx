/**
 * The Sidebar's "Projects" section: one drill-down row per project, or a
 * "Create Project" call-to-action while the user has none.
 */

import type { Project } from '../../api/types';
import { ChevronRightIcon, FolderIcon, FolderPlusIcon, GlobeIcon, PlusIcon } from './icons';

interface ProjectsSectionProps {
  projects: Project[];
  onOpenProject: (projectId: string) => void;
  onCreateProject: () => void;
}

export function ProjectsSection({ projects, onOpenProject, onCreateProject }: ProjectsSectionProps) {
  return (
    <div className="projects-section">
      <div className="section-header">
        <div className="section-label">Projects</div>
        {projects.length > 0 && (
          <button
            className="section-add-button"
            onClick={onCreateProject}
            title="New Project"
          >
            <PlusIcon />
          </button>
        )}
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
        projects.map((project) => (
          <div key={project.id} className="project-group">
            <div
              className="project-header"
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
