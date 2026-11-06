/**
 * The inbox's Other tab: system notifications, things the system did for
 * the user that need no decision (unlike the Requests and Memories tabs).
 *
 * Open notifications come first, each with its title, plain-text body and
 * a Dismiss button; a kind this tab knows may add a link (a
 * ``guides_converted`` notification opens Settings > Skills, where the
 * converted skills are). Dismissed ones stay listed below, muted. The list
 * comes from ``/app/api/notifications`` (chat/notification_routes.py) and
 * is re-read on every ``notifications_changed`` event, so a dismissal in
 * another tab shows here too. The newest read to start wins, and a row
 * dismissed here stays dismissed whatever an older read says.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Bell, LoaderCircle } from 'lucide-react';
import { dismissNotification, fetchNotifications } from '../api/client';
import type { UserNotification } from '../api/types';
import { useNavigationState } from '../contexts/NavigationContext';
import { NOTIFICATIONS_CHANGED_EVENT } from '../hooks/useOpenNotificationCount';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { formatRelativeTimestamp } from '../utils/formatters';
import './MemorySuggestions.css';
import './MemoryInbox.css';
import './NotificationsInbox.css';

/** A link a notification kind offers: its label and the Settings section it opens. */
const KIND_SETTINGS_LINKS: Record<string, { label: string; section: string }> = {
  guides_converted: { label: 'Open Skills', section: 'skills' },
};

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export function NotificationsInbox() {
  const { setSettingsOpen, setSettingsInitialSection } = useNavigationState();
  // null until the first fetch lands.
  const [notifications, setNotifications] = useState<UserNotification[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  // Reads are numbered as they start; one older than the last applied is dropped.
  const readSeqRef = useRef(0);
  const appliedSeqRef = useRef(0);
  // When each row dismissed here was dismissed, kept over any read.
  const dismissedHereRef = useRef(new Map<string, string>());

  const load = useCallback(async () => {
    const seq = ++readSeqRef.current;
    try {
      const resp = await fetchNotifications();
      if (seq < appliedSeqRef.current) return;
      appliedSeqRef.current = seq;
      setNotifications(resp.notifications.map((row) => {
        const dismissedAt = dismissedHereRef.current.get(row.id);
        return dismissedAt && !row.dismissed_at ? { ...row, dismissed_at: dismissedAt } : row;
      }));
      setLoadError(null);
    } catch (err) {
      if (seq < appliedSeqRef.current) return;
      setLoadError(errorMessage(err, 'Failed to load notifications'));
      setNotifications((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== NOTIFICATIONS_CHANGED_EVENT) return;
      void load();
    });
  }, [load]);

  const dismiss = useCallback(async (notification: UserNotification) => {
    const { id } = notification;
    setBusyIds((prev) => new Set(prev).add(id));
    setRowErrors((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try {
      const resp = await dismissNotification(id);
      const dismissedAt = resp.notification.dismissed_at ?? new Date().toISOString();
      dismissedHereRef.current.set(id, dismissedAt);
      setNotifications((prev) => prev && prev.map(
        (row) => (row.id === id ? { ...row, dismissed_at: dismissedAt } : row),
      ));
    } catch (err) {
      setRowErrors((prev) => ({ ...prev, [id]: errorMessage(err, 'Failed to dismiss') }));
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  }, []);

  const openSettings = useCallback((section: string) => {
    setSettingsInitialSection(section);
    setSettingsOpen(true);
  }, [setSettingsInitialSection, setSettingsOpen]);

  const open = notifications?.filter((n) => !n.dismissed_at) ?? [];
  const dismissed = notifications?.filter((n) => n.dismissed_at) ?? [];

  const renderCard = (notification: UserNotification) => {
    const isOpen = !notification.dismissed_at;
    const link = KIND_SETTINGS_LINKS[notification.kind];
    return (
      <article
        key={notification.id}
        className={`notification-card${isOpen ? ' notification-card--open' : ' notification-card--dismissed'}`}
        aria-label={notification.title}
      >
        <div className="notification-card-header">
          <Bell size={14} className="notification-card-icon" aria-hidden="true" />
          <h4 className="notification-card-title">{notification.title}</h4>
          <span className="memory-inbox-time" title={notification.created_at}>
            {formatRelativeTimestamp(notification.created_at)}
          </span>
        </div>
        {notification.body && (
          <div className="notification-card-body">{notification.body}</div>
        )}
        {rowErrors[notification.id] && (
          <div className="memory-suggestions-error" role="alert">{rowErrors[notification.id]}</div>
        )}
        {(isOpen || link) && (
          <div className="notification-card-actions">
            {link && (
              <button
                type="button"
                className="notification-card-button notification-card-button--primary"
                onClick={() => openSettings(link.section)}
              >
                {link.label}
              </button>
            )}
            {isOpen && (
              <button
                type="button"
                className="notification-card-button"
                onClick={() => dismiss(notification)}
                disabled={busyIds.has(notification.id)}
              >
                {busyIds.has(notification.id) ? 'Dismissing...' : 'Dismiss'}
              </button>
            )}
          </div>
        )}
      </article>
    );
  };

  return (
    <div className="memory-inbox notifications-inbox">
      <section className="memory-inbox-section" aria-labelledby="notifications-inbox-open-title">
        <div className="memory-inbox-section-header">
          <h3 id="notifications-inbox-open-title" className="memory-inbox-section-title">
            Notifications
            <span className="memory-inbox-section-count">{open.length}</span>
          </h3>
        </div>
        <p className="memory-inbox-hint">
          Messages from the system about changes made on your behalf. Nothing
          here needs your approval; dismiss a notification once you have read it.
        </p>
        {loadError && (
          <div className="memory-suggestions-error" role="alert">{loadError}</div>
        )}
        {notifications === null ? (
          <div className="memory-inbox-status">
            <LoaderCircle size={16} className="memory-suggestions-spinner" aria-hidden="true" />
            Loading notifications...
          </div>
        ) : open.length === 0 ? (
          <div className="memory-inbox-empty">No new notifications.</div>
        ) : (
          <div className="memory-inbox-list">{open.map(renderCard)}</div>
        )}
      </section>

      {dismissed.length > 0 && (
        <section className="memory-inbox-section" aria-labelledby="notifications-inbox-dismissed-title">
          <div className="memory-inbox-section-header">
            <h3 id="notifications-inbox-dismissed-title" className="memory-inbox-section-title">
              Dismissed
            </h3>
          </div>
          <div className="memory-inbox-list">{dismissed.map(renderCard)}</div>
        </section>
      )}
    </div>
  );
}
