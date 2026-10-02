import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Routine } from '../api/types';
import { useProjectRoutines } from './useProjectRoutines';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  fetchProjectRoutines: vi.fn<(projectId: string) => Promise<{ routines: Routine[] }>>(),
  global: new Set<GlobalListener>(),
}));

vi.mock('../api/client', () => ({
  fetchProjectRoutines: mocks.fetchProjectRoutines,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

function routine(id: string, projectId: string): Routine {
  return {
    id,
    project_id: projectId,
    name: `Routine ${id}`,
    prompt: 'do the thing',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as Routine;
}

beforeEach(() => {
  mocks.fetchProjectRoutines.mockReset();
  mocks.global.clear();
});

describe('useProjectRoutines', () => {
  it('loads the drilled project routines on mount so a remount regroups runs', async () => {
    mocks.fetchProjectRoutines.mockResolvedValue({ routines: [routine('r1', 'p1')] });

    // Simulates the Sidebar being re-created (e.g. the viewport crossing the
    // phone breakpoint) while drilledProjectId is still set in context.
    const { result } = renderHook(() => useProjectRoutines('p1'));

    await waitFor(() => expect(result.current.byProject.p1).toHaveLength(1));
    expect(mocks.fetchProjectRoutines).toHaveBeenCalledTimes(1);
    expect(mocks.fetchProjectRoutines).toHaveBeenCalledWith('p1');
  });

  it('fetches when the drilled project changes and reuses cached lists', async () => {
    mocks.fetchProjectRoutines.mockImplementation(async (projectId) => ({
      routines: [routine(`${projectId}-r`, projectId)],
    }));

    const { result, rerender } = renderHook(
      ({ drilled }: { drilled: string | null }) => useProjectRoutines(drilled),
      { initialProps: { drilled: null as string | null } },
    );
    expect(mocks.fetchProjectRoutines).not.toHaveBeenCalled();

    rerender({ drilled: 'p1' });
    await waitFor(() => expect(result.current.byProject.p1).toHaveLength(1));

    rerender({ drilled: 'p2' });
    await waitFor(() => expect(result.current.byProject.p2).toHaveLength(1));

    // Back to p1: cached, no refetch.
    rerender({ drilled: 'p1' });
    await waitFor(() => expect(result.current.byProject.p1).toHaveLength(1));
    expect(mocks.fetchProjectRoutines).toHaveBeenCalledTimes(2);
  });

  it('refetches a project on routine_list_changed', async () => {
    mocks.fetchProjectRoutines.mockResolvedValue({ routines: [] });
    const { result } = renderHook(() => useProjectRoutines('p1'));
    await waitFor(() => expect(result.current.byProject.p1).toEqual([]));

    mocks.fetchProjectRoutines.mockResolvedValue({ routines: [routine('r1', 'p1')] });
    for (const cb of mocks.global) cb({ type: 'routine_list_changed', project_id: 'p1' });

    await waitFor(() => expect(result.current.byProject.p1).toHaveLength(1));
  });
});
