// The main pane of a /autoprojects URL: with the `autoprojects` gate closed
// every route shows the gate notice and mounts none of the views (so nothing
// is fetched); with it open the route picks the list, New AutoProject or the
// AutoProject page (a run URL is the page with that run open). The views are
// stubbed: each has its own tests.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { AutoProjectsRoute } from '../../utils/autoprojectsRoute';
import { AutoProjectsMain } from './AutoProjectsMain';

vi.mock('./AutoProjectsListView', () => ({
  AutoProjectsListView: ({ archived }: { archived: boolean }) => <div data-testid="view">list:{String(archived)}</div>,
}));
vi.mock('./NewAutoProjectPage', () => ({
  NewAutoProjectPage: () => <div data-testid="view">new</div>,
}));
vi.mock('./AutoProjectPage', () => ({
  AutoProjectPage: ({ autoprojectId, runConversationId }: { autoprojectId: string; runConversationId: string | null }) => (
    <div data-testid="view">page:{autoprojectId}:{runConversationId ?? '-'}</div>
  ),
}));

const ROUTES: [string, AutoProjectsRoute, string][] = [
  ['the list', { kind: 'list', archived: false }, 'list:false'],
  ['the archive', { kind: 'list', archived: true }, 'list:true'],
  ['New AutoProject', { kind: 'new' }, 'new'],
  ['an AutoProject page', { kind: 'page', autoprojectId: 'ap1' }, 'page:ap1:-'],
  ['a run view', { kind: 'run', autoprojectId: 'ap1', conversationId: 'c9' }, 'page:ap1:c9'],
];

afterEach(cleanup);

describe('AutoProjectsMain', () => {
  it.each(ROUTES)('shows only the gate notice for %s while the gate is closed', (_name, route) => {
    render(<AutoProjectsMain route={route} enabled={false} />);
    expect(screen.getByRole('status').textContent).toContain('AutoProjects are turned off for your account.');
    expect(screen.getByText('Ask an admin to turn them on in Settings > Features.')).toBeTruthy();
    expect(screen.queryByTestId('view')).toBeNull();
  });

  it.each(ROUTES)('renders %s while the gate is open', (_name, route, view) => {
    render(<AutoProjectsMain route={route} enabled />);
    expect(screen.getByTestId('view').textContent).toBe(view);
    expect(screen.queryByText(/turned off/)).toBeNull();
  });
});
