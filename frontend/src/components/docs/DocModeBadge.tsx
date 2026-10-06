/**
 * Small pill naming a doc's mode: "Private" (lock, neutral) or "Public"
 * (globe, the amber public-project tone). Shared by the sidebar doc rows,
 * the All Docs list, the doc viewer header and the write_doc card.
 */

import { Globe, Lock, type LucideIcon } from 'lucide-react';
import type { DocMode } from '../../api/types';
import './DocModeBadge.css';

const MODE_INFO: Record<DocMode, { label: string; title: string; Icon: LucideIcon }> = {
  private: {
    label: 'Private',
    title: 'Private doc -- only private conversations can read or change it',
    Icon: Lock,
  },
  public: {
    label: 'Public',
    title:
      'Public doc -- public conversations can read and change it; private conversations can only read it',
    Icon: Globe,
  },
};

export interface DocModeBadgeProps {
  mode: DocMode;
  size?: 'sm' | 'md';
}

export function DocModeBadge({ mode, size = 'md' }: DocModeBadgeProps) {
  const { label, title, Icon } = MODE_INFO[mode] ?? MODE_INFO.private;
  return (
    <span
      className={`doc-mode-badge doc-mode-badge--${mode === 'public' ? 'public' : 'private'} doc-mode-badge--${size}`}
      title={title}
    >
      <Icon size={size === 'sm' ? 10 : 12} strokeWidth={2.25} aria-hidden="true" />
      <span className="doc-mode-badge-label">{label}</span>
    </span>
  );
}
