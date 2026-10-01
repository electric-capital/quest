/**
 * Projects and project navigation: the user's project list (loaded once
 * authenticated, re-loaded by the project modals), the URL-mirrored active
 * project, and the project the Sidebar is drilled into.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { fetchProjects } from '../api/client';
import type { Project } from '../api/types';
import { useAuth } from './AuthContext';

export interface ProjectsContextValue {
  projects: Project[];
  projectsLoaded: boolean;
  loadProjects: () => Promise<void>;
  // Mirrors the URL's project param (App.tsx); null on "/" and "/chats/:id".
  activeProjectId: string | null;
  setActiveProjectId: (id: string | null) => void;
  // The project the Sidebar is currently drilled into (its per-project view).
  // Distinct from activeProjectId, which mirrors the URL and is nulled whenever
  // the URL is "/" -- e.g. right after drilling into an EMPTY project, which
  // shows the home composer. The home composer reads this to create its first
  // conversation inside the drilled project instead of as a standalone chat.
  drilledProjectId: string | null;
  setDrilledProjectId: (id: string | null) => void;
}

const ProjectsContext = createContext<ProjectsContextValue | null>(null);

export function ProjectsProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [drilledProjectId, setDrilledProjectId] = useState<string | null>(null);

  const loadProjects = useCallback(async () => {
    try {
      const response = await fetchProjects();
      setProjects(response.projects);
      setProjectsLoaded(true);
    } catch (err) {
      console.error('Failed to load projects:', err);
    }
  }, []);

  // Load projects once authenticated
  useEffect(() => {
    if (isAuthenticated) {
      loadProjects();
    }
  }, [isAuthenticated, loadProjects]);

  const value = useMemo<ProjectsContextValue>(() => ({
    projects,
    projectsLoaded,
    loadProjects,
    activeProjectId,
    setActiveProjectId,
    drilledProjectId,
    setDrilledProjectId,
  }), [projects, projectsLoaded, loadProjects, activeProjectId, drilledProjectId]);

  return <ProjectsContext.Provider value={value}>{children}</ProjectsContext.Provider>;
}

export function useProjects(): ProjectsContextValue {
  const context = useContext(ProjectsContext);
  if (!context) {
    throw new Error('useProjects must be used within a ProjectsProvider');
  }
  return context;
}
