// Sidebar: the Tables block follows the `tables` feature gate exactly like the
// Docs block -- rendered right under Docs while open, absent (and nothing
// fetched) while closed -- and its rows / header navigate to the /tables URLs.
// Everything around the docs and tables blocks is stubbed.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { Project, TableListRow } from '../api/types';
import type { UseTablesOptions } from '../hooks/useTables';
import { Sidebar } from './Sidebar';

const mocks = vi.hoisted(() => ({
  enabledFeatures: [] as string[],
  projects: [] as Project[],
  drilledProjectId: null as string | null,
  useTables: vi.fn(),
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ userEmail: 'a@example.com', userName: 'A', enabledFeatures: mocks.enabledFeatures }),
}));
vi.mock('../contexts/AppConfigContext', () => ({
  useAppConfig: () => ({ appName: 'Quest', updateAvailable: false }),
}));
vi.mock('../contexts/ConversationModelsContext', () => ({
  useConversationModels: () => ({ setModelForConversation: vi.fn() }),
}));
vi.mock('../contexts/ConversationSkillsContext', () => ({
  useConversationSkills: () => ({ setLoadedSkillsForConversation: vi.fn() }),
}));
vi.mock('../contexts/NavigationContext', () => ({
  useNavigationState: () => ({
    setSettingsOpen: vi.fn(),
    showRequestsView: false,
    setShowRequestsView: vi.fn(),
    setScrollToMessageIndex: vi.fn(),
    setPendingRoutineMessage: vi.fn(),
  }),
}));
vi.mock('../contexts/ProjectsContext', () => ({
  useProjects: () => ({
    projects: mocks.projects,
    projectsLoaded: true,
    loadProjects: vi.fn(),
    activeProjectId: null,
    setActiveProjectId: vi.fn(),
    drilledProjectId: mocks.drilledProjectId,
    setDrilledProjectId: vi.fn(),
  }),
}));
vi.mock('../hooks/useTopLevelConversations', () => ({
  useTopLevelConversations: () => ({ error: null, setError: vi.fn(), refreshSilently: vi.fn() }),
}));
vi.mock('../hooks/useProjectConversations', () => ({
  useProjectConversations: () => ({ byProject: {}, loadingByProject: {}, load: vi.fn(), update: vi.fn() }),
}));
vi.mock('../hooks/useProjectRoutines', () => ({
  useProjectRoutines: () => ({ byProject: {} }),
}));
vi.mock('../hooks/useDocs', () => ({
  useDocs: () => ({ docs: [], loading: false, error: null }),
}));
vi.mock('../hooks/useTables', () => ({ useTables: mocks.useTables }));
vi.mock('../hooks/useFlipListAnimation', () => ({
  useFlipListAnimation: () => ({ current: null }),
}));

// Everything else the Sidebar renders.
vi.mock('./UserInfoBar', () => ({ UserInfoBar: () => null }));
vi.mock('./NewProjectModal', () => ({ NewProjectModal: () => null }));
vi.mock('./ConvertToProjectModal', () => ({ ConvertToProjectModal: () => null }));
vi.mock('./ProjectSettingsModal', () => ({ ProjectSettingsModal: () => null }));
vi.mock('./NewRoutineModal', () => ({ NewRoutineModal: () => null }));
vi.mock('./RoutineSettingsModal', () => ({ RoutineSettingsModal: () => null }));
vi.mock('./SearchModal', () => ({ SearchModal: () => null }));
vi.mock('./QuestLogo', () => ({ QuestLogo: () => null }));
vi.mock('./sidebar/RequestsBadge', () => ({ RequestsBadge: () => null }));
vi.mock('./sidebar/ProjectsSection', () => ({ ProjectsSection: () => null }));
vi.mock('./sidebar/ConversationsSection', () => ({ ConversationsSection: () => null }));

function tableRow(id: string, overrides: Partial<TableListRow> = {}): TableListRow {
  return {
    id, owner_id: 1, project_id: null, title: `Table ${id}`, description: '', mode: 'private',
    schema_version: 1, row_count: 0, last_write_source: null, require_approval: false,
    created_at: '2026-10-01T00:00:00', updated_at: '2026-10-01T00:00:00', scope: 'user',
    column_count: 1, shared: false, shared_with_me: false, permission: null, owner: null,
    shares: [], last_write_user: null,
    access: {
      can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
      can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
      can_require_approval: true, can_download_history: true, deny_reason: null,
      schema_deny_reason: null,
    },
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function renderSidebar(path = '/', onNavigateAway?: () => void) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Sidebar
        activeConversationId={null}
        onConversationSelect={vi.fn()}
        onNewConversation={vi.fn()}
        onNavigateAway={onNavigateAway}
      />
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe('Sidebar Tables block', () => {
  beforeEach(() => {
    mocks.enabledFeatures = [];
    mocks.projects = [];
    mocks.drilledProjectId = null;
    mocks.useTables.mockReset();
    mocks.useTables.mockImplementation((opts: UseTablesOptions) => ({
      tables: opts.enabled && !opts.projectId ? [tableRow('t1'), tableRow('t2')] : [],
      loading: false,
      loadingMore: false,
      error: null,
      hasMore: false,
      loadMore: vi.fn(),
      refresh: vi.fn(),
    }));
  });

  afterEach(() => {
    cleanup();
  });

  it('is absent and fetches nothing while the tables gate is closed', () => {
    mocks.enabledFeatures = ['docs'];
    const { container } = renderSidebar();
    expect(container.querySelector('.tables-section')).toBeNull();
    expect(screen.queryByText('Tables')).toBeNull();
    expect(mocks.useTables).toHaveBeenCalled();
    for (const [opts] of mocks.useTables.mock.calls) {
      expect((opts as UseTablesOptions).enabled).toBe(false);
    }
  });

  it('renders right under Docs while the gate is open, 5 newest from useTables', () => {
    mocks.enabledFeatures = ['docs', 'tables'];
    const { container } = renderSidebar();
    expect(mocks.useTables).toHaveBeenCalledWith(expect.objectContaining({ limit: 5, enabled: true }));
    const labels = [...container.querySelectorAll('.sidebar-panel-main .section-label')].map(
      (el) => el.textContent,
    );
    expect(labels).toEqual(['Docs', 'Tables']);
    expect(container.querySelectorAll('.table-row')).toHaveLength(2);
  });

  it('navigates to /tables/<id> and /tables, and highlights the open table', () => {
    mocks.enabledFeatures = ['tables'];
    // The phone drawer passes onNavigateAway to close itself.
    const onNavigateAway = vi.fn();
    const { container } = renderSidebar('/tables/t2', onNavigateAway);
    expect(container.querySelector('.table-row.active')?.textContent).toContain('Table t2');

    fireEvent.click(screen.getByRole('button', { name: /Table t1/ }));
    expect(screen.getByTestId('location').textContent).toBe('/tables/t1');
    expect(onNavigateAway).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTitle('All tables'));
    expect(screen.getByTestId('location').textContent).toBe('/tables');
    expect(onNavigateAway).toHaveBeenCalledTimes(2);
    expect(screen.getByTitle('All tables').getAttribute('aria-current')).toBe('page');
  });

  it("asks for the drilled project's tables only while the gate is open", () => {
    mocks.enabledFeatures = ['tables'];
    mocks.drilledProjectId = 'p1';
    mocks.projects = [{
      id: 'p1', user_id: 1, name: 'Launch', guide: '', public: false, archived: false,
      created_at: '2026-01-01T00:00:00', updated_at: null, conversation_count: 0,
    }];
    renderSidebar();
    expect(mocks.useTables).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 'p1', limit: 5, enabled: true }),
    );
  });
});
