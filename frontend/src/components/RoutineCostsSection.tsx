/**
 * RoutineCostsSection - the "Costs" section of RoutineSettingsModal.
 *
 * Read-only view of one routine's inference spend, fetched on mount from
 * GET /projects/{id}/routines/{id}/costs: headline cards for the last 7 and
 * 28 days (each with a period-over-period delta), the lifetime total since
 * the routine was created, and a table of the most recent runs. Every
 * figure buckets runs by their start time, so the cards, the lifetime line
 * and the table always agree (see chat/routine_costs.py).
 */

import { useEffect, useRef, useState } from 'react';
import { fetchRoutineCosts } from '../api/client';
import type {
  RoutineCostBucket,
  RoutineCostReport,
  RoutineCostRun,
} from '../api/types';
import { describeCostSource, formatCost, formatUsd } from './ConversationUsageCell';
import { getModelDisplayName } from '../constants/models';
import { formatNumber, parseUTCTimestamp } from '../utils/formatters';
import './RoutineCostsSection.css';

interface RoutineCostsSectionProps {
  projectId: string;
  routineId: string;
  /** Open one of the listed runs (its conversation); undefined = rows inert. */
  onOpenRun?: (conversationId: string) => void;
  /**
   * Called once the fetch settles (report or error rendered) so the host
   * dialog can hold its size across the loading placeholder.
   */
  onLoaded?: () => void;
}

/** "Sep 20, 2:30 PM", with the year appended once it is not the current one. */
function formatRunStart(iso: string): string {
  const date = parseUTCTimestamp(iso);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

function formatDate(iso: string): string {
  return parseUTCTimestamp(iso).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function pluralRuns(n: number): string {
  return `${formatNumber(n)} ${n === 1 ? 'run' : 'runs'}`;
}

/**
 * Cost figure for a bucket: "~$1.23" / "$1.23" (provenance in the title),
 * or an em dash with an explanation when a run in the bucket used a model
 * without pricing and the total is therefore unknown.
 */
function CostFigure({ bucket, className }: { bucket: RoutineCostBucket; className?: string }) {
  if (bucket.cost_usd === null) {
    return (
      <span
        className={`${className ?? ''} routine-costs-unknown`}
        title="Includes a run on a model without a pricing entry, so the total is unknown"
      >
        &mdash;
      </span>
    );
  }
  return (
    <span className={className} title={`Cost ${describeCostSource(bucket.cost_source)}`}>
      {formatCost(bucket.cost_usd, bucket.cost_source)}
    </span>
  );
}

type Trend = 'up' | 'down' | 'flat' | 'none';

interface Delta {
  trend: Trend;
  text: string;
  title: string;
}

/**
 * Period-over-period wording for a window: the change in spend vs the
 * preceding period of the same length, as a signed dollar amount plus a
 * percentage when the base period had spend to compare against.
 */
export function describeDelta(current: RoutineCostBucket, previous: RoutineCostBucket, days: number): Delta {
  const base = `vs the ${days} days before (${pluralRuns(previous.run_count)})`;
  if (current.cost_usd === null || previous.cost_usd === null) {
    return { trend: 'none', text: 'n/a', title: `${base}: one of the periods includes an unpriced model` };
  }
  const diff = current.cost_usd - previous.cost_usd;
  if (Math.abs(diff) < 0.005) {
    return { trend: 'flat', text: 'no change', title: base };
  }
  const sign = diff > 0 ? '+' : '-';
  const amount = `${sign}${formatUsd(Math.abs(diff))}`;
  if (previous.cost_usd < 0.005) {
    // Nothing to divide by: the previous period had no (priced) spend.
    return { trend: 'up', text: `${amount} (no spend before)`, title: base };
  }
  const pct = Math.round((diff / previous.cost_usd) * 100);
  return {
    trend: diff > 0 ? 'up' : 'down',
    text: `${amount} (${sign}${Math.abs(pct)}%)`,
    title: `${base}: ${formatCost(previous.cost_usd, previous.cost_source)}`,
  };
}

function TrendGlyph({ trend }: { trend: Trend }) {
  if (trend === 'up') return <span aria-hidden="true">&#9650;</span>;
  if (trend === 'down') return <span aria-hidden="true">&#9660;</span>;
  return null;
}

function WindowCard({ days, current, previous }: { days: number; current: RoutineCostBucket; previous: RoutineCostBucket }) {
  const delta = describeDelta(current, previous, days);
  return (
    <div className="routine-costs-card">
      <div className="routine-costs-card-label">Last {days} days</div>
      <CostFigure bucket={current} className="routine-costs-card-value" />
      <div className="routine-costs-card-runs">{pluralRuns(current.run_count)}</div>
      <div className={`routine-costs-card-delta routine-costs-delta-${delta.trend}`} title={delta.title}>
        <TrendGlyph trend={delta.trend} /> {delta.text}
        <span className="routine-costs-card-delta-base"> vs prior {days} days</span>
      </div>
    </div>
  );
}

function RunRow({ run, onOpen }: { run: RoutineCostRun; onOpen?: (id: string) => void }) {
  const clickable = onOpen !== undefined;
  // Primary (heaviest) model by name, further models as a "+N" marker; the
  // full list lives in the cell tooltip. The column is narrow.
  const modelNames = run.models.map(getModelDisplayName);
  const extraModels = modelNames.length - 1;
  const cost = run.cost_usd === null ? null : formatCost(run.cost_usd, run.cost_source);
  return (
    <tr
      className={clickable ? 'routine-costs-run-clickable' : undefined}
      onClick={clickable ? () => onOpen(run.conversation_id) : undefined}
      onKeyDown={clickable ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(run.conversation_id); } } : undefined}
      tabIndex={clickable ? 0 : undefined}
      role={clickable ? 'link' : undefined}
      title={clickable ? `Open "${run.title}"` : undefined}
    >
      <td className="routine-costs-col-start">{formatRunStart(run.started_at)}</td>
      <td className="routine-costs-col-models" title={modelNames.join(', ')}>
        {modelNames[0] ?? '—'}
        {extraModels > 0 && <span className="routine-costs-more-models"> +{extraModels}</span>}
      </td>
      <td className="routine-costs-col-num">{formatNumber(run.total_tokens)}</td>
      <td className="routine-costs-col-num">
        {cost === null ? (
          <span className="routine-costs-unknown" title="Used a model without a pricing entry">&mdash;</span>
        ) : (
          <span title={`Cost ${describeCostSource(run.cost_source)} · ${formatNumber(run.call_count)} calls`}>{cost}</span>
        )}
      </td>
    </tr>
  );
}

export function RoutineCostsSection({ projectId, routineId, onOpenRun, onLoaded }: RoutineCostsSectionProps) {
  const [report, setReport] = useState<RoutineCostReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Latest onLoaded for the fetch callbacks (which outlive the render that
  // passed it).
  const onLoadedRef = useRef(onLoaded);
  useEffect(() => {
    onLoadedRef.current = onLoaded;
  }, [onLoaded]);

  // One fetch per mount: the modal keys this section on the routine id, so a
  // different routine means a fresh instance (no in-place state reset).
  useEffect(() => {
    let cancelled = false;
    fetchRoutineCosts(projectId, routineId)
      .then((data) => {
        if (cancelled) return;
        setReport(data);
        onLoadedRef.current?.();
      })
      .catch((err) => {
        console.error('Failed to load routine costs:', err);
        if (cancelled) return;
        setError('Failed to load costs. Please try again.');
        onLoadedRef.current?.();
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, routineId]);

  if (error) {
    return <div className="routine-settings-error">{error}</div>;
  }
  if (!report) {
    return <div className="routine-settings-loading">Loading costs...</div>;
  }

  const { lifetime } = report;
  const hasEstimate = [lifetime, ...report.windows.map((w) => w.current)].some(
    (b) => b.cost_usd !== null && b.cost_source !== 'reported' && b.run_count > 0,
  );

  return (
    <div className="routine-costs">
      <div className="routine-costs-cards">
        {report.windows.map((w) => (
          <WindowCard key={w.days} days={w.days} current={w.current} previous={w.previous} />
        ))}
      </div>

      <div className="routine-costs-lifetime">
        <span className="routine-costs-lifetime-label">
          Total{report.routine_created_at ? ` since ${formatDate(report.routine_created_at)}` : ''}
        </span>
        <CostFigure bucket={lifetime} className="routine-costs-lifetime-value" />
        <span className="routine-costs-lifetime-detail">
          {pluralRuns(lifetime.run_count)}
          {lifetime.run_count > 0 && (
            <> · {formatNumber(lifetime.call_count)} calls · {formatNumber(lifetime.total_tokens)} tokens</>
          )}
        </span>
      </div>

      <div className="routine-settings-field">
        <div className="routine-settings-label">Recent runs</div>
        {report.recent_runs.length === 0 ? (
          <div className="routine-costs-empty">This routine has not run yet.</div>
        ) : (
          <div className="routine-costs-table-wrap">
            <table className="routine-costs-table">
              <thead>
                <tr>
                  <th className="routine-costs-col-start">Started</th>
                  <th className="routine-costs-col-models">Model</th>
                  <th className="routine-costs-col-num">Tokens</th>
                  <th className="routine-costs-col-num">Cost</th>
                </tr>
              </thead>
              <tbody>
                {report.recent_runs.map((run) => (
                  <RunRow key={run.conversation_id} run={run} onOpen={onOpenRun} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="routine-costs-footnote">
        Each run's cost is the recorded usage of the conversation it created, counted
        on the day the run started.
        {hasEstimate && ' Figures marked ~ are list-price estimates.'}
        {' '}Runs whose conversation was deleted are not included.
      </p>
    </div>
  );
}
