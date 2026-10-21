// Inline "Connect <service>" card opened by the model's
// request_service_connection tool (chat/connect_requests.py). The agent is
// suspended on a service_connection wait handle while the card is open:
// Connect runs the same flow Settings > Data Connections would (an OAuth
// popup, or the Settings add flow for key / form services) and then asks
// the server to verify the connection; Not now declines. Either outcome
// resolves the handle and resumes the agent.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { resolveConnectRequest } from '../api/client';
import { endpoints } from '../api/config';
import { ApiClientError } from '../api/request';
import type { ConnectRequestMessage as ConnectRequestMessageType, ConnectRequestStatus } from '../api/types';
import { useAuth } from '../contexts/AuthContext';
import { useNavigationState } from '../contexts/NavigationContext';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { openOAuthPopup } from '../utils/oauthPopup';
import { AnimatedWidthButton } from './AnimatedWidthButton';
import { ServiceIcon } from './settings/serviceIcons';
import './ConnectRequestMessage.css';

interface ConnectRequestMessageProps {
  message: ConnectRequestMessageType;
}

/** Wait-handle status -> card status (accepted = verified connected). */
function statusFromHandle(handleStatus: string | undefined): ConnectRequestStatus | null {
  switch (handleStatus) {
    case 'pending':
      return null;
    case 'accepted':
      return 'connected';
    case 'rejected':
      return 'declined';
    default:
      // cancelled (conversation stopped), timed_out, stopped
      return 'cancelled';
  }
}

function errorMessage(e: unknown, fallback: string): string {
  if (e instanceof ApiClientError && e.errorCode === 'already_resolved') {
    return 'This request was already resolved elsewhere.';
  }
  return e instanceof Error && e.message ? e.message : fallback;
}

export const ConnectRequestMessage = React.memo(function ConnectRequestMessage({ message }: ConnectRequestMessageProps) {
  const [status, setStatus] = useState<ConnectRequestStatus>(message.status);
  const [pending, setPending] = useState<'connect' | 'check' | 'decline' | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The server said the service is still not connected after a check:
  // show a retry row instead of silently doing nothing.
  const [notConnectedYet, setNotConnectedYet] = useState(false);
  const [popup, setPopup] = useState<Window | null>(null);
  const [popupBlockedUrl, setPopupBlockedUrl] = useState<string | null>(null);
  // True once this card sent the user to Settings, so closing the modal
  // triggers a connection check.
  const sentToSettings = useRef(false);
  const { refreshConnectionStatus } = useAuth();
  const { isSettingsOpen, setSettingsOpen } = useNavigationState();

  const usesPopup = message.kind === 'oauth' && !!message.connect_url;
  const verb = message.needs_reauth ? 'Re-authorize' : 'Connect';

  // Verify server-side. Only the server decides that the service is
  // connected (it re-reads the user's credentials); a negative answer
  // leaves the handle pending so the user can retry or decline.
  const check = useCallback(async () => {
    setPending('check');
    setError(null);
    try {
      const result = await resolveConnectRequest(message.wait_handle_id, 'check');
      if (result.resolved) {
        setStatus('connected');
        setNotConnectedYet(false);
        refreshConnectionStatus();
      } else {
        setNotConnectedYet(true);
      }
    } catch (e) {
      if (e instanceof ApiClientError && e.errorCode === 'already_resolved') {
        const handleStatus = e.details?.status as string | undefined;
        setStatus(statusFromHandle(handleStatus) ?? 'cancelled');
      } else {
        setError(errorMessage(e, 'Could not verify the connection'));
      }
    } finally {
      setPending(null);
    }
  }, [message.wait_handle_id, refreshConnectionStatus]);

  const decline = useCallback(async () => {
    setPending('decline');
    setError(null);
    try {
      await resolveConnectRequest(message.wait_handle_id, 'decline');
      setStatus('declined');
    } catch (e) {
      if (e instanceof ApiClientError && e.errorCode === 'already_resolved') {
        const handleStatus = e.details?.status as string | undefined;
        setStatus(statusFromHandle(handleStatus) ?? 'cancelled');
      } else {
        setError(errorMessage(e, 'Could not decline the request'));
      }
    } finally {
      setPending(null);
    }
  }, [message.wait_handle_id]);

  const connect = useCallback(() => {
    setError(null);
    setNotConnectedYet(false);
    if (usesPopup) {
      if (popup && !popup.closed) popup.close();
      const win = openOAuthPopup(message.connect_url as string);
      if (win) {
        setPopup(win);
        setPopupBlockedUrl(null);
      } else {
        setPopupBlockedUrl(message.connect_url as string);
      }
    } else {
      sentToSettings.current = true;
      setSettingsOpen(true);
    }
  }, [usesPopup, popup, message.connect_url, setSettingsOpen]);

  // OAuth popup completion: the callback page posts oauth_callback_success
  // / oauth_callback_error to the opener (docs/architecture/oauth-popup.md);
  // polling popup.closed covers a popup closed by hand. Either way the
  // server verifies -- the postMessage is only a cue to check sooner.
  useEffect(() => {
    if (!popup) return;
    let handled = false;
    const finish = (ok: boolean, err?: string) => {
      if (handled) return;
      handled = true;
      setPopup(null);
      if (err) setError(err);
      if (ok || !err) void check();
    };
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const type = event.data?.type;
      if (type === 'oauth_callback_success') finish(true);
      else if (type === 'oauth_callback_error') finish(false, String(event.data.error || 'Sign-in failed'));
    };
    window.addEventListener('message', onMessage);
    const interval = setInterval(() => {
      if (popup.closed) finish(true);
    }, 500);
    return () => {
      window.removeEventListener('message', onMessage);
      clearInterval(interval);
    };
  }, [popup, check]);

  // Settings add flow: when the modal this card opened closes, check.
  useEffect(() => {
    if (isSettingsOpen || !sentToSettings.current) return;
    sentToSettings.current = false;
    if (status === 'open') void check();
  }, [isSettingsOpen, status, check]);

  // On mount, an open card re-reads its handle: the transcript row may
  // predate a resolve from another tab or a stop.
  useEffect(() => {
    if (message.status !== 'open') return;
    fetch(endpoints.waitHandle(message.wait_handle_id), { credentials: 'include' })
      .then((res) => (res.ok ? res.json() : null))
      .then((handle) => {
        if (!handle) return;
        const next = statusFromHandle(handle.status as string | undefined);
        if (next) setStatus(next);
      })
      .catch(() => {
        // Ignore: the live event below and the user's own action still apply.
      });
  }, [message.status, message.wait_handle_id]);

  // Live flip from any resolver (other tab, stop, timeout).
  useEffect(() => persistentWebSocket.onGlobalEvent((event) => {
    if (event.type !== 'wait_handle_resolved') return;
    if (event.handle_id !== message.wait_handle_id) return;
    const next = statusFromHandle(event.status as string | undefined);
    if (next) setStatus(next);
  }), [message.wait_handle_id]);

  const isBusy = pending !== null;

  if (status !== 'open') {
    const outcome = status === 'connected'
      ? (message.needs_reauth ? 'Re-authorized' : 'Connected')
      : status === 'declined' ? 'Not now' : 'Cancelled';
    return (
      <div className={`connect-request-message collapsed ${status}`} data-testid="connect-request-card">
        <div className="connect-request-icon"><ServiceIcon service={message.service} /></div>
        <div className="connect-request-outcome">
          <span className="connect-request-outcome-status">{outcome}:</span>{' '}
          <span className="connect-request-outcome-label">{message.label}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="connect-request-message pending" data-testid="connect-request-card">
      <div className="connect-request-icon"><ServiceIcon service={message.service} /></div>
      <div className="connect-request-body">
        <div className="connect-request-title">{verb} {message.label}</div>
        {message.reason && <div className="connect-request-reason">{message.reason}</div>}
        {message.description && <div className="connect-request-description">{message.description}</div>}
        {!usesPopup && (
          <div className="connect-request-hint">
            Add it under Settings &gt; Data Connections, then come back here.
          </div>
        )}
        {error && <div className="connect-request-error">{error}</div>}
        {notConnectedYet && (
          <div className="connect-request-notice">
            {message.label} is not connected yet. Finish the sign-in, then check again.
          </div>
        )}
        {popupBlockedUrl && (
          <div className="connect-request-notice">
            The sign-in window was blocked.{' '}
            <a href={popupBlockedUrl} target="_blank" rel="noopener">Open it in a new tab</a>, then check again.
          </div>
        )}
        <div className="connect-request-buttons">
          <AnimatedWidthButton
            className="connect-request-btn connect"
            onClick={connect}
            disabled={isBusy}
          >
            {usesPopup ? verb : 'Open Settings'}
          </AnimatedWidthButton>
          {(notConnectedYet || popupBlockedUrl || sentToSettings.current) && (
            <AnimatedWidthButton
              className="connect-request-btn check"
              onClick={() => void check()}
              disabled={isBusy}
            >
              {pending === 'check' ? 'Checking...' : 'Check again'}
            </AnimatedWidthButton>
          )}
          <AnimatedWidthButton
            className="connect-request-btn decline"
            onClick={() => void decline()}
            disabled={isBusy}
            title="Continue without connecting this service"
          >
            {pending === 'decline' ? 'Declining...' : 'Not now'}
          </AnimatedWidthButton>
        </div>
      </div>
    </div>
  );
});
