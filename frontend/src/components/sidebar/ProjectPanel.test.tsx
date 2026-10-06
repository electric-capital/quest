// ProjectPanel: the drill-down's section order is Routines, Docs, then the
// Conversations header, and each block follows its show flag.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { Project } from '../../api/types';
import { ProjectPanel } from './ProjectPanel';

// The FLIP animation measures layout and reads matchMedia, which jsdom lacks.
vi.mock('../../hooks/useFlipListAnimation', () => ({
  useFlipListAnimation: () => ({ current: null }),
}));

const PROJECT: Project = {
  id: 'p1',
  user_id: 1,
  name: 'Launch Plan',
  guide: '',
  public: false,
  archived: false,
  created_at: '2026-01-01T00:00:00',
  updated_at: null,
  conversation_count: 0,
};

function renderPanel(props: { showRoutines: boolean; showDocs: boolean }) {
  const noop = vi.fn();
  return render(
    <ProjectPanel
      project={PROJECT}
      conversations={[]}
      loading={false}
      routines={[]}
      showRoutines={props.showRoutines}
      showArchived={false}
      onShowArchivedChange={noop}
      showSlack={false}
      onShowSlackChange={noop}
      expandedRoutineGroups={{}}
      onToggleRoutineGroup={noop}
      activeConversationId={null}
      busy={false}
      onBack={noop}
      onOpenProjectSettings={noop}
      onNewRoutine={noop}
      onRoutineSettings={noop}
      onRunRoutine={noop}
      onNewChat={noop}
      onSelect={noop}
      updateConversations={noop}
      onArchived={noop}
      showDocs={props.showDocs}
      docs={[]}
    />,
  );
}

/** True when `a` comes before `b` in document order. */
function precedes(a: Element, b: Element): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

describe('ProjectPanel', () => {
  afterEach(() => {
    cleanup();
  });

  it('orders Routines, then Docs, then the Conversations header', () => {
    const { container } = renderPanel({ showRoutines: true, showDocs: true });

    const routines = container.querySelector('.routines-section');
    const docs = container.querySelector('.docs-section');
    const conversations = screen.getByText('Conversations');
    expect(routines).not.toBeNull();
    expect(docs).not.toBeNull();
    expect(precedes(routines!, docs!)).toBe(true);
    expect(precedes(docs!, conversations)).toBe(true);

    // The section labels read in the same order.
    expect(
      [...container.querySelectorAll('.section-label')].map((label) => label.textContent),
    ).toEqual(['Routines', 'Docs', 'Conversations']);
  });

  it('leaves out the Routines and Docs blocks when their flags are off', () => {
    const { container } = renderPanel({ showRoutines: false, showDocs: false });
    expect(container.querySelector('.routines-section')).toBeNull();
    expect(container.querySelector('.docs-section')).toBeNull();
    expect(screen.getByText('Conversations')).toBeTruthy();
  });
});
