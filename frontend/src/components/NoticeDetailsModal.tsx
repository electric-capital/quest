/**
 * Full-text readout behind a composer notice that carries a diagnostic
 * report (today: voice-input failures, see utils/voiceInputReport.ts).
 *
 * The point is a phone: there is no console, so the notice itself is a
 * button and this modal shows the report as selectable monospace text that
 * fits a screenshot, with a Copy button for pasting it into a debugging
 * session.
 */

import { useCallback, useEffect, useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { ModalShell } from './ModalShell';
import './NoticeDetailsModal.css';

interface NoticeDetailsModalProps {
  isOpen: boolean;
  title: string;
  report: string;
  onClose: () => void;
}

export function NoticeDetailsModal({ isOpen, title, report, onClose }: NoticeDetailsModalProps) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1800);
    return () => window.clearTimeout(t);
  }, [copied]);

  useEffect(() => {
    if (!isOpen) setCopied(false);
  }, [isOpen]);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(report);
      setCopied(true);
    } catch {
      // Clipboard API unavailable (insecure context, denied permission):
      // the text stays selectable for a manual copy.
    }
  }, [report]);

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      overlayClassName="notice-details-overlay"
      modalClassName="notice-details-modal"
      ariaLabelledBy="notice-details-title"
    >
      <div className="notice-details-header">
        <h2 id="notice-details-title">{title}</h2>
        <button type="button" className="notice-details-close" onClick={onClose} aria-label="Close">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <p className="notice-details-hint">
        Screenshot this, or copy it, and paste it into a debugging session.
      </p>
      <pre className="notice-details-report" tabIndex={0}>{report}</pre>
      <div className="notice-details-actions">
        <button type="button" className="notice-details-copy" onClick={handleCopy}>
          {copied ? <Check size={16} /> : <Copy size={16} />}
          {copied ? 'Copied' : 'Copy report'}
        </button>
      </div>
    </ModalShell>
  );
}
