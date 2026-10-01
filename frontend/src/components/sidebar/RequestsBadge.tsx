/**
 * Self-contained badge for the open-action-request count.
 *
 * Initial value comes from a one-shot REST fetch on mount. Subsequent
 * updates arrive via the persistent WS ``request_count_changed`` event,
 * with the local same-tab ``onRequestCountChange`` bus kept as a latency
 * shortcut for clicks in this tab (see open question 5 in 00062).
 */

import { useEffect, useRef, useState } from 'react';
import { fetchActionRequestCounts } from '../../api/client';
import { onRequestCountChange } from '../../services/requestEvents';
import { persistentWebSocket } from '../../services/PersistentWebSocket';

export function RequestsBadge() {
  const [count, setCount] = useState(0);
  const inFlightRef = useRef(false);

  const loadCount = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const resp = await fetchActionRequestCounts();
      setCount(resp.counts.open);
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
        setCount(counts.open);
      } else {
        loadCount();
      }
    });
  }, []);

  if (count <= 0) return null;
  return <span className="requests-badge">{count > 9 ? '9+' : count}</span>;
}
