/**
 * The user's pending suggestions per kind (personal memories, project
 * memories, doc notes) across all conversations: what the inbox's Memories
 * tab label (RequestsView.tsx) and the sidebar's inbox badge
 * (sidebar/RequestsBadge.tsx) count.
 *
 * Seeded by `GET /memory-inbox/counts`, then followed through each kind's
 * per-user global (`suggestionCountKind`), whose `total_pending` replaces
 * that kind's count; an event without a numeric `total_pending` re-fetches.
 * Each fetch start and each event takes the next tick of a local clock, and
 * a kind keeps the value of its latest tick: a fetch that started before an
 * event (the mount seed landing late, say) or before a newer fetch never
 * overwrites that kind.
 */

import { useEffect, useRef, useState } from 'react';
import { fetchMemoryInboxCounts } from '../api/client';
import { persistentWebSocket } from '../services/PersistentWebSocket';

export type SuggestionCountKind = 'user' | 'project' | 'doc';

/** Pending suggestion counts, one per kind. */
export type PendingSuggestionCounts = Record<SuggestionCountKind, number>;

const KINDS: readonly SuggestionCountKind[] = ['user', 'project', 'doc'];

/**
 * The suggestion kind whose pending total a per-user global event carries,
 * or null for any other event. Also what the inbox re-reads its open list on.
 */
export function suggestionCountKind(type: unknown): SuggestionCountKind | null {
  switch (type) {
    case 'memory_suggestions_changed': return 'user';
    case 'project_memory_suggestions_changed': return 'project';
    case 'doc_note_suggestions_changed': return 'doc';
    default: return null;
  }
}

/** All three counts added up. */
export function totalPendingSuggestions(counts: PendingSuggestionCounts): number {
  return counts.user + counts.project + counts.doc;
}

export function usePendingSuggestionCounts(): PendingSuggestionCounts {
  const [counts, setCounts] = useState<PendingSuggestionCounts>({ user: 0, project: 0, doc: 0 });
  const clockRef = useRef(0);
  // The tick each kind's current value came from.
  const ticksRef = useRef<PendingSuggestionCounts>({ user: 0, project: 0, doc: 0 });

  useEffect(() => {
    let active = true;

    const load = async () => {
      const startedAt = ++clockRef.current;
      try {
        const resp = await fetchMemoryInboxCounts();
        if (!active) return;
        const ticks = ticksRef.current;
        const fresh = KINDS.filter((kind) => ticks[kind] < startedAt);
        if (fresh.length === 0) return;
        for (const kind of fresh) ticks[kind] = startedAt;
        setCounts((prev) => {
          const next = { ...prev };
          for (const kind of fresh) next[kind] = resp[kind];
          return next;
        });
      } catch {
        // The counts keep their last values.
      }
    };

    void load();
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      const kind = suggestionCountKind(event.type);
      if (!kind) return;
      const total = event.total_pending;
      if (typeof total !== 'number') {
        void load();
        return;
      }
      ticksRef.current[kind] = ++clockRef.current;
      setCounts((prev) => ({ ...prev, [kind]: total }));
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  return counts;
}
