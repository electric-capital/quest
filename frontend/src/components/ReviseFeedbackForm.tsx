import { useEffect, useRef, useState } from 'react';
import { AnimatedWidthButton } from './AnimatedWidthButton';
import './ReviseFeedbackForm.css';

interface ReviseFeedbackFormProps {
  onSubmit: (feedback: string) => void;
  onCancel: () => void;
  isProcessing: boolean;
  /**
   * Optional placeholder override. Defaults to a friendly hint that
   * makes it clear feedback is sent to the agent.
   */
  placeholder?: string;
}

/**
 * Inline revise-with-feedback form rendered in place of the
 * Approve/Revise/Stop button row on an open action-request card. The
 * submit button is disabled when the textarea is empty -- a revise
 * with no feedback is equivalent to a plain deny, so we require text
 * here (users who just want out have the Stop button). Enter submits, Shift+Enter
 * inserts a newline, Esc cancels.
 *
 * Owns no business logic -- it just collects text and dispatches.
 */
export function ReviseFeedbackForm({
  onSubmit,
  onCancel,
  isProcessing,
  placeholder,
}: ReviseFeedbackFormProps) {
  const [feedback, setFeedback] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Auto-focus the textarea on mount so users can start typing right
  // after clicking Revise.
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  const trimmed = feedback.trim();
  const canSubmit = !isProcessing && trimmed.length > 0;

  const handleSubmit = () => {
    if (!canSubmit) return;
    onSubmit(feedback);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      if (!isProcessing) onCancel();
    }
  };

  return (
    <div className="revise-feedback-form">
      <textarea
        ref={textareaRef}
        className="revise-feedback-textarea"
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder ?? 'What should the agent change? (sent back to the agent)'}
        rows={2}
        disabled={isProcessing}
      />
      <div className="revise-feedback-buttons">
        <AnimatedWidthButton
          type="button"
          className="revise-feedback-btn revise-feedback-send"
          onClick={handleSubmit}
          disabled={!canSubmit}
        >
          {isProcessing ? 'Processing...' : 'Send revision'}
        </AnimatedWidthButton>
        <button
          type="button"
          className="revise-feedback-btn revise-feedback-cancel"
          onClick={onCancel}
          disabled={isProcessing}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
