import React, { useState, useEffect } from 'react';
import { resolveActionRequest } from '../api/client';
import { endpoints } from '../api/config';
import type { ActionRequestMessage as ActionRequestMessageType, PreviewField } from '../api/types';
import { emitRequestCountChange } from '../services/requestEvents';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { ReviseFeedbackForm } from './ReviseFeedbackForm';
import { ActionRequestPreviewFields } from './ActionRequestPreviewFields';
import { AnimatedWidthButton } from './AnimatedWidthButton';
import './ActionRequestMessage.css';

interface ActionRequestMessageProps {
  message: ActionRequestMessageType;
  conversationId: string;
}

type PendingAction = 'approve' | 'revise' | 'stop' | null;

/**
 * Wrapped in React.memo to prevent re-renders when parent re-renders
 * with the same props. Action request messages are static once created,
 * so memoization prevents unnecessary re-renders during streaming.
 */
export const ActionRequestMessage = React.memo(function ActionRequestMessage({ message, conversationId }: ActionRequestMessageProps) {
  const [status, setStatus] = useState<'open' | 'denied' | 'executed' | 'stopped'>(message.status);
  // Which button's request is in flight, so only THAT button relabels
  // (Stop -> "Stopping...") instead of the Approve button showing
  // "Processing..." for every action.
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const isProcessing = pendingAction !== null;
  const [error, setError] = useState<string | null>(null);
  const [previewFields, setPreviewFields] = useState<PreviewField[] | undefined>(message.preview_fields);
  const [approveLabel, setApproveLabel] = useState<string>(message.approve_label || 'Approve');
  // Server-derived collapsed-card fields (see the handler's resolved_label /
  // summary_snippet); refreshed from the API on mount for old messages that
  // predate them.
  const [resolvedLabel, setResolvedLabel] = useState<string | undefined>(message.resolved_label);
  const [summarySnippet, setSummarySnippet] = useState<string | undefined>(message.summary_snippet);
  // True while the inline revise-with-feedback textarea is showing in
  // place of the Approve/Revise/Stop button row.
  const [reviseMode, setReviseMode] = useState(false);
  // Persisted deny feedback (shown under the "Denied" status when the
  // card is collapsed). Populated from the message itself if present
  // and refreshed from the API on mount when the status flips.
  const initialFeedback = (message.feedback as string | undefined)
    ?? (message.result && typeof message.result === 'object'
      ? (message.result as Record<string, unknown>).feedback as string | undefined
      : undefined)
    ?? null;
  const [feedback, setFeedback] = useState<string | null>(initialFeedback ?? null);
  // Resolved cards collapse to a one-line summary; this re-expands them so
  // the reasoning and preview fields stay accessible after a decision.
  const [expanded, setExpanded] = useState(false);

  // On mount, if the stored status is 'open', check the database for the current status.
  // Also fetch preview_fields from the server for old messages that lack them.
  useEffect(() => {
    if (message.request_id) {
      const needsStatusCheck = message.status === 'open';
      const needsPreview = !message.preview_fields;
      const needsResolvedMeta = message.resolved_label === undefined;
      if (needsStatusCheck || needsPreview || needsResolvedMeta) {
        fetch(endpoints.actionRequest(message.request_id), { credentials: 'include' })
          .then((res) => {
            if (res.ok) return res.json();
            return null;
          })
          .then((data) => {
            if (data) {
              if (data.status !== 'open') {
                setStatus(data.status);
              }
              if (data.preview_fields) {
                setPreviewFields(data.preview_fields);
              }
              if (data.approve_label) {
                setApproveLabel(data.approve_label);
              }
              if (data.resolved_label) {
                setResolvedLabel(data.resolved_label);
              }
              if (typeof data.summary_snippet === 'string') {
                setSummarySnippet(data.summary_snippet);
              }
              // Surface persisted deny feedback (lives in result.feedback)
              // so a reload still shows the user's reason.
              const result = data.result as Record<string, unknown> | null | undefined;
              const fb = result && typeof result === 'object'
                ? (result.feedback as string | undefined)
                : undefined;
              if (fb) setFeedback(fb);
            }
          })
          .catch(() => {
            // Ignore fetch errors on mount check
          });
      }
    }
  }, [message.request_id, message.status, message.preview_fields, message.resolved_label]);

  // Persistent WS: live-flip the card when this action request is resolved
  // from anywhere (another tab, REST call, headless resume). The REST
  // mount-time refresh above stays as a fallback for sockets that have not
  // yet connected on mount.
  useEffect(() => {
    if (!message.request_id) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'wait_handle_resolved') return;
      const requestId = event.request_id as number | undefined;
      if (requestId !== message.request_id) return;
      const eventStatus = event.status as string | undefined;
      // Map wait-handle status to action-request status. Accepted means the
      // request was executed; rejected means denied; stopped means the
      // user halted the loop (the model hears about it with their next
      // message); cancelled / timed_out we treat as still resolved (the
      // model loop will close the card on its next turn but visually we
      // collapse it).
      if (eventStatus === 'accepted') {
        setStatus('executed');
      } else if (eventStatus === 'stopped') {
        setStatus('stopped');
      } else if (
        eventStatus === 'rejected'
        || eventStatus === 'cancelled'
        || eventStatus === 'timed_out'
      ) {
        setStatus('denied');
        const response = event.response as Record<string, unknown> | undefined;
        const fb = response && typeof response.feedback === 'string'
          ? response.feedback
          : undefined;
        if (fb) setFeedback(fb);
      }
    });
  }, [message.request_id]);

  const handleApprove = async () => {
    setPendingAction('approve');
    setError(null);
    try {
      const result = await resolveActionRequest(message.request_id, 'execute');
      setStatus(result.status as 'executed');
      emitRequestCountChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to execute request');
    } finally {
      setPendingAction(null);
    }
  };

  // subagent_return cards live in a read-only subagent conversation the
  // target user cannot type into, so "stop and wait for the next message"
  // has no meaning there: they keep the hard Deny that ends the run.
  // Every other card gets Stop, which discards the request AND halts the
  // conversation loop; the composer unlocks and the model only learns of
  // it (verdict "stopped") together with the user's next message.
  const isSubagentReturn = message.request_type === 'subagent_return';

  const handleStopOrDeny = async () => {
    setPendingAction('stop');
    setError(null);
    try {
      const result = await resolveActionRequest(
        message.request_id,
        isSubagentReturn ? 'deny' : 'stop',
      );
      setStatus(result.status as 'denied' | 'stopped');
      emitRequestCountChange();
    } catch (e) {
      setError(e instanceof Error
        ? e.message
        : (isSubagentReturn ? 'Failed to deny request' : 'Failed to stop request'));
    } finally {
      setPendingAction(null);
    }
  };

  const enterReviseMode = () => {
    setError(null);
    setReviseMode(true);
  };

  const cancelReviseMode = () => {
    setReviseMode(false);
  };

  const submitRevise = async (rawFeedback: string) => {
    const trimmed = rawFeedback.trim();
    if (!trimmed) return;
    setPendingAction('revise');
    setError(null);
    try {
      const result = await resolveActionRequest(
        message.request_id,
        'deny',
        trimmed,
      );
      setStatus(result.status as 'denied');
      // Reflect the feedback locally so the collapsed card shows it
      // immediately without waiting for a refetch.
      setFeedback(trimmed);
      setReviseMode(false);
      emitRequestCountChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to send revision');
    } finally {
      setPendingAction(null);
    }
  };

  const renderPreview = () => (
    <ActionRequestPreviewFields
      previewFields={previewFields}
      params={message.params}
      conversationId={conversationId}
      classPrefix="action-request-preview"
    />
  );

  // Collapsed resolved state
  if (status !== 'open') {
    // Both fields are computed server-side by the request type's handler
    // (chat/action_request_types); generic fallbacks cover requests whose
    // handler no longer exists.
    const statusLabel = status === 'executed'
      ? (resolvedLabel || 'Sent')
      : status === 'stopped' ? 'Stopped' : 'Denied';
    const snippet = summarySnippet || '';

    return (
      <div className={`action-request-message collapsed ${status}${expanded ? ' expanded' : ''}`}>
        <div className="action-request-icon">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M22 2L11 13" />
            <path d="M22 2L15 22L11 13L2 9L22 2Z" />
          </svg>
        </div>
        <div className="action-request-collapsed-body">
          <div className="action-request-collapsed-row">
            <div className="action-request-outcome-text">
              <span className="action-request-outcome-status">{statusLabel}:</span>{' '}
              <span className="action-request-id">#{message.request_id}</span>{' '}
              <span className="action-request-outcome-content">{message.display_name}{snippet ? ` -- ${snippet}` : ''}</span>
              {status === 'denied' && feedback && (
                <div className="action-request-feedback">
                  <span className="action-request-feedback-label">Feedback:</span>
                  {feedback}
                </div>
              )}
            </div>
            <button
              className="action-request-expand-toggle"
              onClick={() => setExpanded((prev) => !prev)}
              aria-expanded={expanded}
              title={expanded ? 'Hide details' : 'Show details'}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M6 9L12 15L18 9" />
              </svg>
            </button>
          </div>
          {expanded && (
            <div className="action-request-expanded-details">
              {message.reasoning && (
                <div className="action-request-reasoning">{message.reasoning}</div>
              )}
              {renderPreview()}
            </div>
          )}
        </div>
      </div>
    );
  }

  // Open state
  return (
    <div className="action-request-message pending">
      <div className="action-request-icon">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M22 2L11 13" />
          <path d="M22 2L15 22L11 13L2 9L22 2Z" />
        </svg>
      </div>
      <div className="action-request-body">
        <div className="action-request-header">
          <span className="action-request-display-name">{message.display_name}</span>
          <span className="action-request-id">#{message.request_id}</span>
        </div>

        {message.reasoning && (
          <div className="action-request-reasoning">{message.reasoning}</div>
        )}

        {renderPreview()}

        {error && (
          <div className="action-request-error">{error}</div>
        )}

        {reviseMode ? (
          <ReviseFeedbackForm
            onSubmit={submitRevise}
            onCancel={cancelReviseMode}
            isProcessing={isProcessing}
          />
        ) : (
          <div className="action-request-buttons">
            <AnimatedWidthButton
              className="action-request-btn approve"
              onClick={handleApprove}
              disabled={isProcessing}
            >
              {pendingAction === 'approve' ? 'Processing...' : approveLabel}
            </AnimatedWidthButton>
            <button
              className="action-request-btn revise"
              onClick={enterReviseMode}
              disabled={isProcessing}
            >
              Revise
            </button>
            <AnimatedWidthButton
              className={`action-request-btn ${isSubagentReturn ? 'deny' : 'stop'}`}
              onClick={handleStopOrDeny}
              disabled={isProcessing}
              title={isSubagentReturn
                ? 'Deny the return and end the subagent run'
                : 'Discard this request and stop the conversation; the AI resumes with your next message'}
            >
              {pendingAction === 'stop'
                ? (isSubagentReturn ? 'Denying...' : 'Stopping...')
                : (isSubagentReturn ? 'Deny' : 'Stop')}
            </AnimatedWidthButton>
          </div>
        )}
      </div>
    </div>
  );
});
