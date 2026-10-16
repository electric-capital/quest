/**
 * FeatureUsageReport - the shared frame and fetch hook of the System Reports
 * "Docs", "Tables" and "Memories" sections.
 *
 * The three feature-usage sections are the same report in three shapes: a
 * per-user table for a selected date range (the ReportDateRange picker of
 * Cost Analysis / Users / Models), fetched on mount, on range change and via
 * the refresh button, with a stale-response guard. `useRangedAdminReport`
 * owns that fetch cycle; `FeatureUsageReportFrame` renders the header
 * (title, summary, picker, refresh), the loading / empty / error states and
 * wraps the section's own `<table>`. The small cell helpers keep the three
 * tables' number and sub-line formatting identical.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';
import { ReportDateRange, resolveRange } from './ReportDateRange';
import type { RangeKey } from './ReportDateRange';
import { formatNumber, formatRelativeTimestamp } from '../utils/formatters';
import './FeatureUsageReport.css';

export function useRangedAdminReport<T>(
  fetcher: (start?: string, end?: string) => Promise<T>,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [rangeKey, setRangeKey] = useState<RangeKey>('last30');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  const mountedRef = useRef(true);
  // Monotonic fetch counter: a stale response (slow query for a wide range)
  // must never overwrite the result of a newer range selection.
  const fetchSeqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++fetchSeqRef.current;
    setIsRefreshing(true);
    try {
      const { start, end } = resolveRange(rangeKey, customStart, customEnd);
      const resp = await fetcher(start, end);
      if (!mountedRef.current || seq !== fetchSeqRef.current) return;
      setData(resp);
      setError(null);
    } catch (err) {
      if (!mountedRef.current || seq !== fetchSeqRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current && seq === fetchSeqRef.current) {
        setIsRefreshing(false);
      }
    }
  }, [fetcher, rangeKey, customStart, customEnd]);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  return {
    data,
    error,
    isRefreshing,
    load,
    range: {
      rangeKey,
      customStart,
      customEnd,
      onRangeKeyChange: setRangeKey,
      onCustomStartChange: setCustomStart,
      onCustomEndChange: setCustomEnd,
    },
  };
}

interface FeatureUsageReportFrameProps {
  title: string;
  // Header summary next to the title (null while loading).
  summary: ReactNode;
  summaryTitle?: string;
  range: Parameters<typeof ReportDateRange>[0];
  isRefreshing: boolean;
  onRefresh: () => void;
  loading: boolean;
  // Message shown instead of the table when there are no rows.
  emptyMessage: string | null;
  error: string | null;
  children: ReactNode;
}

export function FeatureUsageReportFrame({
  title,
  summary,
  summaryTitle,
  range,
  isRefreshing,
  onRefresh,
  loading,
  emptyMessage,
  error,
  children,
}: FeatureUsageReportFrameProps) {
  return (
    <div className="feature-report">
      <div className="feature-report-header">
        <h3>{title}</h3>
        <div className="header-actions">
          {summary && (
            <span className="feature-report-summary" title={summaryTitle}>
              {summary}
            </span>
          )}
          <ReportDateRange {...range} />
          <button
            type="button"
            className="refresh-button"
            title="Refresh now"
            aria-label="Refresh now"
            disabled={isRefreshing}
            onClick={onRefresh}
          >
            <RefreshCw size={12} className={isRefreshing ? 'spinning' : undefined} />
          </button>
        </div>
      </div>
      {loading ? (
        <div className="feature-report-loading">Loading...</div>
      ) : emptyMessage ? (
        <div className="feature-report-empty">{emptyMessage}</div>
      ) : (
        children
      )}
      {error && <div className="feature-report-error">error: {error}</div>}
    </div>
  );
}

/** Owner identity cell shared by the three tables (name over email). */
export function OwnerCell({ name, email }: { name: string; email: string }) {
  return (
    <td className="col-owner" title={name ? `${name} <${email}>` : email}>
      {name ? (
        <>
          <span className="owner-cell-name">{name}</span>
          <span className="owner-cell-email">{email}</span>
        </>
      ) : (
        <span className="owner-cell-name">{email}</span>
      )}
    </td>
  );
}

/** A count: an em dash for zero, a localized integer otherwise. */
export function Count({ value, title }: { value: number; title?: string }) {
  if (value === 0) {
    return (
      <span className="cell-empty" title={title}>
        &mdash;
      </span>
    );
  }
  return <span title={title}>{formatNumber(value)}</span>;
}

/** A "· "-joined sub-line under a cell's main figure (hidden when empty). */
export function SubLine({ parts }: { parts: (string | null)[] }) {
  const shown = parts.filter((p): p is string => Boolean(p));
  if (shown.length === 0) return null;
  return <span className="cell-sub">{shown.join(' · ')}</span>;
}

/** Relative timestamp with the raw ISO value in the tooltip. */
export function WhenCell({ iso }: { iso: string | null }) {
  return (
    <td className="col-when" title={iso ?? undefined}>
      {iso ? formatRelativeTimestamp(iso) : <span className="cell-empty">&mdash;</span>}
    </td>
  );
}

export function plural(n: number, noun: string, pluralNoun?: string): string {
  return `${formatNumber(n)} ${n === 1 ? noun : (pluralNoun ?? `${noun}s`)}`;
}
