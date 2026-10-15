/**
 * Small pill naming a doc's mode: "Private" (lock, neutral) or "Public"
 * (globe, the amber public-project tone). Shared by the sidebar doc rows,
 * the All Docs list, the doc viewer header and the write_doc card, and by
 * the Quest Tables views with `noun="table"`.
 */

import { Globe, Lock, type LucideIcon } from 'lucide-react';
import type { DocMode } from '../../api/types';
import './DocModeBadge.css';

const MODE_INFO: Record<DocMode, { label: string; title: (noun: string) => string; Icon: LucideIcon }> = {
  private: {
    label: 'Private',
    title: (noun) => `Private ${noun} -- only private conversations can read or change it`,
    Icon: Lock,
  },
  public: {
    label: 'Public',
    title: (noun) =>
      `Public ${noun} -- public conversations can read and change it; private conversations can only read it`,
    Icon: Globe,
  },
};

export interface DocModeBadgeProps {
  mode: DocMode;
  size?: 'sm' | 'md';
  /** What the hover text calls the item ("doc" by default). */
  noun?: 'doc' | 'table';
}

export function DocModeBadge({ mode, size = 'md', noun = 'doc' }: DocModeBadgeProps) {
  const { label, title, Icon } = MODE_INFO[mode] ?? MODE_INFO.private;
  return (
    <span
      className={`doc-mode-badge doc-mode-badge--${mode === 'public' ? 'public' : 'private'} doc-mode-badge--${size}`}
      title={title(noun)}
    >
      <Icon size={size === 'sm' ? 10 : 12} strokeWidth={2.25} aria-hidden="true" />
      <span className="doc-mode-badge-label">{label}</span>
    </span>
  );
}
