import { useCallback, useEffect, useMemo, useState } from 'react';
import { LoaderCircle, Moon } from 'lucide-react';
import { fetchDreamingSettings, runDreamNow, updateDreamingSettings } from '../../api/client';
import type { DreamingAdminResponse, DreamingModelRow, DreamNowResponse, DreamRun } from '../../api/types';
import { formatRelativeTimestamp } from '../../utils/formatters';
import './ServiceCredentialsSection.css';
import './DreamingSection.css';

/**
 * Admin-only settings for dreaming (chat/dreaming.py): the model and
 * thinking level the periodic memory reviews run on (persisted in
 * data/dreaming.json), a run-now for the admin's own memories, and the
 * recent run log. The feature itself is switched on under Features; this
 * section stays usable while it is off so the model can be picked first.
 */

const UNAVAILABLE_LABELS: Record<NonNullable<DreamingModelRow['unavailable_reason']>, string> = {
  not_allowed_private: 'not allowed in private conversations',
  not_configured: 'no credentials',
  failing: 'failing health check',
};

const SKIP_LABELS: Record<string, string> = {
  pending_review: 'You still have suggestions from an earlier review waiting in your inbox. Review those first.',
  too_few_memories: 'You need at least two saved memories before there is anything to review.',
  running: 'A review of your memories is already running.',
};

function formatCost(cost: number | null): string {
  if (cost === null) return '—';
  return cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`;
}

function describeRun(run: DreamRun): string {
  if (run.status === 'failed') return `Failed: ${run.error || 'unknown error'}`;
  if (run.status === 'running') return 'Running...';
  const n = run.suggestions_created;
  return `Read ${run.memories_read} memories, proposed ${n} change${n === 1 ? '' : 's'}`;
}

export function DreamingSection() {
  const [data, setData] = useState<DreamingAdminResponse | null>(null);
  const [loadError, setLoadError] = useState('');
  const [model, setModel] = useState<string>('');
  const [effort, setEffort] = useState<string>('');
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [saveError, setSaveError] = useState('');
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState<DreamNowResponse | null>(null);
  const [runError, setRunError] = useState('');

  const applyResponse = useCallback((response: DreamingAdminResponse) => {
    setData(response);
    setModel(response.settings.model ?? '');
    setEffort(response.settings.thinking_effort ?? '');
  }, []);

  useEffect(() => {
    fetchDreamingSettings()
      .then(applyResponse)
      .catch((err) => setLoadError(err instanceof Error ? err.message : 'Failed to load.'));
  }, [applyResponse]);

  // The levels the thinking dropdown offers: the pinned model's, or the
  // automatic choice's while no model is pinned.
  const effortModelId = model || data?.resolved?.model || null;
  const effortModel = useMemo(
    () => data?.models.find((m) => m.id === effortModelId) ?? null,
    [data, effortModelId],
  );
  const efforts = effortModel?.thinking_efforts ?? [];

  const dirty = data !== null && (
    (data.settings.model ?? '') !== model || (data.settings.thinking_effort ?? '') !== effort
  );

  const handleSave = useCallback(async () => {
    if (!dirty) return;
    setSaveStatus('saving');
    setSaveError('');
    try {
      applyResponse(await updateDreamingSettings({
        model: model || null,
        thinking_effort: effort || null,
      }));
      setSaveStatus('saved');
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save');
      setSaveStatus('error');
    }
  }, [dirty, model, effort, applyResponse]);

  const handleRunNow = useCallback(async () => {
    setRunning(true);
    setRunError('');
    setRunResult(null);
    try {
      const result = await runDreamNow();
      setRunResult(result);
      // The run log has a new row now.
      applyResponse(await fetchDreamingSettings());
    } catch (err) {
      setRunError(err instanceof Error ? err.message : 'The review failed to start');
    } finally {
      setRunning(false);
    }
  }, [applyResponse]);

  if (loadError) {
    return (
      <div className="settings-section">
        <h3>Dreaming</h3>
        <div className="svc-cred-load-error">{loadError}</div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="settings-section">
        <h3>Dreaming</h3>
        <div className="settings-loading">Loading...</div>
      </div>
    );
  }

  const resolvedRow = data.resolved
    ? data.models.find((m) => m.id === data.resolved!.model) ?? null
    : null;

  return (
    <div className="settings-section">
      <h3>Dreaming</h3>
      <p className="settings-description">
        While it is on, Quest periodically reviews each user&apos;s saved memories in the
        background and proposes clean-ups — merging duplicates, archiving stale or
        contradicted facts, fixing relative dates, shortening long entries — as suggestions in
        their inbox. Nothing changes until the user accepts a suggestion. Turn it on or off under
        Features; choose here which model does the reviewing.
      </p>

      <div className="svc-cred-card">
        <div className="svc-cred-card-header">
          <h4>Model</h4>
          <span className={`svc-cred-badge ${data.enabled ? 'configured' : 'unconfigured'}`}>
            {data.enabled ? 'Dreaming enabled' : 'Dreaming disabled'}
          </span>
        </div>
        <p className="settings-description">
          A review reads a user&apos;s private memories, so only models allowed in private
          conversations (Model Selection) are offered. Automatic picks the most capable Claude
          model available and falls back when it is not.
        </p>
        <label className="dreaming-field">
          <span className="dreaming-field-label">Model</span>
          <select
            className="svc-cred-input svc-cred-select dreaming-select"
            value={model}
            onChange={(e) => {
              setModel(e.target.value);
              setEffort('');
              setSaveStatus('idle');
            }}
          >
            <option value="">
              {resolvedRow
                ? `Automatic (currently ${resolvedRow.display_name})`
                : 'Automatic (no model available)'}
            </option>
            {data.models.map((m) => (
              <option key={m.id} value={m.id} disabled={!m.dreamable}>
                {m.display_name} · {m.provider_label}
                {m.unavailable_reason ? ` (${UNAVAILABLE_LABELS[m.unavailable_reason]})` : ''}
              </option>
            ))}
          </select>
        </label>
        <label className="dreaming-field">
          <span className="dreaming-field-label">Thinking</span>
          <select
            className="svc-cred-input svc-cred-select dreaming-select"
            value={effort}
            disabled={efforts.length === 0}
            onChange={(e) => {
              setEffort(e.target.value);
              setSaveStatus('idle');
            }}
          >
            <option value="">
              {effortModel?.default_thinking_effort
                ? `Default (${effortModel.default_thinking_effort})`
                : 'Default'}
            </option>
            {efforts.map((level) => (
              <option key={level} value={level}>{level}</option>
            ))}
          </select>
        </label>
        {data.unavailable_reason && (
          <p className="dreaming-warning" role="note">{data.unavailable_reason}</p>
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

      <div className="svc-cred-card">
        <div className="svc-cred-card-header">
          <h4>Try it on your own memories</h4>
        </div>
        <p className="settings-description">
          Runs a review of your saved memories right away, without waiting for the schedule.
          Any proposals land in your inbox&apos;s Memories tab.
        </p>
        <div className="settings-actions">
          <button
            className="settings-save-button dreaming-run-button"
            onClick={() => void handleRunNow()}
            disabled={running || !data.enabled_for_me}
            title={data.enabled_for_me ? undefined : 'Dreaming is off for you under Features'}
          >
            {running ? (
              <LoaderCircle size={14} className="dreaming-spinner" aria-hidden="true" />
            ) : (
              <Moon size={14} aria-hidden="true" />
            )}
            {running ? 'Reviewing...' : 'Review my memories now'}
          </button>
          {!data.enabled_for_me && (
            <span className="dreaming-hint">Turn dreaming on for yourself under Features first.</span>
          )}
        </div>
        {runError && <p className="settings-save-status error dreaming-result" role="alert">{runError}</p>}
        {runResult && runResult.status === 'skipped' && (
          <p className="dreaming-result" role="status">
            {SKIP_LABELS[runResult.reason] ?? `Skipped: ${runResult.reason}`}
          </p>
        )}
        {runResult && runResult.status !== 'skipped' && (
          <p
            className={`dreaming-result${runResult.status === 'failed' ? ' settings-save-status error' : ''}`}
            role="status"
          >
            {describeRun(runResult.run)}
            {runResult.status === 'completed' && runResult.run.model && (
              <> on {runResult.run.model} ({formatCost(runResult.run.estimated_cost_usd)}).</>
            )}
          </p>
        )}
      </div>

      <div className="svc-cred-card">
        <div className="svc-cred-card-header">
          <h4>Recent reviews</h4>
        </div>
        {data.recent_runs.length === 0 ? (
          <p className="settings-description">No reviews have run yet.</p>
        ) : (
          <table className="dreaming-runs">
            <thead>
              <tr>
                <th>When</th>
                <th>User</th>
                <th>Model</th>
                <th>Result</th>
                <th className="dreaming-runs-cost">Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.recent_runs.map((run) => (
                <tr key={run.id} className={`dreaming-run dreaming-run--${run.status}`}>
                  <td title={run.started_at ?? undefined}>
                    {run.started_at ? formatRelativeTimestamp(run.started_at) : '—'}
                    {run.trigger === 'manual' && <span className="dreaming-run-trigger">manual</span>}
                  </td>
                  <td>{run.user_email ?? `user ${run.user_id}`}</td>
                  <td>{run.model ?? '—'}</td>
                  <td>{describeRun(run)}</td>
                  <td className="dreaming-runs-cost">{formatCost(run.estimated_cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
