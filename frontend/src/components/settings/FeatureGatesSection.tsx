import { useState, useEffect, useCallback, useRef } from 'react';
import {
  fetchFeatureGates,
  updateFeatureGate,
  fetchAdminUsers,
} from '../../api/client';
import type { FeatureGate, UserSearchResult } from '../../api/types';
import { useAuth } from '../../contexts/AuthContext';
import './ServiceCredentialsSection.css';
import './FeatureGatesSection.css';

/**
 * Admin-only on/off switches for optional server-global features (persisted
 * in data/feature_gates.json; every feature is off by default). Toggles save
 * immediately -- there is no separate Save button. Reuses the service
 * credential card + toggle chrome.
 *
 * Gates with supports_user_access (currently public_projects) additionally
 * get an access editor: "All users" vs "Only specific users" with a
 * checkbox roster, each change saved immediately like the toggle.
 */
export function FeatureGatesSection() {
  const { refreshEnabledFeatures } = useAuth();
  const [features, setFeatures] = useState<FeatureGate[] | null>(null);
  const [loadError, setLoadError] = useState('');
  const [savingFeature, setSavingFeature] = useState<string | null>(null);
  const [saveError, setSaveError] = useState('');
  const [users, setUsers] = useState<UserSearchResult[] | null>(null);
  const [usersError, setUsersError] = useState('');
  // Last non-null allowed_users list per feature, so switching a gate to
  // "All users" and back to "Only specific users" restores the selection
  // (the server stores null while the gate is open to everyone).
  const lastSelectionRef = useRef<Record<string, string[]>>({});

  useEffect(() => {
    const load = async () => {
      setLoadError('');
      try {
        const response = await fetchFeatureGates();
        setFeatures(response.features);
        for (const gate of response.features) {
          if (gate.allowed_users !== null) {
            lastSelectionRef.current[gate.feature] = gate.allowed_users;
          }
        }
        if (response.features.some((gate) => gate.supports_user_access)) {
          try {
            const roster = await fetchAdminUsers(true);
            setUsers(roster.users);
          } catch (error) {
            console.error('Failed to load user roster:', error);
            setUsersError(
              error instanceof Error ? error.message : 'Failed to load users',
            );
          }
        }
      } catch (error) {
        console.error('Failed to load feature gates:', error);
        setLoadError(error instanceof Error ? error.message : 'Failed to load');
      }
    };
    load();
  }, []);

  const saveGate = useCallback(
    async (
      feature: string,
      update: { enabled: boolean; allowed_users?: string[] | null },
    ) => {
      setSavingFeature(feature);
      setSaveError('');
      try {
        const updated = await updateFeatureGate(feature, update);
        if (updated.allowed_users !== null) {
          lastSelectionRef.current[updated.feature] = updated.allowed_users;
        }
        setFeatures((prev) =>
          prev
            ? prev.map((f) => (f.feature === updated.feature ? updated : f))
            : prev,
        );
        // Keep this session's composer in sync (feature-gated flags are
        // hidden from the Flags popover while their gate is closed).
        refreshEnabledFeatures();
      } catch (error) {
        console.error(`Failed to update feature gate ${feature}:`, error);
        setSaveError(error instanceof Error ? error.message : 'Failed to save');
      } finally {
        setSavingFeature(null);
      }
    },
    [refreshEnabledFeatures],
  );

  const handleAccessModeChange = useCallback(
    (gate: FeatureGate, mode: 'all' | 'selected') => {
      const allowed_users =
        mode === 'all' ? null : (lastSelectionRef.current[gate.feature] ?? []);
      saveGate(gate.feature, { enabled: gate.enabled, allowed_users });
    },
    [saveGate],
  );

  const handleUserToggle = useCallback(
    (gate: FeatureGate, email: string, checked: boolean) => {
      const current = gate.allowed_users ?? [];
      const next = checked
        ? [...current, email]
        : current.filter((e) => e !== email);
      saveGate(gate.feature, { enabled: gate.enabled, allowed_users: next });
    },
    [saveGate],
  );

  const renderAccessEditor = (gate: FeatureGate) => {
    const saving = savingFeature === gate.feature;
    const selectedMode = gate.allowed_users !== null;
    const allowed = gate.allowed_users ?? [];
    // Emails granted access but missing from the roster (e.g. edited by
    // hand or the user row was deleted) stay listed so they can be revoked.
    const extraEmails = allowed.filter(
      (email) => !(users ?? []).some((u) => u.email.toLowerCase() === email),
    );
    return (
      <div className="feature-gate-access">
        <div className="feature-gate-access-title">Who has access</div>
        <label className="feature-gate-radio">
          <input
            type="radio"
            name={`feature-access-${gate.feature}`}
            checked={!selectedMode}
            disabled={saving}
            onChange={() => handleAccessModeChange(gate, 'all')}
          />
          <span>All users</span>
        </label>
        <label className="feature-gate-radio">
          <input
            type="radio"
            name={`feature-access-${gate.feature}`}
            checked={selectedMode}
            disabled={saving}
            onChange={() => handleAccessModeChange(gate, 'selected')}
          />
          <span>Only specific users</span>
        </label>
        {selectedMode && (
          <div className="feature-gate-user-list">
            {usersError && (
              <div className="svc-cred-load-error">{usersError}</div>
            )}
            {users === null && !usersError && (
              <div className="settings-loading">Loading users...</div>
            )}
            {(users ?? []).map((u) => (
              <label key={u.id} className="feature-gate-user-row">
                <input
                  type="checkbox"
                  checked={allowed.includes(u.email.toLowerCase())}
                  disabled={saving}
                  onChange={(e) =>
                    handleUserToggle(
                      gate,
                      u.email.toLowerCase(),
                      e.target.checked,
                    )
                  }
                />
                <span>{u.name || u.email}</span>
                {u.name && (
                  <span className="feature-gate-user-email">{u.email}</span>
                )}
              </label>
            ))}
            {extraEmails.map((email) => (
              <label key={email} className="feature-gate-user-row">
                <input
                  type="checkbox"
                  checked
                  disabled={saving}
                  onChange={() => handleUserToggle(gate, email, false)}
                />
                <span>{email}</span>
                <span className="feature-gate-user-email">
                  (no matching user)
                </span>
              </label>
            ))}
            {allowed.length === 0 && (
              <div className="feature-gate-empty-note">
                No users selected — nobody has access until you select
                someone.
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="settings-section">
      <h3>Features</h3>
      <p className="settings-description">
        Server-wide switches for optional features. Every feature is off by
        default. Admin only.
      </p>
      {loadError ? (
        <div className="svc-cred-load-error">{loadError}</div>
      ) : features === null ? (
        <div className="settings-loading">Loading...</div>
      ) : (
        features.map((gate) => (
          <div key={gate.feature} className="svc-cred-card">
            <div className="svc-cred-card-header">
              <h4>{gate.label}</h4>
              <span
                className={`svc-cred-badge ${gate.enabled ? 'configured' : 'unconfigured'}`}
              >
                {gate.enabled
                  ? gate.allowed_users === null
                    ? 'Enabled'
                    : `Enabled for ${gate.allowed_users.length} user${gate.allowed_users.length === 1 ? '' : 's'}`
                  : 'Disabled'}
              </span>
            </div>
            <p className="settings-description">{gate.description}</p>
            {/* A feature the server cannot run yet (e.g. voice input with
                no Gemini Vertex model) cannot be turned on; turning an
                already-enabled gate off is always allowed. */}
            {!gate.available && gate.unavailable_reason && (
              <p className="feature-gate-unavailable" role="note">
                {gate.unavailable_reason}
              </p>
            )}
            <label className="svc-cred-toggle">
              <input
                type="checkbox"
                className="svc-cred-toggle-input"
                checked={gate.enabled}
                disabled={
                  savingFeature === gate.feature
                  || (!gate.available && !gate.enabled)
                }
                onChange={(e) =>
                  saveGate(gate.feature, { enabled: e.target.checked })
                }
              />
              <span className="svc-cred-toggle-track" aria-hidden="true">
                <span className="svc-cred-toggle-knob" />
              </span>
              <span className="svc-cred-toggle-text">
                {savingFeature === gate.feature
                  ? 'Saving...'
                  : `Enable ${gate.label.toLowerCase()}`}
              </span>
            </label>
            {gate.supports_user_access && gate.enabled && renderAccessEditor(gate)}
          </div>
        ))
      )}
      {saveError && <div className="svc-cred-load-error">{saveError}</div>}
    </div>
  );
}
