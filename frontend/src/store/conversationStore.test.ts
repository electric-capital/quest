/**
 * Pending suggestion counts in ``conversationStore``.
 *
 * Each review bubble reads one count per conversation; the setters replace
 * it and stay silent when the value is unchanged, so a burst of identical
 * ``*_suggestions_changed`` events does not re-render subscribers.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { conversationStore } from './conversationStore';

let counter = 0;
const unsubscribers: Array<() => void> = [];

/** A conversation id no other test has touched (the store is a singleton). */
function freshId(): string {
  counter += 1;
  return `store-conv-${counter}`;
}

/** Count store notifications until the test ends. */
function countNotifications(): { calls: number } {
  const tally = { calls: 0 };
  unsubscribers.push(conversationStore.subscribe(() => {
    tally.calls += 1;
  }));
  return tally;
}

describe('conversationStore pending doc-note suggestions', () => {
  afterEach(() => {
    while (unsubscribers.length > 0) unsubscribers.pop()?.();
  });

  it('defaults to 0 alongside the other suggestion counts', () => {
    const state = conversationStore.getConversation(freshId());

    expect(state.pendingDocNoteSuggestions).toBe(0);
    expect(state.pendingMemorySuggestions).toBe(0);
    expect(state.pendingProjectMemorySuggestions).toBe(0);
  });

  it('setPendingDocNoteSuggestions replaces the count and notifies subscribers', () => {
    const id = freshId();
    const tally = countNotifications();

    conversationStore.setPendingDocNoteSuggestions(id, 3);
    expect(conversationStore.getConversation(id).pendingDocNoteSuggestions).toBe(3);
    expect(tally.calls).toBe(1);

    conversationStore.setPendingDocNoteSuggestions(id, 0);
    expect(conversationStore.getConversation(id).pendingDocNoteSuggestions).toBe(0);
    expect(tally.calls).toBe(2);
  });

  it('an unchanged count neither notifies nor replaces the state object', () => {
    const id = freshId();
    conversationStore.setPendingDocNoteSuggestions(id, 2);
    const before = conversationStore.getConversationSnapshot(id);
    const tally = countNotifications();

    conversationStore.setPendingDocNoteSuggestions(id, 2);

    expect(tally.calls).toBe(0);
    expect(conversationStore.getConversationSnapshot(id)).toBe(before);
  });

  it('leaves the personal and project memory counts alone, and they leave it alone', () => {
    const id = freshId();
    conversationStore.setPendingMemorySuggestions(id, 1);
    conversationStore.setPendingProjectMemorySuggestions(id, 4);

    conversationStore.setPendingDocNoteSuggestions(id, 5);
    let state = conversationStore.getConversation(id);
    expect(state.pendingDocNoteSuggestions).toBe(5);
    expect(state.pendingMemorySuggestions).toBe(1);
    expect(state.pendingProjectMemorySuggestions).toBe(4);

    conversationStore.setPendingProjectMemorySuggestions(id, 0);
    conversationStore.setPendingMemorySuggestions(id, 0);
    state = conversationStore.getConversation(id);
    expect(state.pendingDocNoteSuggestions).toBe(5);
  });

  it('is kept per conversation', () => {
    const a = freshId();
    const b = freshId();

    conversationStore.setPendingDocNoteSuggestions(a, 7);

    expect(conversationStore.getConversation(a).pendingDocNoteSuggestions).toBe(7);
    expect(conversationStore.getConversation(b).pendingDocNoteSuggestions).toBe(0);
  });
});
