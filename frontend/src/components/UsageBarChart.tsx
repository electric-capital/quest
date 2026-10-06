/**
 * UsageBarChart - one metric of the Total Usage report over time.
 *
 * A dependency-free SVG bar chart: one bar per series point, three
 * gridlines on a "nice" axis ceiling, sparse x-axis ticks, and a hover
 * readout line in the header (the hovered point, else the latest) instead
 * of a positioned tooltip. The in-progress last period and points whose
 * cost is partial (an unpriced model in the period) are drawn differently
 * and called out in the readout.
 */

import { useState } from 'react';
import type { AdminUsageBucket, AdminUsageGranularity } from '../api/types';
import {
  formatPeriodLabel,
  formatPeriodTick,
  niceCeil,
  niceCeilEven,
  selectTickIndices,
} from '../utils/usageReport';

interface UsageBarChartProps {
  title: string;
  points: AdminUsageBucket[];
  granularity: AdminUsageGranularity;
  /** The report's `today`: the period containing it is in progress. */
  today: string;
  valueOf: (point: AdminUsageBucket) => number;
  /** Whole-number metric: even axis ceilings, no fractional gridline labels. */
  integer?: boolean;
  /** True when the point's figure is known to be incomplete (unpriced calls). */
  partialOf?: (point: AdminUsageBucket) => boolean;
  /** Readout format (full precision). */
  formatValue: (value: number) => string;
  /** Axis-label format (compact). */
  formatAxis: (value: number) => string;
}

// viewBox geometry; the SVG scales to its container width.
const WIDTH = 600;
const HEIGHT = 170;
const PAD_LEFT = 44;
const PAD_RIGHT = 8;
const PAD_TOP = 8;
const PAD_BOTTOM = 20;
const PLOT_WIDTH = WIDTH - PAD_LEFT - PAD_RIGHT;
const PLOT_HEIGHT = HEIGHT - PAD_TOP - PAD_BOTTOM;
const MAX_TICKS = 6;

export function UsageBarChart({
  title,
  points,
  granularity,
  today,
  valueOf,
  integer = false,
  partialOf,
  formatValue,
  formatAxis,
}: UsageBarChartProps) {
  const [hovered, setHovered] = useState<number | null>(null);

  const values = points.map(valueOf);
  const peak = Math.max(0, ...values);
  const ceiling = integer ? niceCeilEven(peak) : niceCeil(peak);
  const count = points.length;
  const slot = count > 0 ? PLOT_WIDTH / count : PLOT_WIDTH;
  // Thin gap between bars, but never so thin that daily bars vanish.
  const gap = Math.min(2, slot * 0.2);
  const barWidth = Math.max(1, slot - gap);
  const ticks = new Set(selectTickIndices(count, MAX_TICKS));

  const readoutIndex = hovered ?? (count > 0 ? count - 1 : null);
  const readoutPoint = readoutIndex === null ? null : points[readoutIndex];
  const readoutNotes: string[] = [];
  if (readoutPoint) {
    if (readoutPoint.end >= today) readoutNotes.push('in progress');
    if (partialOf?.(readoutPoint)) readoutNotes.push('excludes unpriced calls');
  }

  return (
    <div className="usage-chart">
      <div className="usage-chart-header">
        <span className="usage-chart-title">{title}</span>
        {readoutPoint && readoutIndex !== null && (
          <span className="usage-chart-readout">
            <span className="usage-chart-readout-period">
              {formatPeriodLabel(readoutPoint, granularity)}
            </span>
            {': '}
            <span className="usage-chart-readout-value">{formatValue(values[readoutIndex])}</span>
            {readoutNotes.length > 0 && (
              <span className="usage-chart-readout-note"> · {readoutNotes.join(' · ')}</span>
            )}
          </span>
        )}
      </div>
      <svg
        className="usage-chart-svg"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${title} per ${granularity === 'daily' ? 'day' : granularity === 'weekly' ? 'week' : 'month'}`}
        onMouseLeave={() => setHovered(null)}
      >
        {[0, 0.5, 1].map((fraction) => {
          const y = PAD_TOP + PLOT_HEIGHT * (1 - fraction);
          const gridValue = ceiling * fraction;
          const labelled = !integer || Number.isInteger(gridValue);
          return (
            <g key={fraction}>
              <line
                className="usage-chart-grid"
                x1={PAD_LEFT}
                x2={WIDTH - PAD_RIGHT}
                y1={y}
                y2={y}
              />
              {labelled && (
                <text className="usage-chart-axis-label" x={PAD_LEFT - 6} y={y + 3} textAnchor="end">
                  {formatAxis(gridValue)}
                </text>
              )}
            </g>
          );
        })}
        {points.map((point, i) => {
          const value = values[i];
          const height = ceiling > 0 ? (value / ceiling) * PLOT_HEIGHT : 0;
          const x = PAD_LEFT + i * slot + gap / 2;
          const y = PAD_TOP + PLOT_HEIGHT - height;
          const classes = ['usage-chart-bar'];
          if (point.end >= today) classes.push('usage-chart-bar-current');
          if (partialOf?.(point)) classes.push('usage-chart-bar-partial');
          if (hovered === i) classes.push('usage-chart-bar-hover');
          return (
            <g key={point.start} onMouseEnter={() => setHovered(i)}>
              {/* Full-height transparent hit area so empty periods hover too. */}
              <rect
                className="usage-chart-hit"
                x={PAD_LEFT + i * slot}
                y={PAD_TOP}
                width={slot}
                height={PLOT_HEIGHT}
              />
              <rect
                className={classes.join(' ')}
                x={x}
                y={y}
                width={barWidth}
                // Keep a hairline for zero so the period is visibly present.
                height={Math.max(height, value > 0 ? 1 : 0.5)}
              >
                <title>{`${formatPeriodLabel(point, granularity)}: ${formatValue(value)}`}</title>
              </rect>
              {ticks.has(i) && (
                <text
                  className="usage-chart-axis-label"
                  x={PAD_LEFT + i * slot + slot / 2}
                  y={HEIGHT - 6}
                  textAnchor={i === 0 ? 'start' : 'middle'}
                >
                  {formatPeriodTick(point, granularity)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
