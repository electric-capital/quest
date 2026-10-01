/**
 * The Sidebar's inline SVG glyphs, one component each so the section
 * components stay readable. Markup is kept byte-for-byte what the Sidebar
 * rendered before the split (the stylesheet targets the class names).
 */

import type React from 'react';

interface IconProps {
  className?: string;
  size?: number;
  style?: React.CSSProperties;
}

const STROKE = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: '2',
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const;

export function PlusIcon({ size = 14 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE}>
      <line x1="12" y1="5" x2="12" y2="19"></line>
      <line x1="5" y1="12" x2="19" y2="12"></line>
    </svg>
  );
}

export function FolderIcon({ className, size = 16 }: IconProps) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" {...STROKE}>
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
    </svg>
  );
}

export function FolderPlusIcon({ size = 14 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE}>
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
      <line x1="12" y1="11" x2="12" y2="17"></line>
      <line x1="9" y1="14" x2="15" y2="14"></line>
    </svg>
  );
}

/** Globe: public project (internet sandbox, no internal data access). */
export function GlobeIcon({ className, size = 12, title }: IconProps & { title?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" {...STROKE}>
      {title && <title>{title}</title>}
      <circle cx="12" cy="12" r="10"></circle>
      <line x1="2" y1="12" x2="22" y2="12"></line>
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
    </svg>
  );
}

export function ChevronRightIcon({ className, size = 14 }: IconProps) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" {...STROKE}>
      <polyline points="9 18 15 12 9 6"></polyline>
    </svg>
  );
}

export function ChevronLeftIcon({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE}>
      <polyline points="15 18 9 12 15 6"></polyline>
    </svg>
  );
}

/** Expand/collapse chevron for a routine-run group (rotates when expanded). */
export function RoutineGroupChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg className="routine-group-chevron" width="12" height="12" viewBox="0 0 24 24"
         fill="none" stroke="currentColor" strokeWidth="2"
         style={{ transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)' }}>
      <polyline points="9 18 15 12 9 6"></polyline>
    </svg>
  );
}

/** Two curved arrows: a routine-run group. */
export function RoutineGroupIcon() {
  return (
    <svg className="routine-group-icon" width="14" height="14" viewBox="0 0 24 24" {...STROKE}>
      <polyline points="23 4 23 10 17 10"></polyline>
      <polyline points="1 20 1 14 7 14"></polyline>
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
    </svg>
  );
}

export function GearIcon({ size = 14 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE}>
      <circle cx="12" cy="12" r="3"></circle>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
    </svg>
  );
}

/** Clock: the routine has an enabled schedule. */
export function ClockIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="10"></circle>
      <polyline points="12 6 12 12 16 14"></polyline>
    </svg>
  );
}

export function PlayIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none">
      <polygon points="5,3 19,12 5,21"></polygon>
    </svg>
  );
}

/** Vertical three dots: a conversation row's "more options" button. */
export function KebabIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <circle cx="12" cy="5" r="2"></circle>
      <circle cx="12" cy="12" r="2"></circle>
      <circle cx="12" cy="19" r="2"></circle>
    </svg>
  );
}

/** Small monochrome Slack-style hash mark indicating a Slack-driven conversation. */
export function SlackConversationIcon() {
  return (
    <span className="conversation-slack-icon" title="Started from Slack DM">
      <svg width="12" height="12" viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
        <line x1="4" y1="9" x2="20" y2="9"></line>
        <line x1="4" y1="15" x2="20" y2="15"></line>
        <line x1="10" y1="3" x2="8" y2="21"></line>
        <line x1="16" y1="3" x2="14" y2="21"></line>
      </svg>
    </span>
  );
}

/** Small monochrome robot glyph indicating a cross-user subagent conversation. */
export function SubagentConversationIcon() {
  return (
    <span className="conversation-subagent-icon" title="Subagent run by another user">
      <svg width="12" height="12" viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
        <path d="M12 8V4H8"></path>
        <rect x="4" y="8" width="16" height="12" rx="2"></rect>
        <path d="M2 14h2"></path>
        <path d="M20 14h2"></path>
        <path d="M15 13v2"></path>
        <path d="M9 13v2"></path>
      </svg>
    </span>
  );
}

/** Small monochrome zap glyph indicating a one-shot inference API run. */
export function InferenceConversationIcon() {
  return (
    <span className="conversation-inference-icon" title="One-shot run via the Inference API">
      <svg width="12" height="12" viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
      </svg>
    </span>
  );
}

/** The origin badge shown in front of a conversation title, if any. */
export function ConversationOriginIcon({ origin }: { origin: string | null | undefined }) {
  if (origin === 'slack') return <SlackConversationIcon />;
  if (origin === 'user_subagent') return <SubagentConversationIcon />;
  if (origin === 'inference_api') return <InferenceConversationIcon />;
  return null;
}
