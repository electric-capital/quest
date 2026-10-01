import { useState } from 'react';
import { changePassword } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import './SignInSettings.css';

const MIN_PASSWORD_LENGTH = 8;

/**
 * Settings > Password (email/password sign-in deployments only): change the
 * signed-in user's password. Every other session is signed out; this one
 * gets a fresh cookie from the server.
 */
export function PasswordSection() {
  const { userEmail, hasPassword, setHasPassword, isImpersonating } = useAuth();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaved(false);
    if (next.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (next !== confirm) {
      setError('The new passwords do not match.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await changePassword(current, next);
      setCurrent('');
      setNext('');
      setConfirm('');
      setSaved(true);
      setHasPassword(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not change the password.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-section">
      <h3>Password</h3>
      <p className="settings-description">
        {hasPassword
          ? 'Change the password you sign in with. Your other signed-in devices will be signed out.'
          : 'Your account has no password yet. Set one to be able to sign in with your email and password.'}
      </p>
      {isImpersonating ? (
        <p className="signin-warning">Passwords cannot be changed while impersonating.</p>
      ) : (
        <form className="signin-form" onSubmit={handleSubmit}>
          {/* Lets password managers attach the new password to the account. */}
          <input type="email" autoComplete="username" value={userEmail ?? ''} hidden readOnly />
          {hasPassword && (
            <input
              type="password"
              className="signin-input"
              placeholder="Current password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              required
            />
          )}
          <input
            type="password"
            className="signin-input"
            placeholder={`New password (at least ${MIN_PASSWORD_LENGTH} characters)`}
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            required
          />
          <input
            type="password"
            className="signin-input"
            placeholder="Repeat the new password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
          <button type="submit" className="signin-button" disabled={busy}>
            {busy ? 'Saving...' : hasPassword ? 'Change password' : 'Set password'}
          </button>
          {error && <p className="signin-error">{error}</p>}
          {saved && <p className="signin-success">Password saved.</p>}
        </form>
      )}
    </div>
  );
}
