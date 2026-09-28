/**
 * RequestsView - Full-pane view for all action requests.
 * Replaces the chat panel + file browser when the user clicks "Requests" in the sidebar.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { fetchActionRequestsEnriched, fetchActionRequestCounts, resolveActionRequest } from '../api/client';
import type { EnrichedActionRequest, ActionRequestCountsResponse } from '../api/types';
import { useConversationContext } from '../contexts/ConversationContext';
import { onRequestCountChange } from '../services/requestEvents';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { ReviseFeedbackForm } from './ReviseFeedbackForm';
import { ActionRequestPreviewFields } from './ActionRequestPreviewFields';
import { AnimatedWidthButton } from './AnimatedWidthButton';
import './RequestsView.css';

type FilterStatus = 'all' | 'open' | 'executed' | 'denied' | 'stopped';
type PendingAction = 'approve' | 'revise' | 'stop';

/** Format a timestamp as a relative or absolute string. */
function formatTimestamp(isoTimestamp: string): string {
  const date = new Date(isoTimestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / (1000 * 60));
  const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;

  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

export function RequestsView() {
  const {
    setShowRequestsView,
  } = useConversationContext();

  const navigate = useNavigate();

  const [requests, setRequests] = useState<EnrichedActionRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<FilterStatus>('open');
  // Per-request in-flight action, so only the clicked button relabels
  // (Stop -> "Stopping...") while its siblings just disable.
  const [pendingActions, setPendingActions] = useState<Record<number, PendingAction>>({});
  const setPending = (requestId: number, action: PendingAction | null) => {
    setPendingActions((prev) => {
      const next = { ...prev };
      if (action) next[requestId] = action;
      else delete next[requestId];
      return next;
    });
  };
  const [errors, setErrors] = useState<Record<number, string>>({});
  // Per-request flag indicating the inline revise-feedback form is showing
  // in place of the Approve/Revise/Stop buttons.
  const [reviseModeIds, setReviseModeIds] = useState<Set<number>>(new Set());

  // Server-provided counts (null until first fetch)
  const [serverCounts, setServerCounts] = useState<ActionRequestCountsResponse['counts'] | null>(null);
  // Number of new requests since last full load
  const [newRequestCount, setNewRequestCount] = useState(0);
  // Loading state for the banner click
  const [bannerLoading, setBannerLoading] = useState(false);
  // Track the total count at last full load
  const lastLoadedTotalRef = useRef(0);
  // Deduplication guard for count fetches
  const inFlightCountRef = useRef(false);
  // Ref to the scrollable content area
  const contentRef = useRef<HTMLDivElement>(null);

  const loadRequests = useCallback(async (filterStatus: FilterStatus) => {
    try {
      const statusArg = filterStatus === 'all' ? undefined : filterStatus;
      const resp = await fetchActionRequestsEnriched(statusArg);
      setRequests(resp.action_requests);
      setNewRequestCount(0);
      // Also fetch fresh counts
      try {
        const countsResp = await fetchActionRequestCounts();
        setServerCounts(countsResp.counts);
        lastLoadedTotalRef.current = countsResp.counts.all;
      } catch {
        // Fall back to client-side counts
        lastLoadedTotalRef.current = resp.action_requests.length;
      }
    } catch (err) {
      console.error('Failed to load requests:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRequests(filter);
  }, [loadRequests, filter]);

  // Fetch counts from server (used by polling and event handler)
  const fetchCounts = useCallback(async () => {
    if (inFlightCountRef.current || document.hidden) return;
    inFlightCountRef.current = true;
    try {
      const resp = await fetchActionRequestCounts();
      setServerCounts(resp.counts);
      // Detect new requests
      const serverTotal = resp.counts.all;
      if (serverTotal > lastLoadedTotalRef.current) {
        setNewRequestCount(serverTotal - lastLoadedTotalRef.current);
      }
    } catch {
      // Silently ignore poll errors
    } finally {
      inFlightCountRef.current = false;
    }
  }, []);

  // Subscribe to requestEvents (same-tab immediate refresh after Approve/Deny)
  useEffect(() => {
    return onRequestCountChange(fetchCounts);
  }, [fetchCounts]);

  // Persistent WS: cross-tab + cross-device updates replace the prior 30s poll.
  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'request_count_changed') return;
      const counts = (event.counts as Record<string, number> | undefined) ?? null;
      if (counts) {
        setServerCounts({
          all: counts.all ?? 0,
          open: counts.open ?? 0,
          executed: counts.executed ?? 0,
          denied: counts.denied ?? 0,
          stopped: counts.stopped ?? 0,
        });
        if ((counts.all ?? 0) > lastLoadedTotalRef.current) {
          setNewRequestCount((counts.all ?? 0) - lastLoadedTotalRef.current);
        }
      } else {
        fetchCounts();
      }
    });
  }, [fetchCounts]);

  // Server returns the already-filtered slice, but after an optimistic
  // Approve/Revise/Stop mutates a row's status locally, that row may no longer
  // match the active tab filter. Re-apply the predicate client-side so the
  // resolved row disappears from Open immediately (no refetch needed). The
  // 'all' tab still shows every row.
  const filteredRequests =
    filter === 'all' ? requests : requests.filter((r) => r.status === filter);

  // Use server-provided counts when available, fall back to client-side
  const counts = serverCounts ?? {
    all: requests.length,
    open: requests.filter((r) => r.status === 'open').length,
    executed: requests.filter((r) => r.status === 'executed').length,
    denied: requests.filter((r) => r.status === 'denied').length,
    stopped: requests.filter((r) => r.status === 'stopped').length,
  };

  const handleApprove = async (requestId: number) => {
    setPending(requestId, 'approve');
    setErrors((prev) => {
      const next = { ...prev };
      delete next[requestId];
      return next;
    });
    try {
      const result = await resolveActionRequest(requestId, 'execute');
      // Optimistic update
      setRequests((prev) =>
        prev.map((r) =>
          r.id === requestId
            ? { ...r, status: result.status as 'executed', resolved_at: result.resolved_at }
            : r
        )
      );
      // Sidebar refresh happens automatically via onStreamComplete when the
      // resumed conversation stream finishes, so no manual trigger needed.
    } catch (e) {
      setErrors((prev) => ({
        ...prev,
        [requestId]: e instanceof Error ? e.message : 'Failed to execute request',
      }));
    } finally {
      setPending(requestId, null);
    }
  };

  // Stop discards the request AND halts its conversation loop (the model
  // resumes only with the user's next message there). subagent_return
  // cards keep the hard Deny that ends the subagent run -- the target user
  // cannot type into that read-only conversation, so Stop has no meaning.
  // A Stop on one card of a parallel batch stops its open siblings too, so
  // refetch the list afterwards instead of patching just the clicked row.
  const handleStopOrDeny = async (requestId: number, requestType: string) => {
    const isSubagentReturn = requestType === 'subagent_return';
    setPending(requestId, 'stop');
    setErrors((prev) => {
      const next = { ...prev };
      delete next[requestId];
      return next;
    });
    try {
      const result = await resolveActionRequest(
        requestId,
        isSubagentReturn ? 'deny' : 'stop',
      );
      setRequests((prev) =>
        prev.map((r) =>
          r.id === requestId
            ? { ...r, status: result.status as 'denied' | 'stopped', resolved_at: result.resolved_at }
            : r
        )
      );
      if (!isSubagentReturn) {
        void loadRequests(filter);
      }
    } catch (e) {
      setErrors((prev) => ({
        ...prev,
        [requestId]: e instanceof Error
          ? e.message
          : (isSubagentReturn ? 'Failed to deny request' : 'Failed to stop request'),
      }));
    } finally {
      setPending(requestId, null);
    }
  };

  const enterReviseMode = (requestId: number) => {
    setErrors((prev) => {
      const next = { ...prev };
      delete next[requestId];
      return next;
    });
    setReviseModeIds((prev) => {
      const next = new Set(prev);
      next.add(requestId);
      return next;
    });
  };

  const cancelReviseMode = (requestId: number) => {
    setReviseModeIds((prev) => {
      const next = new Set(prev);
      next.delete(requestId);
      return next;
    });
  };

  const submitRevise = async (requestId: number, rawFeedback: string) => {
    const trimmed = rawFeedback.trim();
    if (!trimmed) return;
    setPending(requestId, 'revise');
    setErrors((prev) => {
      const next = { ...prev };
      delete next[requestId];
      return next;
    });
    try {
      const result = await resolveActionRequest(requestId, 'deny', trimmed);
      // Optimistically reflect the new status AND the feedback in the
      // local result blob so the resolved-state card surfaces it without
      // needing a refetch.
      setRequests((prev) =>
        prev.map((r) => {
          if (r.id !== requestId) return r;
          const mergedResult: Record<string, unknown> = {
            ...(r.result ?? {}),
            denied: true,
            feedback: trimmed,
          };
          return {
            ...r,
            status: result.status as 'denied',
            resolved_at: result.resolved_at,
            result: mergedResult,
          };
        })
      );
      setReviseModeIds((prev) => {
        const next = new Set(prev);
        next.delete(requestId);
        return next;
      });
    } catch (e) {
      setErrors((prev) => ({
        ...prev,
        [requestId]: e instanceof Error ? e.message : 'Failed to send revision',
      }));
    } finally {
      setPending(requestId, null);
    }
  };

  const handleGoToConversation = (conversationId: string, projectId?: string | null) => {
    setShowRequestsView(false);
    if (projectId) {
      navigate(`/projects/${projectId}/${conversationId}`);
    } else {
      navigate(`/chats/${conversationId}`);
    }
  };

  const handleBannerClick = async () => {
    if (bannerLoading) return;
    setBannerLoading(true);
    try {
      const statusArg = filter === 'all' ? undefined : filter;
      const resp = await fetchActionRequestsEnriched(statusArg);
      setRequests(resp.action_requests);
      setNewRequestCount(0);
      // Refresh counts too
      try {
        const countsResp = await fetchActionRequestCounts();
        setServerCounts(countsResp.counts);
        lastLoadedTotalRef.current = countsResp.counts.all;
      } catch {
        // Fall back to client-side counts
        lastLoadedTotalRef.current = resp.action_requests.length;
      }
      // Scroll to top
      if (contentRef.current) {
        contentRef.current.scrollTop = 0;
      }
    } catch (err) {
      console.error('Failed to refresh requests:', err);
    } finally {
      setBannerLoading(false);
    }
  };

  const handleFilterChange = (key: FilterStatus) => {
    setFilter(key);
    // Refresh counts from server when tab changes; the loadRequests effect
    // re-runs on the new filter to fetch the matching slice.
    fetchCounts();
  };

  const renderPreview = (request: EnrichedActionRequest) => (
    <ActionRequestPreviewFields
      previewFields={request.preview_fields}
      params={request.params}
      conversationId={request.conversation_id}
      classPrefix="request-preview"
    />
  );

  const filterOptions: { key: FilterStatus; label: string }[] = [
    { key: 'open', label: 'Open' },
    { key: 'executed', label: 'Executed' },
    { key: 'denied', label: 'Denied' },
    { key: 'stopped', label: 'Stopped' },
    { key: 'all', label: 'All' },
  ];

  return (
    <div className="requests-view">
      <div className="requests-view-header">
        <h2>Requests</h2>
        <div className="requests-filter-pills">
          {filterOptions.map((opt) => (
            <button
              key={opt.key}
              className={`requests-filter-pill${filter === opt.key ? ' active' : ''}`}
              onClick={() => handleFilterChange(opt.key)}
            >
              {opt.label}
              <span className="pill-count">({counts[opt.key]})</span>
            </button>
          ))}
        </div>
      </div>

      <div className="requests-view-content" ref={contentRef}>
        <div className="requests-view-inner">
          {newRequestCount > 0 && !loading && (
            <button
              className="new-requests-banner"
              onClick={handleBannerClick}
              disabled={bannerLoading}
            >
              {bannerLoading
                ? 'Loading...'
                : `${newRequestCount} new request${newRequestCount === 1 ? '' : 's'}`}
            </button>
          )}
          {loading ? (
            <div className="requests-view-loading">Loading requests...</div>
          ) : (serverCounts?.all ?? requests.length) === 0 ? (
            <div className="requests-view-empty">
              No requests yet. Requests will appear here when the AI proposes actions that need your approval.
            </div>
          ) : filteredRequests.length === 0 ? (
            <div className="requests-view-empty">
              No {filter} requests.
            </div>
          ) : (
            filteredRequests.map((request) => (
              <div key={request.id} className={`request-card ${request.status}`}>
                {/* Header row */}
                <div className="request-card-header">
                  <span className="request-type-name">
                    {request.display_name || request.request_type.split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')}
                  </span>
                  <span className="request-id-badge">#{request.id}</span>
                  <span className={`request-status-pill ${request.status}`}>
                    {request.status}
                  </span>
                </div>

                {/* Context row */}
                {request.routine_name && (
                  <div className="request-context">
                    From routine: <span className="request-context-routine">{request.routine_name}</span>
                    {request.project_name && (
                      <span className="request-context-project"> in {request.project_name}</span>
                    )}
                  </div>
                )}

                {/* Conversation link */}
                <span
                  className="request-conversation-link"
                  onClick={() => handleGoToConversation(request.conversation_id, request.project_id)}
                >
                  Go to conversation
                </span>

                {/* Reasoning */}
                {request.reasoning && (
                  <div className="request-reasoning">{request.reasoning}</div>
                )}

                {/* Preview */}
                {renderPreview(request)}

                {/* Error */}
                {errors[request.id] && (
                  <div className="request-error">{errors[request.id]}</div>
                )}

                {/* Action buttons (only for open requests) */}
                {request.status === 'open' && (
                  reviseModeIds.has(request.id) ? (
                    <ReviseFeedbackForm
                      onSubmit={(fb) => submitRevise(request.id, fb)}
                      onCancel={() => cancelReviseMode(request.id)}
                      isProcessing={request.id in pendingActions}
                    />
                  ) : (
                    <div className="request-actions">
                      <AnimatedWidthButton
                        className="request-btn approve"
                        onClick={() => handleApprove(request.id)}
                        disabled={request.id in pendingActions}
                      >
                        {pendingActions[request.id] === 'approve' ? 'Processing...' : (request.approve_label || 'Approve')}
                      </AnimatedWidthButton>
                      <button
                        className="request-btn revise"
                        onClick={() => enterReviseMode(request.id)}
                        disabled={request.id in pendingActions}
                      >
                        Revise
                      </button>
                      <AnimatedWidthButton
                        className={`request-btn ${request.request_type === 'subagent_return' ? 'deny' : 'stop'}`}
                        onClick={() => handleStopOrDeny(request.id, request.request_type)}
                        disabled={request.id in pendingActions}
                        title={request.request_type === 'subagent_return'
                          ? 'Deny the return and end the subagent run'
                          : 'Discard this request and stop the conversation; the AI resumes with your next message'}
                      >
                        {pendingActions[request.id] === 'stop'
                          ? (request.request_type === 'subagent_return' ? 'Denying...' : 'Stopping...')
                          : (request.request_type === 'subagent_return' ? 'Deny' : 'Stop')}
                      </AnimatedWidthButton>
                    </div>
                  )
                )}

                {/* Persisted deny feedback on resolved cards */}
                {request.status === 'denied' && request.result && typeof request.result === 'object' && (request.result as Record<string, unknown>).feedback ? (
                  <div className="request-feedback">
                    <span className="request-feedback-label">Feedback:</span>
                    {(request.result as Record<string, unknown>).feedback as string}
                  </div>
                ) : null}

                {/* Timestamp */}
                <div className="request-timestamp">
                  {formatTimestamp(request.created_at)}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
