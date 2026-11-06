// ConversationRow: the unread dot renders in front of the title while the
// conversation is unread, and goes when it is read.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import type { Conversation } from '../../api/types';
import type { ConversationListActions } from '../../hooks/useConversationListActions';
import { unreadStore } from '../../store/unreadStore';
import { ConversationRow } from './ConversationRow';

vi.mock('../../api/client', () => ({
  markConversationRead: vi.fn(),
}));

vi.mock('../../services/PersistentWebSocket', () => ({
  persistentWebSocket: { onGlobalEvent: () => () => {} },
}));

const noop = () => {};
const ACTIONS: ConversationListActions = {
  openMenuId: null,
  toggleMenu: noop,
  closeMenu: noop,
  renamingId: null,
  renameValue: '',
  setRenameValue: noop,
  startRename: noop,
  submitRename: noop,
  cancelRename: noop,
  archive: noop,
  unarchive: noop,
};

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: 'c1',
    title: 'Quarterly numbers',
    created_at: '2026-11-01T00:00:00',
    last_message_at: '2026-11-01T00:00:00',
    ...overrides,
  };
}

function renderRow(row: Conversation, variant: 'default' | 'routine-run' = 'default') {
  return render(
    <ConversationRow
      conversation={row}
      active={false}
      variant={variant}
      actions={ACTIONS}
      onSelect={noop}
    />,
  );
}

beforeEach(() => unreadStore.reset());
afterEach(cleanup);

describe('ConversationRow unread dot', () => {
  it('has no dot on a read conversation', () => {
    renderRow(conversation({ unread_seq: 4, read_seq: 4 }));
    expect(screen.queryByLabelText('Unread')).toBeNull();
  });

  it('puts the dot before the title of an unread conversation', () => {
    const { container } = renderRow(conversation({ unread_seq: 6, read_seq: 4 }));
    const title = container.querySelector('.conversation-title')!;
    expect(title.firstElementChild).toBe(screen.getByLabelText('Unread'));
    expect(title.textContent).toBe('Quarterly numbers');
  });

  it('shows the dot on a routine run row too', () => {
    renderRow(conversation({ unread_seq: 2, read_seq: 0, routine_id: 'r1' }), 'routine-run');
    expect(screen.getByLabelText('Unread')).toBeTruthy();
  });

  it('follows the store when the conversation becomes unread, then read', () => {
    renderRow(conversation());
    expect(screen.queryByLabelText('Unread')).toBeNull();

    act(() => unreadStore.merge('c1', 3, 0));
    expect(screen.getByLabelText('Unread')).toBeTruthy();

    act(() => unreadStore.merge('c1', 3, 3));
    expect(screen.queryByLabelText('Unread')).toBeNull();
  });
});
