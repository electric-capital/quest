import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Conversation } from '../api/types';
import { useConversationListActions } from './useConversationListActions';

const api = vi.hoisted(() => ({
  archiveConversation: vi.fn(() => Promise.resolve()),
  unarchiveConversation: vi.fn(() => Promise.resolve()),
  renameConversation: vi.fn(() => Promise.resolve()),
}));

vi.mock('../api/client', () => api);

function conversation(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    title: `Title ${id}`,
    created_at: '2026-01-01T00:00:00Z',
    last_message_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

/** A minimal stand-in for a data hook's `update`: applies updaters to a local list. */
function listHarness(initial: Conversation[]) {
  let rows = initial;
  return {
    get rows() {
      return rows;
    },
    update: (updater: (prev: Conversation[]) => Conversation[]) => {
      rows = updater(rows);
    },
  };
}

describe('useConversationListActions', () => {
  beforeEach(() => {
    api.archiveConversation.mockClear();
    api.unarchiveConversation.mockClear();
    api.renameConversation.mockClear();
  });

  it('drops an archived row while archived rows are hidden and tells the caller', () => {
    const list = listHarness([conversation('a'), conversation('b')]);
    const onArchived = vi.fn();
    const { result } = renderHook(() =>
      useConversationListActions({ update: list.update, showArchived: false, onArchived }),
    );

    act(() => result.current.archive('a'));

    expect(list.rows.map((c) => c.id)).toEqual(['b']);
    expect(onArchived).toHaveBeenCalledWith('a');
    expect(api.archiveConversation).toHaveBeenCalledWith('a');
  });

  it('flags an archived row in place while archived rows are shown', () => {
    const list = listHarness([conversation('a'), conversation('b')]);
    const { result } = renderHook(() =>
      useConversationListActions({ update: list.update, showArchived: true }),
    );

    act(() => result.current.archive('a'));
    expect(list.rows.map((c) => [c.id, c.archived])).toEqual([['a', true], ['b', undefined]]);

    act(() => result.current.unarchive('a'));
    expect(list.rows[0].archived).toBe(false);
    expect(api.unarchiveConversation).toHaveBeenCalledWith('a');
  });

  it('keeps one menu open at a time and closes it when a rename starts', () => {
    const list = listHarness([conversation('a'), conversation('b')]);
    const { result } = renderHook(() =>
      useConversationListActions({ update: list.update, showArchived: false }),
    );

    act(() => result.current.toggleMenu('a'));
    expect(result.current.openMenuId).toBe('a');
    act(() => result.current.toggleMenu('b'));
    expect(result.current.openMenuId).toBe('b');
    act(() => result.current.toggleMenu('b'));
    expect(result.current.openMenuId).toBeNull();

    act(() => result.current.toggleMenu('a'));
    act(() => result.current.startRename('a', 'Title a'));
    expect(result.current.openMenuId).toBeNull();
    expect(result.current.renamingId).toBe('a');
    expect(result.current.renameValue).toBe('Title a');
  });

  it('pre-fills an empty editor for the "New Chat" placeholder title', () => {
    const list = listHarness([conversation('a')]);
    const { result } = renderHook(() =>
      useConversationListActions({ update: list.update, showArchived: false }),
    );
    act(() => result.current.startRename('a', 'New Chat'));
    expect(result.current.renameValue).toBe('');
  });

  it('submits a trimmed rename optimistically and clears the custom name when emptied', () => {
    const list = listHarness([conversation('a', { custom_name: 'Old' })]);
    const { result } = renderHook(() =>
      useConversationListActions({ update: list.update, showArchived: false }),
    );

    act(() => result.current.startRename('a', 'Old'));
    act(() => result.current.setRenameValue('  Fresh name  '));
    act(() => result.current.submitRename('a'));

    expect(list.rows[0].custom_name).toBe('Fresh name');
    expect(list.rows[0].title).toBe('Fresh name');
    expect(api.renameConversation).toHaveBeenCalledWith('a', 'Fresh name');
    expect(result.current.renamingId).toBeNull();
    expect(result.current.renameValue).toBe('');

    act(() => result.current.startRename('a', 'Fresh name'));
    act(() => result.current.setRenameValue('   '));
    act(() => result.current.submitRename('a'));

    expect(list.rows[0].custom_name).toBeNull();
    // The display title falls back to the server-generated title.
    expect(list.rows[0].title).toBe('Fresh name');
    expect(api.renameConversation).toHaveBeenLastCalledWith('a', null);
  });

  it('cancelling a rename leaves the row untouched', () => {
    const list = listHarness([conversation('a')]);
    const { result } = renderHook(() =>
      useConversationListActions({ update: list.update, showArchived: false }),
    );
    act(() => result.current.startRename('a', 'Title a'));
    act(() => result.current.setRenameValue('Changed'));
    act(() => result.current.cancelRename());
    expect(result.current.renamingId).toBeNull();
    expect(list.rows[0].custom_name).toBeUndefined();
    expect(api.renameConversation).not.toHaveBeenCalled();
  });
});
