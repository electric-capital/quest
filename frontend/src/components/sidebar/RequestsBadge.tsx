/**
 * Self-contained badge for everything unanswered in the inbox: open action
 * requests plus suggestions awaiting review (personal and project memories,
 * doc notes).
 *
 * Initial values come from one-shot REST fetches on mount. Subsequent
 * updates arrive via the persistent WS per-user globals --
 * ``request_count_changed`` for requests, ``memory_suggestions_changed`` /
 * ``project_memory_suggestions_changed`` / ``doc_note_suggestions_changed``
 * (their ``total_pending``) for the three suggestion kinds -- with the local
 * same-tab ``onRequestCountChange`` bus kept as a latency shortcut for
 * clicks in this tab (see open question 5 in 00062).
 */

import { useEffect, useRef, useState } from 'react';
import { fetchActionRequestCounts, fetchMemoryInboxCounts } from '../../api/client';
import { onRequestCountChange } from '../../services/requestEvents';
import { persistentWebSocket } from '../../services/PersistentWebSocket';

/** The suggestion kind whose ``total_pending`` a per-user global carries, if any. */
function suggestionCountKind(type: string | undefined): 'user' | 'project' | 'doc' | null {
  switch (type) {
    case 'memory_suggestions_changed': return 'user';
    case 'project_memory_suggestions_changed': return 'project';
    case 'doc_note_suggestions_changed': return 'doc';
    default: return null;
  }
}

export function RequestsBadge() {
  const [openRequests, setOpenRequests] = useState(0);
  const [pendingMemories, setPendingMemories] = useState({ user: 0, project: 0, doc: 0 });
  const inFlightRef = useRef(false);
  const memoriesInFlightRef = useRef(false);

  const loadCount = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const resp = await fetchActionRequestCounts();
      setOpenRequests(resp.counts.open);
    } catch {
      // Silently ignore fetch errors
    } finally {
      inFlightRef.current = false;
    }
  };

  const loadMemoryCounts = async () => {
    if (memoriesInFlightRef.current) return;
    memoriesInFlightRef.current = true;
    try {
      const resp = await fetchMemoryInboxCounts();
      setPendingMemories({ user: resp.user, project: resp.project, doc: resp.doc });
    } catch {
      // Silently ignore fetch errors
    } finally {
      memoriesInFlightRef.current = false;
    }
  };

  // One-shot fetches on mount.
  useEffect(() => {
    loadCount();
    loadMemoryCounts();
  }, []);

  // Local same-tab signal (e.g. immediate update after Approve/Deny in this tab).
  useEffect(() => {
    return onRequestCountChange(loadCount);
  }, []);

  // Persistent WS: cross-tab + cross-device updates.
  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type === 'request_count_changed') {
        const counts = (event.counts as Record<string, number> | undefined) ?? null;
        if (counts && typeof counts.open === 'number') {
          setOpenRequests(counts.open);
        } else {
          loadCount();
        }
        return;
      }
      const kind = suggestionCountKind(event.type);
      if (!kind) return;
      const total = event.total_pending;
      if (typeof total === 'number') {
        setPendingMemories((prev) => ({ ...prev, [kind]: total }));
      } else {
        loadMemoryCounts();
      }
    });
  }, []);

  const count = openRequests + pendingMemories.user + pendingMemories.project + pendingMemories.doc;
  if (count <= 0) return null;
  return <span className="requests-badge">{count > 9 ? '9+' : count}</span>;
}
