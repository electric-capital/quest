/**
 * How many of the user's system notifications are open: what the inbox's
 * Other tab label (RequestsView.tsx) and the sidebar's inbox badge
 * (sidebar/RequestsBadge.tsx) count.
 *
 * Seeded by `GET /notifications/count`, then followed through the per-user
 * global `notifications_changed`, whose `open_count` replaces the value; an
 * event without a numeric `open_count` re-fetches. A fetch that started
 * before an event never overwrites the event's value.
 */

import { useEffect, useRef, useState } from 'react';
import { fetchNotificationsCount } from '../api/client';
import { persistentWebSocket } from '../services/PersistentWebSocket';

/** The per-user global the count follows; the Other tab re-reads its list on it. */
export const NOTIFICATIONS_CHANGED_EVENT = 'notifications_changed';

export function useOpenNotificationCount(): number {
  const [count, setCount] = useState(0);
  // Ticks of a local clock: the latest event, and each fetch's start.
  const clockRef = useRef(0);
  const eventTickRef = useRef(0);

  useEffect(() => {
    let active = true;

    const load = async () => {
      const startedAt = ++clockRef.current;
      try {
        const resp = await fetchNotificationsCount();
        if (!active || eventTickRef.current > startedAt) return;
        setCount(resp.open_count);
      } catch {
        // The count keeps its last value.
      }
    };

    void load();
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== NOTIFICATIONS_CHANGED_EVENT) return;
      const openCount = event.open_count;
      if (typeof openCount !== 'number') {
        void load();
        return;
      }
      eventTickRef.current = ++clockRef.current;
      setCount(openCount);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  return count;
}
