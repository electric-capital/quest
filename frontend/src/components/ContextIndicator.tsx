/**
 * Context usage indicator component.
 *
 * Renders a small circular meter in the composer controls row that fills
 * clockwise as the model's context window is consumed (the same idiom as
 * Claude Code's context ring). The ring has a FIXED footprint whatever the
 * percentage, so it never changes the row's layout -- a variable-width
 * "NN% context" label used to push the send button out of a narrow phone
 * row. The exact figures ("14% · 28K / 200K max") live in the hover tooltip,
 * and clicking the ring opens the system prompt view when a handler is given.
 */

import React from 'react';
import { getModelInfo } from '../constants/models';
import './ContextIndicator.css';

interface ContextIndicatorProps {
  contextTokens: number | null;
  maxContextTokens: number | null;
  modelId: string;
  onInfoClick?: () => void;
}

/** Ring geometry (viewBox units; the SVG is scaled by CSS). */
const RING_SIZE = 20;
const RING_STROKE = 2.5;
const RING_RADIUS = (RING_SIZE - RING_STROKE) / 2;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

/**
 * Format a token count into a human-readable string.
 * - Under 1000: show raw number (e.g., "950")
 * - 1000-999999: show with K suffix (e.g., "70K", "1.5K")
 * - 1000000+: show with M suffix (e.g., "1M", "1.5M")
 */
function formatTokenCount(count: number): string {
  if (count < 1000) {
    return String(count);
  }
  if (count < 1_000_000) {
    const k = count / 1000;
    // Use integer if it's a round number, otherwise one decimal
    return k === Math.floor(k) ? `${k}K` : `${k.toFixed(1).replace(/\.0$/, '')}K`;
  }
  const m = count / 1_000_000;
  return m === Math.floor(m) ? `${m}M` : `${m.toFixed(1).replace(/\.0$/, '')}M`;
}

export const ContextIndicator = React.memo(function ContextIndicator({
  contextTokens,
  maxContextTokens,
  modelId,
  onInfoClick,
}: ContextIndicatorProps) {
  // Don't render if no context data yet
  if (contextTokens == null) {
    return null;
  }

  // Determine max context: prefer stats-reported value, fall back to model constant
  let effectiveMax = maxContextTokens;
  if (effectiveMax == null || effectiveMax === 0) {
    effectiveMax = getModelInfo(modelId)?.maxInputTokens ?? null;
  }

  // If we still don't have a max, don't render
  if (effectiveMax == null || effectiveMax === 0) {
    return null;
  }

  const percentage = Math.round((contextTokens / effectiveMax) * 100);
  // Clamp to 100% in case of slight overcount
  const displayPercentage = Math.max(0, Math.min(percentage, 100));

  // Color based on usage level
  let colorClass = 'context-normal';
  if (percentage >= 90) {
    colorClass = 'context-danger';
  } else if (percentage >= 70) {
    colorClass = 'context-warning';
  }

  const usageText = `${displayPercentage}% of context used`;
  const tooltipText = `${usageText} · ${formatTokenCount(contextTokens)} / ${formatTokenCount(effectiveMax)} max`;
  // Dash offset shrinks the gap as usage grows: 0% = empty ring, 100% = full.
  const dashOffset = RING_CIRCUMFERENCE * (1 - displayPercentage / 100);

  const ring = (
    <svg
      className="context-ring"
      viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`}
      aria-hidden="true"
      focusable="false"
    >
      <circle
        className="context-ring-track"
        cx={RING_SIZE / 2}
        cy={RING_SIZE / 2}
        r={RING_RADIUS}
        fill="none"
        strokeWidth={RING_STROKE}
      />
      <circle
        className="context-ring-fill"
        cx={RING_SIZE / 2}
        cy={RING_SIZE / 2}
        r={RING_RADIUS}
        fill="none"
        strokeWidth={RING_STROKE}
        strokeLinecap={displayPercentage > 0 && displayPercentage < 100 ? 'round' : 'butt'}
        strokeDasharray={RING_CIRCUMFERENCE}
        strokeDashoffset={dashOffset}
        // Start the arc at 12 o'clock and fill clockwise.
        transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
      />
    </svg>
  );

  return (
    <div className={`context-indicator-container ${colorClass}`}>
      {onInfoClick ? (
        <button
          type="button"
          className="context-indicator-button"
          onClick={onInfoClick}
          aria-label={`${usageText}. View system prompt`}
        >
          {ring}
        </button>
      ) : (
        <span className="context-indicator-button" role="img" aria-label={usageText}>
          {ring}
        </span>
      )}
      <span className="context-indicator-tooltip">
        {tooltipText}
        {onInfoClick && <span className="context-indicator-tooltip-hint">Click to view the system prompt</span>}
      </span>
    </div>
  );
});
