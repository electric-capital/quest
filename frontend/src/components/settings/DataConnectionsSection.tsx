import { useState, useEffect, useCallback } from 'react';
import { fetchConnectors, saveConnectorKey, disconnectConnector } from '../../api/client';
import type { ConnectorRow } from '../../api/types';
import { openOAuthPopup } from '../../utils/oauthPopup';
import { ServiceIcon } from './serviceIcons';
import './DataConnectionsSection.css';

function ApiKeyForm({ placeholder, onSave }: { placeholder: string; onSave: (key: string) => Promise<void> }) {
  const [keyValue, setKeyValue] = useState('');
  const [status, setStatus] = useState<'idle' | 'saving' | 'error'>('idle');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!keyValue.trim()) return;

    setStatus('saving');
    try {
      await onSave(keyValue.trim());
      setKeyValue('');
      setStatus('idle');
    } catch {
      setStatus('error');
    }
  };

  return (
    <form className="api-key-form" onSubmit={handleSubmit}>
      <input
        type="text"
        value={keyValue}
        onChange={(e) => setKeyValue(e.target.value)}
        placeholder={placeholder}
        className="api-key-input"
        autoFocus
      />
      <button type="submit" className="connector-btn connector-btn-connect" disabled={status === 'saving' || !keyValue.trim()}>
        {status === 'saving' ? 'Saving...' : 'Save'}
      </button>
      {status === 'error' && <span className="api-key-status error">Error</span>}
    </form>
  );
}

interface DataConnectionsSectionProps {
  refreshConnectionStatus: () => void;
}

// The add-flow panel is either closed, showing the service picker, or
// showing the key-entry step for one picked api_key service.
type AddFlowState =
  | { step: 'closed' }
  | { step: 'pick' }
  | { step: 'enter_key'; service: string };

/**
 * The user's Data Connections, rendered generically from the
 * GET /connectors list. Only connected rows are listed; new connections
 * are added through an explicit flow: "Add Connection" opens a picker of
 * the supported (server-available, not-yet-connected) services, and
 * picking one triggers that service's flow -- an OAuth popup via the
 * row's connect_url, or a key-entry step posting to the row's key_url.
 * Adding a connector (core or plugin) requires no change here.
 */
export function DataConnectionsSection({ refreshConnectionStatus }: DataConnectionsSectionProps) {
  const [connectors, setConnectors] = useState<ConnectorRow[] | null>(null);
  const [isLoadingConnectors, setIsLoadingConnectors] = useState(false);
  const [pendingOAuthPopup, setPendingOAuthPopup] = useState<Window | null>(null);
  const [popupBlockedUrl, setPopupBlockedUrl] = useState<string | null>(null);
  const [addFlow, setAddFlow] = useState<AddFlowState>({ step: 'closed' });

  // Load connectors on mount
  useEffect(() => {
    const loadConnectors = async () => {
      setIsLoadingConnectors(true);
      try {
        const response = await fetchConnectors();
        setConnectors(response.connectors);
      } catch (error) {
        console.error('Failed to load connectors:', error);
      } finally {
        setIsLoadingConnectors(false);
      }
    };

    loadConnectors();
  }, []);

  const refreshConnectors = useCallback(() => {
    fetchConnectors().then((r) => setConnectors(r.connectors));
  }, []);

  // Handle opening an OAuth flow in a popup window
  const handleOAuthConnect = useCallback((url: string) => {
    // Close any existing popup
    if (pendingOAuthPopup && !pendingOAuthPopup.closed) {
      pendingOAuthPopup.close();
    }

    const popup = openOAuthPopup(url);

    if (popup) {
      setPendingOAuthPopup(popup);
      setPopupBlockedUrl(null);
    } else {
      // Popup was blocked
      setPopupBlockedUrl(url);
    }
  }, [pendingOAuthPopup]);

  // Listen for OAuth popup completion via postMessage
  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      // Verify origin for security
      if (event.origin !== window.location.origin) return;

      if (event.data?.type === 'oauth_callback_success') {
        // Re-fetch connectors to show updated status
        setPendingOAuthPopup(null);
        setPopupBlockedUrl(null);
        refreshConnectors();
        // Also refresh the app-level connection status
        refreshConnectionStatus();
      } else if (event.data?.type === 'oauth_callback_error') {
        setPendingOAuthPopup(null);
        // Optionally show error in the settings modal
        console.error(`OAuth error for ${event.data.service}: ${event.data.error}`);
        // Re-fetch connectors anyway (status may have changed)
        refreshConnectors();
      }
    };

    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [refreshConnectionStatus, refreshConnectors]);

  // Poll for popup closure as a fallback
  useEffect(() => {
    if (!pendingOAuthPopup) return;

    const interval = setInterval(() => {
      if (pendingOAuthPopup.closed) {
        setPendingOAuthPopup(null);
        setPopupBlockedUrl(null);
        // Re-fetch connectors -- the flow may have completed
        refreshConnectors();
        refreshConnectionStatus();
      }
    }, 500);

    return () => clearInterval(interval);
  }, [pendingOAuthPopup, refreshConnectionStatus, refreshConnectors]);

  const handleDisconnect = async (connector: ConnectorRow) => {
    if (!connector.disconnect_url) return;
    if (!confirm(`Remove ${connector.label} connection?`)) return;

    try {
      await disconnectConnector(connector.disconnect_url);
      refreshConnectors();
      refreshConnectionStatus();
    } catch (error) {
      console.error(`Failed to disconnect ${connector.label}:`, error);
      alert((error as Error).message || `Failed to disconnect ${connector.label}`);
    }
  };

  // Pick a service in the add flow: OAuth services launch their popup
  // immediately (the row appears in the list once the flow completes and
  // status refreshes); api_key services advance to the key-entry step.
  const handlePickService = (connector: ConnectorRow) => {
    if (connector.kind === 'oauth') {
      setAddFlow({ step: 'closed' });
      handleOAuthConnect(connector.connect_url || '');
    } else {
      setAddFlow({ step: 'enter_key', service: connector.service });
    }
  };

  const renderConnectedActions = (connector: ConnectorRow) => {
    if (connector.kind === 'oauth') {
      const connect = () => handleOAuthConnect(connector.connect_url || '');
      if (connector.needs_reauth) {
        return (
          <>
            <span className="connector-badge connector-badge-warn">Update Available</span>
            <button className="connector-btn connector-btn-connect" onClick={connect}>
              Re-authorize
            </button>
          </>
        );
      }
      return (
        <>
          <span className="connector-badge connector-badge-ok">Connected</span>
          <button className="connector-btn connector-btn-reconnect" onClick={connect}>
            Reconnect
          </button>
        </>
      );
    }
    return (
      <>
        <span className="connector-badge connector-badge-ok">Connected</span>
        <div className="connector-key-connected">
          {connector.key_hint && (
            <span className="connector-key-hint">...{connector.key_hint}</span>
          )}
          <button onClick={() => handleDisconnect(connector)} className="connector-btn-disconnect">
            Disconnect
          </button>
        </div>
      </>
    );
  };

  const renderAddFlow = (addable: ConnectorRow[]) => {
    if (addFlow.step === 'closed') {
      if (addable.length === 0) return null;
      return (
        <button
          className="connector-btn connector-btn-connect add-connection-btn"
          onClick={() => setAddFlow({ step: 'pick' })}
        >
          + Add Connection
        </button>
      );
    }

    if (addFlow.step === 'enter_key') {
      // Resolve the picked service against the current connector list so
      // a background refresh never leaves the step on stale row data.
      const connector = addable.find((c) => c.service === addFlow.service);
      if (!connector) {
        // Connected elsewhere (or no longer available) -- fall back to the picker.
        return renderAddPicker(addable);
      }
      return (
        <div className="add-connection-panel">
          <div className="add-connection-header">
            <span className="add-connection-title">Connect {connector.label}</span>
            <button className="add-connection-cancel" onClick={() => setAddFlow({ step: 'pick' })}>
              Back
            </button>
          </div>
          {(connector.key_help || (connector.key_help_links && connector.key_help_links.length > 0)) && (
            <p className="add-connection-help">
              {connector.key_help && <span>{connector.key_help}</span>}
              {connector.key_help_links && connector.key_help_links.length > 0 && (
                <span className="add-connection-help-links">
                  {connector.key_help_links.map((link, index) => (
                    <span key={link.url}>
                      {index > 0 && <span className="add-connection-help-sep" aria-hidden="true">·</span>}
                      <a href={link.url} target="_blank" rel="noopener noreferrer">
                        {link.label}
                      </a>
                    </span>
                  ))}
                </span>
              )}
            </p>
          )}
          <div className="add-connection-keystep">
            <ApiKeyForm
              placeholder={connector.key_placeholder || 'Paste API key'}
              onSave={async (key) => {
                await saveConnectorKey(connector.key_url || '', connector.key_field || 'key', key);
                setAddFlow({ step: 'closed' });
                refreshConnectors();
                refreshConnectionStatus();
              }}
            />
          </div>
        </div>
      );
    }

    return renderAddPicker(addable);
  };

  const renderAddPicker = (addable: ConnectorRow[]) => (
    <div className="add-connection-panel">
      <div className="add-connection-header">
        <span className="add-connection-title">Add a connection</span>
        <button className="add-connection-cancel" onClick={() => setAddFlow({ step: 'closed' })}>
          Cancel
        </button>
      </div>
      {addable.length === 0 ? (
        <p className="add-connection-empty">All supported services are already connected.</p>
      ) : (
        <div className="add-connection-grid">
          {addable.map((connector) => (
            <button
              key={connector.service}
              className="add-connection-tile"
              title={connector.description || connector.label}
              onClick={() => handlePickService(connector)}
            >
              <span className="add-connection-tile-icon">
                <ServiceIcon service={connector.service} />
              </span>
              <span className="add-connection-tile-name">{connector.label}</span>
              <span className="add-connection-tile-kind">
                {connector.kind === 'oauth' ? 'Sign in' : 'API key'}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );

  // Rows the server reports as unavailable (unconfigured plugin/service)
  // are hidden from both the connected list and the add picker.
  const visible = (connectors || []).filter((c) => c.available !== false);
  const connectedRows = visible.filter((c) => c.connected);
  const addableRows = visible.filter((c) => !c.connected);

  return (
    <div className="settings-section">
      <h3>Data Connections</h3>
      <p className="settings-description">
        Manage your connected services. These connections allow the AI assistant to access your data on your behalf.
      </p>
      {isLoadingConnectors && !connectors ? (
        <div className="settings-loading">Loading connections...</div>
      ) : connectors ? (
        <>
          {connectedRows.length === 0 ? (
            <p className="connectors-empty">
              No connections yet. Add a connection to let the assistant access your data.
            </p>
          ) : (
            <div className="connectors-list">
              {connectedRows.map((connector) => (
                <div className="connector-row" key={connector.service}>
                  <span className="connector-row-icon">
                    <ServiceIcon service={connector.service} />
                  </span>
                  <div className="connector-info">
                    <span className="connector-name">{connector.label}</span>
                    {connector.description && (
                      <span className="connector-detail">{connector.description}</span>
                    )}
                  </div>
                  {renderConnectedActions(connector)}
                </div>
              ))}
            </div>
          )}
          {renderAddFlow(addableRows)}
        </>
      ) : null}
      {popupBlockedUrl && (
        <div className="popup-blocked-notice">
          <p>Popup was blocked by your browser.{' '}
            <a href={popupBlockedUrl} target="_blank" rel="noopener">
              Click here to open in a new tab
            </a>, or allow popups for this site.
          </p>
        </div>
      )}
    </div>
  );
}
