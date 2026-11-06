// ProjectsSection: a project row shows the unread dot in front of its name
// while one of the project's conversations is unread.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import type { Project } from '../../api/types';
import { unreadStore } from '../../store/unreadStore';
import { ProjectsSection } from './ProjectsSection';

vi.mock('../../api/client', () => ({
  markConversationRead: vi.fn(),
}));

vi.mock('../../services/PersistentWebSocket', () => ({
  persistentWebSocket: { onGlobalEvent: () => () => {} },
}));

const noop = () => {};

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p1',
    user_id: 1,
    name: 'Launch plan',
    guide: '',
    public: false,
    archived: false,
    created_at: '2026-11-01T00:00:00',
    updated_at: null,
    conversation_count: 2,
    unread_conversations: [],
    ...overrides,
  };
}

function renderSection(projects: Project[]) {
  return render(
    <ProjectsSection
      projects={projects}
      showArchived={false}
      onShowArchivedChange={noop}
      onOpenProject={noop}
      onCreateProject={noop}
    />,
  );
}

beforeEach(() => unreadStore.reset());
afterEach(cleanup);

describe('ProjectsSection unread dot', () => {
  it('has no dot on a project without unread conversations', () => {
    renderSection([project()]);
    expect(screen.queryByLabelText('Unread conversations')).toBeNull();
  });

  it('puts the dot before the name of a project with an unread conversation', () => {
    const { container } = renderSection([
      project({ unread_conversations: [{ id: 'c1', unread_seq: 6, read_seq: 4 }] }),
      project({ id: 'p2', name: 'Archive' }),
    ]);
    const dots = screen.getAllByLabelText('Unread conversations');
    expect(dots).toHaveLength(1);
    const name = container.querySelector('.project-name')!;
    expect(name.firstElementChild).toBe(dots[0]);
    expect(name.textContent).toBe('Launch plan');
  });

  it('follows the store as a conversation of the project becomes unread, then read', () => {
    renderSection([project()]);

    act(() => unreadStore.merge('c7', 3, 0, 'p1'));
    expect(screen.getByLabelText('Unread conversations')).toBeTruthy();

    act(() => unreadStore.merge('c7', 3, 3, 'p1'));
    expect(screen.queryByLabelText('Unread conversations')).toBeNull();
  });

  it('clears the dot when the listed conversation is read', () => {
    renderSection([
      project({ unread_conversations: [{ id: 'c1', unread_seq: 6, read_seq: 4 }] }),
    ]);

    act(() => unreadStore.merge('c1', 6, 6, 'p1'));
    expect(screen.queryByLabelText('Unread conversations')).toBeNull();
  });
});
