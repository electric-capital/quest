/**
 * Self-contained badge for everything unanswered in the inbox: open action
 * requests plus suggestions awaiting review (personal and project memories,
 * doc notes).
 *
 * The open requests come from a one-shot REST fetch on mount, then the
 * persistent WS per-user global ``request_count_changed``, with the local
 * same-tab ``onRequestCountChange`` bus kept as a latency shortcut for
 * clicks in this tab (see open question 5 in 00062). The suggestion counts
 * are usePendingSuggestionCounts, shared with the inbox's Memories tab
 * label.
 */

import { useEffect, useRef, useState } from 'react';
import { fetchActionRequestCounts } from '../../api/client';
import {
  totalPendingSuggestions,
  usePendingSuggestionCounts,
} from '../../hooks/usePendingSuggestionCounts';
import { onRequestCountChange } from '../../services/requestEvents';
import { persistentWebSocket } from '../../services/PersistentWebSocket';

export function RequestsBadge() {
  const [openRequests, setOpenRequests] = useState(0);
  const pendingSuggestions = usePendingSuggestionCounts();
  const inFlightRef = useRef(false);

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

  // One-shot fetch on mount.
  useEffect(() => {
    loadCount();
  }, []);

  // Local same-tab signal (e.g. immediate update after Approve/Deny in this tab).
  useEffect(() => {
    return onRequestCountChange(loadCount);
  }, []);

  // Persistent WS: cross-tab + cross-device updates.
  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'request_count_changed') return;
      const counts = (event.counts as Record<string, number> | undefined) ?? null;
      if (counts && typeof counts.open === 'number') {
        setOpenRequests(counts.open);
      } else {
        loadCount();
      }
    });
  }, []);

  const count = openRequests + totalPendingSuggestions(pendingSuggestions);
  if (count <= 0) return null;
  return <span className="requests-badge">{count > 9 ? '9+' : count}</span>;
}
