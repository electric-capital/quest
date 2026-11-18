// The Sidebar's AutoProjects block: at most five rows by newest activity, a
// "Show all (N)" link when there are more, each row's status slot (working
// spinner, "Needs you", else the time) and the unread dot, and the header,
// "+" and Archived links. The gate (block absent while closed) is covered in
// Sidebar.test.tsx.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { AutoProjectListRow } from '../../api/types';
import { AutoProjectsSection, type AutoProjectsSectionProps } from './AutoProjectsSection';

let nextId = 0;

/** A row with a unique id (the unread store is a module singleton). */
function row(title: string, overrides: Partial<AutoProjectListRow> = {}): AutoProjectListRow {
  nextId += 1;
  return {
    id: `sec-ap-${nextId}`,
    title,
    archived: false,
    last_activity_at: `2026-11-${String(10 + (nextId % 10)).padStart(2, '0')}T10:00:00`,
    active_run: null,
    unread: false,
    event_seq: 1,
    unread_seq: 0,
    read_seq: 0,
    ...overrides,
  };
}

function renderSection(props: Partial<AutoProjectsSectionProps> = {}) {
  const handlers = {
    onOpenAutoProject: vi.fn(),
    onOpenAll: vi.fn(),
    onOpenArchived: vi.fn(),
    onNewAutoProject: vi.fn(),
  };
  const utils = render(
    <AutoProjectsSection
      autoprojects={[]}
      loading={false}
      activeAutoProjectId={null}
      {...handlers}
      {...props}
    />,
  );
  return { ...utils, handlers };
}

function rowTitles(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.autoproject-row .doc-row-title')].map((el) => el.textContent ?? '');
}

afterEach(cleanup);

describe('AutoProjectsSection', () => {
  it('lists at most five AutoProjects, newest activity first, with "Show all (N)"', () => {
    const rows = [
      row('Oldest', { last_activity_at: '2026-11-01T10:00:00' }),
      row('Newest', { last_activity_at: '2026-11-07T10:00:00' }),
      row('No activity', { last_activity_at: null }),
      row('Fourth', { last_activity_at: '2026-11-03T10:00:00' }),
      row('Second', { last_activity_at: '2026-11-06T10:00:00' }),
      row('Third', { last_activity_at: '2026-11-05T10:00:00' }),
      row('Fifth', { last_activity_at: '2026-11-02T10:00:00' }),
    ];
    const { container, handlers } = renderSection({ autoprojects: rows });
    expect(rowTitles(container)).toEqual(['Newest', 'Second', 'Third', 'Fourth', 'Fifth']);

    fireEvent.click(screen.getByRole('button', { name: 'Show all (7)' }));
    expect(handlers.onOpenAll).toHaveBeenCalledTimes(1);
  });

  it('has no "Show all" link with five or fewer AutoProjects', () => {
    renderSection({ autoprojects: [row('A'), row('B'), row('C'), row('D'), row('E')] });
    expect(screen.queryByRole('button', { name: /Show all/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Archived' })).toBeTruthy();
  });

  it('marks a working run, a run that needs the user, and an unread AutoProject', () => {
    const working = row('Site build', { active_run: { id: 'r1', seq: 2, status: 'running' } });
    const waiting = row('Hiring plan', { active_run: { id: 'r2', seq: 4, status: 'waiting' } });
    const unread = row('Market study', { unread: true, unread_seq: 5, read_seq: 2 });
    const quiet = row('Quiet one');
    renderSection({ autoprojects: [working, waiting, unread, quiet] });

    const rowFor = (title: string) => screen.getByTitle(title, { selector: 'button' });
    expect(within(rowFor('Site build')).getByRole('img', { name: 'Working' })).toBeTruthy();
    expect(within(rowFor('Hiring plan')).getByText('Needs you')).toBeTruthy();
    expect(within(rowFor('Market study')).getByRole('img', { name: 'Unread' })).toBeTruthy();

    for (const title of ['Site build', 'Hiring plan', 'Quiet one']) {
      expect(within(rowFor(title)).queryByRole('img', { name: 'Unread' })).toBeNull();
    }
    expect(within(rowFor('Quiet one')).queryByText('Needs you')).toBeNull();
    expect(within(rowFor('Quiet one')).queryByRole('img', { name: 'Working' })).toBeNull();
  });

  it('opens a row, highlights the active one, and wires the header, "+" and Archived', () => {
    const a = row('Alpha');
    const b = row('Beta');
    const { handlers } = renderSection({ autoprojects: [a, b], activeAutoProjectId: b.id });

    expect(screen.getByTitle('Beta', { selector: 'button' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByTitle('Alpha', { selector: 'button' }).getAttribute('aria-current')).toBeNull();

    fireEvent.click(screen.getByTitle('Alpha', { selector: 'button' }));
    expect(handlers.onOpenAutoProject).toHaveBeenCalledWith(a.id);
    fireEvent.click(screen.getByTitle('All AutoProjects'));
    expect(handlers.onOpenAll).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'New AutoProject' }));
    expect(handlers.onNewAutoProject).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Archived' }));
    expect(handlers.onOpenArchived).toHaveBeenCalledTimes(1);
  });

  it('shows the empty hint, nothing while loading, and an error instead of the hint', () => {
    const { rerender, handlers } = renderSection({ loading: true });
    expect(screen.queryByText('No AutoProjects yet')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Archived' })).toBeNull();

    const base = { activeAutoProjectId: null, autoprojects: [], ...handlers };
    rerender(<AutoProjectsSection {...base} loading={false} />);
    expect(screen.getByText('No AutoProjects yet')).toBeTruthy();

    rerender(<AutoProjectsSection {...base} loading={false} error="boom" />);
    expect(screen.getByText("Couldn't load AutoProjects")).toBeTruthy();
    expect(screen.queryByText('No AutoProjects yet')).toBeNull();
  });
});
