import { useState, useEffect, useCallback, useRef } from 'react';
import { fetchSettings, updateSettings } from '../../api/client';
import { getSelectableModels, getModelDisplayName } from '../../constants/models';
import './SlackSection.css';

type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

// Mirrors MIN/MAX_INTERVAL_MINUTES in chat/slack_notifier.py.
const MIN_NOTIFY_INTERVAL_MINUTES = 5;
const MAX_NOTIFY_INTERVAL_MINUTES = 1440;
const DEFAULT_NOTIFY_INTERVAL_MINUTES = 60;

export function SlackSection() {
  const [model, setModel] = useState<string>('');
  const [notifyEnabled, setNotifyEnabled] = useState(true);
  const [notifyInterval, setNotifyInterval] = useState<string>(
    String(DEFAULT_NOTIFY_INTERVAL_MINUTES)
  );
  const [loading, setLoading] = useState(true);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [saveError, setSaveError] = useState('');
  const statusTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        const response = await fetchSettings();
        setModel(response.settings.slack_default_model ?? '');
        // Absent/null means enabled; false is the only stored off state.
        setNotifyEnabled(
          response.settings.slack_pending_notifications_enabled !== false
        );
        setNotifyInterval(
          String(
            response.settings.slack_pending_notification_interval_minutes ??
              DEFAULT_NOTIFY_INTERVAL_MINUTES
          )
        );
      } catch (error) {
        console.error('Failed to load settings:', error);
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  useEffect(() => {
    return () => {
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
    };
  }, []);

  const parsedInterval = parseInt(notifyInterval, 10);
  const intervalValid =
    Number.isFinite(parsedInterval) &&
    parsedInterval >= MIN_NOTIFY_INTERVAL_MINUTES &&
    parsedInterval <= MAX_NOTIFY_INTERVAL_MINUTES;

  const handleSave = useCallback(async () => {
    setSaveStatus('saving');
    setSaveError('');
    try {
      // Backend treats an empty string as "clear" (stored as NULL). Sending
      // null would be silently dropped by the exclude_none filter in the
      // settings update handler. The interval uses 0 as its "clear" value;
      // the server default is sent as 0 so a later default change applies.
      await updateSettings({
        slack_default_model: model,
        slack_pending_notifications_enabled: notifyEnabled,
        slack_pending_notification_interval_minutes:
          parsedInterval === DEFAULT_NOTIFY_INTERVAL_MINUTES ? 0 : parsedInterval,
      });
      setSaveStatus('saved');
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      statusTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 2000);
    } catch (error) {
      console.error('Failed to save Slack settings:', error);
      setSaveStatus('error');
      setSaveError(error instanceof Error ? error.message : 'Failed to save');
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      statusTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 3000);
    }
  }, [model, notifyEnabled, parsedInterval]);

  return (
    <div className="settings-section">
      <h3>Slack</h3>
      <p className="settings-description">
        Choose which model Quest uses when it starts a new conversation from a Slack DM.
        Leave as "Server default" to use the server's configured model.
      </p>
      {loading ? (
        <div className="settings-loading">Loading...</div>
      ) : (
        <>
          <div className="slack-settings-row">
            <label htmlFor="slack-default-model" className="slack-settings-label">
              Default model for Slack DMs
            </label>
            <select
              id="slack-default-model"
              className="slack-settings-select"
              value={model}
              onChange={(e) => setModel(e.target.value)}
            >
              <option value="">Server default</option>
              {/* Keep a stored deprecated model visible so saving other
                  settings doesn't silently switch it. */}
              {model && !getSelectableModels().some((m) => m.id === model) && (
                <option value={model}>{getModelDisplayName(model)} (deprecated)</option>
              )}
              {getSelectableModels().map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
          </div>
          <div className="slack-settings-row">
            <label className="slack-settings-checkbox-label">
              <input
                type="checkbox"
                checked={notifyEnabled}
                onChange={(e) => setNotifyEnabled(e.target.checked)}
              />
              Remind me on Slack about my inbox
            </label>
            <p className="slack-settings-hint">
              While you have requests waiting for approval or suggested memories
              to review, Quest sends you a Slack DM at most once per interval.
            </p>
          </div>
          <div className="slack-settings-row">
            <label htmlFor="slack-notify-interval" className="slack-settings-label">
              Reminder interval (minutes)
            </label>
            <input
              id="slack-notify-interval"
              type="number"
              className="slack-settings-number"
              min={MIN_NOTIFY_INTERVAL_MINUTES}
              max={MAX_NOTIFY_INTERVAL_MINUTES}
              value={notifyInterval}
              disabled={!notifyEnabled}
              onChange={(e) => setNotifyInterval(e.target.value)}
            />
            {notifyEnabled && !intervalValid && (
              <p className="slack-settings-hint slack-settings-hint-error">
                Enter a value between {MIN_NOTIFY_INTERVAL_MINUTES} and{' '}
                {MAX_NOTIFY_INTERVAL_MINUTES} minutes.
              </p>
            )}
          </div>
          <div className="settings-actions">
            <button
              className="settings-save-button"
              onClick={handleSave}
              disabled={saveStatus === 'saving' || (notifyEnabled && !intervalValid)}
            >
              {saveStatus === 'saving' ? 'Saving...' : 'Save'}
            </button>
            {saveStatus === 'saved' && (
              <span className="settings-save-status saved">Saved</span>
            )}
            {saveStatus === 'error' && (
              <span className="settings-save-status error">
                {saveError || 'Failed to save'}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
