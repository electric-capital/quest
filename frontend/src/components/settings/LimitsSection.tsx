import { useCallback, useEffect, useState } from 'react';
import { fetchAdminLimits, updateAdminLimits } from '../../api/client';
import type { AdminLimits } from '../../api/types';
import { useAppConfig } from '../../contexts/AppConfigContext';
import './ServiceCredentialsSection.css';
import './LimitsSection.css';

/**
 * Admin-only server limits (persisted in data/limits.json). Today: the
 * per-file workspace upload cap, which applies to file-browser uploads in
 * conversation and project workspaces and to composer file attachments.
 * After a save it re-fetches the app config so this tab's composer picks
 * up the new cap without a reload.
 */
export function LimitsSection() {
  const { refreshAppConfig } = useAppConfig();
  const [limits, setLimits] = useState<AdminLimits | null>(null);
  const [loadError, setLoadError] = useState('');
  const [draft, setDraft] = useState('');
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [saveError, setSaveError] = useState('');

  useEffect(() => {
    fetchAdminLimits()
      .then((result) => {
        setLimits(result);
        setDraft(String(result.max_upload_mb));
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : 'Failed to load limits.'));
  }, []);

  const parsed = Number(draft);
  const valid =
    limits !== null &&
    draft.trim() !== '' &&
    Number.isInteger(parsed) &&
    parsed >= limits.max_upload_mb_min &&
    parsed <= limits.max_upload_mb_max;
  const dirty = limits !== null && valid && parsed !== limits.max_upload_mb;

  const handleSave = useCallback(async () => {
    if (!dirty) return;
    setSaveStatus('saving');
    setSaveError('');
    try {
      const result = await updateAdminLimits({ max_upload_mb: parsed });
      setLimits(result);
      setDraft(String(result.max_upload_mb));
      setSaveStatus('saved');
      void refreshAppConfig();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save');
      setSaveStatus('error');
    }
  }, [dirty, parsed, refreshAppConfig]);

  if (loadError) {
    return (
      <div className="settings-section">
        <h3>Limits</h3>
        <div className="svc-cred-load-error">{loadError}</div>
      </div>
    );
  }
  if (!limits) {
    return (
      <div className="settings-section">
        <h3>Limits</h3>
        <div className="settings-loading">Loading...</div>
      </div>
    );
  }

  return (
    <div className="settings-section">
      <h3>Limits</h3>
      <p className="settings-description">Server-wide limits that apply to every user.</p>
      <div className="svc-cred-card">
        <div className="svc-cred-card-header">
          <h4>Workspace file uploads</h4>
        </div>
        <p className="settings-description">
          Largest single file a user can upload to a conversation or project workspace, including
          files attached in the composer. Uploads are held in server memory while they are saved,
          so keep this within what the server can spare. A reverse proxy in front of Quest may
          enforce its own request size limit (e.g. Nginx <code>client_max_body_size</code>).
        </p>
        <label className="limits-field">
          <span className="limits-field-label">Maximum file size</span>
          <input
            type="number"
            className="svc-cred-input limits-number-input"
            min={limits.max_upload_mb_min}
            max={limits.max_upload_mb_max}
            step={1}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setSaveStatus('idle');
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void handleSave();
              }
            }}
          />
          <span className="limits-field-unit">MB</span>
        </label>
        <p className="limits-hint">
          {limits.max_upload_mb_min}–{limits.max_upload_mb_max} MB; default{' '}
          {limits.max_upload_mb_default} MB.
        </p>
        {!valid && (
          <p className="limits-invalid settings-save-status error" role="alert">
            Enter a whole number from {limits.max_upload_mb_min} to {limits.max_upload_mb_max}.
          </p>
        )}
        <div className="settings-actions">
          <button
            className="settings-save-button"
            onClick={() => void handleSave()}
            disabled={!dirty || saveStatus === 'saving'}
          >
            {saveStatus === 'saving' ? 'Saving...' : 'Save'}
          </button>
          {saveStatus === 'saved' && <span className="settings-save-status saved">Saved</span>}
          {saveStatus === 'error' && (
            <span className="settings-save-status error">{saveError || 'Failed to save'}</span>
          )}
        </div>
      </div>
    </div>
  );
}
