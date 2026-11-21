/**
 * PolledReport - the shared polling hook and frame of the System Reports
 * "latest activity" sections (Latest Conversations, Projects, AutoProjects).
 *
 * usePolledReport fetches on mount and every 60s, pausing while the tab is
 * hidden; PolledReportFrame renders the section header (title, the "Updated
 * 5s ago" label, the manual refresh button) around the loading / empty /
 * table / error states. The sections share the styles of
 * LatestActiveConversationsTable.css.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';
import './LatestActiveConversationsTable.css';

const POLL_INTERVAL_MS = 60_000;

/**
 * Seconds-granularity elapsed-time label for the header ("5s ago", "2m ago",
 * "1h ago"). Distinct from formatRelativeTimestamp, which has minute
 * granularity ("just now") and is used for the last-active columns.
 */
function formatElapsed(sinceMs: number): string {
  const seconds = Math.max(0, Math.floor(sinceMs / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

export interface PolledReport<T> {
  // null until the first successful fetch.
  data: T | null;
  error: string | null;
  lastUpdated: Date | null;
  isRefreshing: boolean;
  refresh: () => void;
}

/**
 * Poll `fetcher` every 60s. Pass a stable function: a new identity (e.g. a
 * useCallback whose filter changed) restarts the poll with an immediate
 * re-fetch.
 */
export function usePolledReport<T>(fetcher: () => Promise<T>): PolledReport<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);

  // `manual` bypasses the hidden-tab guard (a user clicking refresh is by
  // definition on a visible tab); interval-driven polls stay paused while
  // hidden. In-flight dedupe applies to both paths.
  const load = useCallback(async (manual = false) => {
    if (inFlightRef.current || (!manual && document.hidden)) return;
    inFlightRef.current = true;
    setIsRefreshing(true);
    try {
      const resp = await fetcher();
      if (!mountedRef.current) return;
      setData(resp);
      setError(null);
      // Set only on success: after a failed poll the elapsed label keeps
      // growing while the error strip shows what went wrong (intended).
      setLastUpdated(new Date());
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      inFlightRef.current = false;
      if (mountedRef.current) setIsRefreshing(false);
    }
  }, [fetcher]);

  useEffect(() => {
    mountedRef.current = true;
    load();
    const interval = window.setInterval(() => load(), POLL_INTERVAL_MS);
    const onVisibilityChange = () => {
      if (!document.hidden) load();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      mountedRef.current = false;
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [load]);

  const refresh = useCallback(() => {
    load(true);
  }, [load]);

  return { data, error, lastUpdated, isRefreshing, refresh };
}

/** "Updated 5s ago", re-rendered once a second. */
function UpdatedLabel({ since }: { since: Date }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, []);
  return (
    <span className="meta">
      Updated {formatElapsed(Math.max(now, since.getTime()) - since.getTime())}
    </span>
  );
}

interface PolledReportFrameProps {
  title: string;
  report: Pick<PolledReport<unknown>, 'error' | 'lastUpdated' | 'isRefreshing' | 'refresh'>;
  // Number of rows to show; null while the first fetch is pending.
  rowCount: number | null;
  emptyLabel: string;
  // Extra header controls, left of the updated label.
  actions?: ReactNode;
  // The table, rendered once there is at least one row.
  children: ReactNode;
}

export function PolledReportFrame({
  title,
  report,
  rowCount,
  emptyLabel,
  actions,
  children,
}: PolledReportFrameProps) {
  return (
    <div className="latest-active-conversations">
      <div className="latest-active-conversations-header">
        <h3>{title}</h3>
        <div className="header-actions">
          {actions}
          {report.lastUpdated && <UpdatedLabel since={report.lastUpdated} />}
          <button
            type="button"
            className="refresh-button"
            title="Refresh now"
            aria-label="Refresh now"
            disabled={report.isRefreshing}
            onClick={report.refresh}
          >
            <RefreshCw size={12} className={report.isRefreshing ? 'spinning' : undefined} />
          </button>
        </div>
      </div>
      {rowCount === null ? (
        report.error ? null : (
          <div className="latest-active-conversations-loading">Loading...</div>
        )
      ) : rowCount === 0 ? (
        <div className="latest-active-conversations-empty">{emptyLabel}</div>
      ) : (
        children
      )}
      {report.error && (
        <div className="latest-active-conversations-error">error: {report.error}</div>
      )}
    </div>
  );
}

/** Owner cell: display name with the email beside it (email alone when unnamed). */
export function ReportUserCell({ name, email }: { name: string; email: string }) {
  return (
    <td className="col-user" title={name ? `${name} <${email}>` : email}>
      {name ? (
        <>
          <span className="user-cell-name">{name}</span>
          <span className="user-cell-email">{email}</span>
        </>
      ) : (
        <span className="user-cell-name">{email}</span>
      )}
    </td>
  );
}
