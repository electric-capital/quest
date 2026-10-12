import { useRef, useState } from 'react';
import { deleteConnection, updateConnection } from '../../api/client';
import type { ConnectionRow, ConnectorRow } from '../../api/types';
import { ServiceIcon } from './serviceIcons';
import { ConnectionForm } from './ConnectionForm';
import {
  DESCRIPTION_MAX_LENGTH,
  accountLabel,
  connectionErrorMessage,
  normalizeDescription,
} from './connectionHelpers';

/**
 * Inline description editor: a borderless single-line input that saves on
 * blur (Enter blurs) via PUT {description}, skips the PUT when nothing
 * changed, and reverts on Escape. Remounted by its parent (key) whenever the
 * stored description changes, so the draft never lags behind the server.
 */
function DescriptionEditor({
  connection,
  serviceLabel,
  onSave,
}: {
  connection: ConnectionRow;
  serviceLabel: string;
  onSave: (description: string | null) => Promise<void>;
}) {
  const saved = connection.description ?? '';
  const [draft, setDraft] = useState(saved);
  const [status, setStatus] = useState<'idle' | 'saving' | 'error'>('idle');
  const [error, setError] = useState('');
  const revertingRef = useRef(false);

  const commit = async () => {
    if (revertingRef.current) {
      revertingRef.current = false;
      return;
    }
    const next = normalizeDescription(draft);
    if (next === normalizeDescription(saved)) {
      setDraft(saved);
      if (status === 'error') setStatus('idle');
      return;
    }
    setDraft(next);
    setStatus('saving');
    try {
      await onSave(next || null);
      setStatus('idle');
    } catch (err) {
      setError(connectionErrorMessage(err, serviceLabel));
      setStatus('error');
    }
  };

  const inputId = `connection-description-${connection.id}`;
  return (
    <div className="connection-description">
      <input
        id={inputId}
        className="connection-description-input"
        type="text"
        value={draft}
        maxLength={DESCRIPTION_MAX_LENGTH}
        placeholder="Add description"
        aria-label={`Description for ${accountLabel(connection)}`}
        disabled={status === 'saving' || undefined}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            e.currentTarget.blur();
          } else if (e.key === 'Escape') {
            // Revert without closing the Settings modal.
            e.preventDefault();
            e.stopPropagation();
            revertingRef.current = true;
            setDraft(saved);
            if (status === 'error') setStatus('idle');
            e.currentTarget.blur();
          }
        }}
      />
      {status === 'saving' && <span className="connection-description-status">Saving...</span>}
      {status === 'error' && (
        <span className="connection-description-status error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

export interface ConnectionCardProps {
  connector: ConnectorRow;
  connection: ConnectionRow;
  highlighted: boolean;
  /** Refresh the connectors list and the app-level connection status. */
  onChanged: () => void;
  /** Open an OAuth popup (oauth-kind Reconnect). */
  onOAuthConnect: (url: string) => void;
}

/**
 * One connection of a multi-connection service: brand icon, account and
 * service label, the editable description, a Default chip or a Make default
 * action, per-connection needs_reauth / warning badges, Reconnect (an inline
 * re-entry form, or the OAuth popup for oauth kind) and Disconnect.
 */
export function ConnectionCard({ connector, connection, highlighted, onChanged, onOAuthConnect }: ConnectionCardProps) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [reconnectOpen, setReconnectOpen] = useState(false);
  const url = connector.connections_url || '';
  const account = accountLabel(connection);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setActionError('');
    try {
      await action();
      onChanged();
    } catch (err) {
      setActionError(connectionErrorMessage(err, connector.label));
    } finally {
      setBusy(false);
    }
  };

  const handleMakeDefault = () => run(() => updateConnection(url, connection.id, { is_default: true }));

  const handleDisconnect = () => {
    if (!confirm(`Remove the ${connector.label} connection ${account}?`)) return;
    void run(() => deleteConnection(url, connection.id));
  };

  const handleReconnect = () => {
    if (connector.kind === 'oauth') {
      onOAuthConnect(connector.connect_url || '');
    } else {
      setReconnectOpen((open) => !open);
    }
  };

  const saveDescription = async (description: string | null) => {
    await updateConnection(url, connection.id, { description });
    onChanged();
  };

  const className = [
    'connector-row',
    'connection-card',
    highlighted ? 'connection-card-highlight' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div
      className={className}
      id={`connection-${connector.service}-${connection.id}`}
      data-connection-id={connection.id}
    >
      <span className="connector-row-icon">
        <ServiceIcon service={connector.service} />
      </span>
      <div className="connection-card-main">
        <div className="connection-card-title">
          <span className={connection.account ? 'connection-account' : 'connection-account connection-account-unnamed'}>
            {account}
          </span>
          {connection.is_default && <span className="connector-badge connection-chip-default">Default</span>}
          {connection.needs_reauth && (
            <span className="connector-badge connector-badge-warn">
              {connector.kind === 'oauth' ? 'Update Available' : 'Needs re-entry'}
            </span>
          )}
        </div>
        <span className="connector-detail">{connector.label}</span>
        <DescriptionEditor
          key={connection.description ?? ''}
          connection={connection}
          serviceLabel={connector.label}
          onSave={saveDescription}
        />
        {connection.warning && <div className="connection-card-warning">{connection.warning}</div>}
        {actionError && (
          <div className="connection-card-error" role="alert">
            {actionError}
          </div>
        )}
      </div>
      <div className="connection-card-actions">
        {!connection.is_default && (
          <button type="button" className="connection-link-btn" onClick={handleMakeDefault} disabled={busy}>
            Make default
          </button>
        )}
        <button
          type="button"
          className={`connector-btn ${connection.needs_reauth ? 'connector-btn-connect' : 'connector-btn-reconnect'}`}
          onClick={handleReconnect}
          disabled={busy}
          aria-expanded={connector.kind === 'oauth' ? undefined : reconnectOpen}
          aria-label={`Reconnect ${account}`}
        >
          Reconnect
        </button>
        <button
          type="button"
          className="connector-btn-disconnect"
          onClick={handleDisconnect}
          disabled={busy}
          aria-label={`Disconnect ${account}`}
        >
          Disconnect
        </button>
      </div>
      {reconnectOpen && (
        <div className="connection-card-panel">
          <div className="add-connection-header">
            <span className="add-connection-title">Reconnect {account}</span>
            <button type="button" className="add-connection-cancel" onClick={() => setReconnectOpen(false)}>
              Cancel
            </button>
          </div>
          <ConnectionForm
            connector={connector}
            connection={connection}
            onDone={() => {
              setReconnectOpen(false);
              onChanged();
            }}
          />
        </div>
      )}
    </div>
  );
}
