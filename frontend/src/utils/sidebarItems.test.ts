import { describe, expect, it } from 'vitest';
import type { Conversation, Routine } from '../api/types';
import {
  applyConversationFilters,
  buildSortedSidebarItems,
  deriveProjectSidebarItems,
  groupConversationsByRoutine,
} from './sidebarItems';

function conversation(overrides: Partial<Conversation> & { id: string }): Conversation {
  return {
    title: overrides.id,
    created_at: '2026-01-01T00:00:00Z',
    last_message_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function routine(id: string, name: string): Routine {
  return {
    id,
    name,
    project_id: 'p1',
    user_id: 1,
    prompt: '',
    guide_id: null,
    model: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: null,
  };
}

describe('applyConversationFilters', () => {
  const rows = [
    conversation({ id: 'plain' }),
    conversation({ id: 'archived', archived: true }),
    conversation({ id: 'slack', origin: 'slack' }),
    conversation({ id: 'inference', origin: 'inference_api' }),
  ];

  it('hides archived, Slack and Inference API rows while their toggles are off', () => {
    expect(applyConversationFilters(rows, false, false, false).map((c) => c.id)).toEqual(['plain']);
  });

  it('reveals each category independently', () => {
    expect(applyConversationFilters(rows, true, false, false).map((c) => c.id)).toEqual(['plain', 'archived']);
    expect(applyConversationFilters(rows, false, true, false).map((c) => c.id)).toEqual(['plain', 'slack']);
    expect(applyConversationFilters(rows, false, false, true).map((c) => c.id)).toEqual(['plain', 'inference']);
  });
});

describe('groupConversationsByRoutine', () => {
  it('folds runs of a known routine into one group and keeps the rest ungrouped', () => {
    const routines = [routine('r1', 'Daily digest')];
    const rows = [
      conversation({ id: 'run-new', routine_id: 'r1', last_message_at: '2026-01-03T00:00:00Z' }),
      conversation({ id: 'chat', last_message_at: '2026-01-02T00:00:00Z' }),
      conversation({ id: 'run-old', routine_id: 'r1', last_message_at: '2026-01-01T00:00:00Z' }),
      // A routine_id the project no longer has (deleted routine) stays a plain row.
      conversation({ id: 'orphan', routine_id: 'gone' }),
    ];
    const { ungrouped, routineGroups } = groupConversationsByRoutine(rows, routines);
    expect(ungrouped.map((c) => c.id)).toEqual(['chat', 'orphan']);
    expect(routineGroups).toHaveLength(1);
    expect(routineGroups[0].routineName).toBe('Daily digest');
    expect(routineGroups[0].conversations.map((c) => c.id)).toEqual(['run-new', 'run-old']);
  });

  it('orders groups by their most recent run', () => {
    const routines = [routine('a', 'A'), routine('b', 'B')];
    const rows = [
      conversation({ id: 'a1', routine_id: 'a', last_message_at: '2026-01-01T00:00:00Z' }),
      conversation({ id: 'b1', routine_id: 'b', last_message_at: '2026-01-05T00:00:00Z' }),
    ];
    const { routineGroups } = groupConversationsByRoutine(rows, routines);
    expect(routineGroups.map((g) => g.routineId)).toEqual(['b', 'a']);
  });
});

describe('buildSortedSidebarItems', () => {
  it('interleaves groups and standalone rows by most recent activity', () => {
    const items = buildSortedSidebarItems(
      [
        conversation({ id: 'newest', last_message_at: '2026-01-09T00:00:00Z' }),
        conversation({ id: 'oldest', last_message_at: '2026-01-01T00:00:00Z' }),
      ],
      [{
        routineId: 'r1',
        routineName: 'R',
        conversations: [conversation({ id: 'run', last_message_at: '2026-01-05T00:00:00Z' })],
      }],
    );
    expect(items.map((i) => (i.type === 'conversation' ? i.conversation.id : `group:${i.group.routineId}`)))
      .toEqual(['newest', 'group:r1', 'oldest']);
  });
});

describe('deriveProjectSidebarItems', () => {
  it('applies the project filters before grouping and never shows inference runs', () => {
    const routines = [routine('r1', 'R')];
    const rows = [
      conversation({ id: 'run-archived', routine_id: 'r1', archived: true }),
      conversation({ id: 'run', routine_id: 'r1' }),
      conversation({ id: 'slack', origin: 'slack' }),
      conversation({ id: 'inference', origin: 'inference_api' }),
    ];
    const hidden = deriveProjectSidebarItems(rows, routines, { showArchived: false, showSlack: false });
    expect(hidden).toHaveLength(1);
    expect(hidden[0].type).toBe('routine-group');
    expect(hidden[0].type === 'routine-group' && hidden[0].group.conversations.map((c) => c.id)).toEqual(['run']);

    const shown = deriveProjectSidebarItems(rows, routines, { showArchived: true, showSlack: true });
    const ids = shown.map((i) => (i.type === 'conversation' ? i.conversation.id : `group:${i.group.routineId}`));
    expect(ids).toContain('slack');
    expect(ids).not.toContain('inference');
    const group = shown.find((i) => i.type === 'routine-group');
    expect(group && group.type === 'routine-group' && group.group.conversations).toHaveLength(2);
  });
});
