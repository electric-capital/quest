import { useEffect, useState } from 'react';
import './SignInScreen.css';
import { useAppConfig } from '../contexts/AppConfigContext';
import { fetchPasswordLinkInfo, setPasswordWithLink } from '../api/client';
import type { PasswordLinkInfo } from '../api/types';

const MIN_PASSWORD_LENGTH = 8;

// The one-time token rides in the URL fragment (#token=...), which the
// browser never sends to the server, so it stays out of access logs.
function readToken(): string {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  return params.get('token') ?? '';
}

/**
 * Landing page of set-password links (/set-password#token=...): admin
 * invites, self-service sign-up and password resets. Sets the password,
 * creating the account when needed, and signs the user in.
 */
export function SetPasswordScreen() {
  const { appName } = useAppConfig();
  const [token] = useState(readToken);
  const [info, setInfo] = useState<PasswordLinkInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      setLoadError('This link is incomplete. Open the full link from your email or administrator.');
      return;
    }
    fetchPasswordLinkInfo(token)
      .then((result) => {
        setInfo(result);
        setName(result.name);
      })
      .catch((err) => {
        setLoadError(err instanceof Error ? err.message : 'This link is invalid or has expired.');
      });
  }, [token]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (password !== confirm) {
      setError('The passwords do not match.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await setPasswordWithLink(token, password, name.trim());
      // Drop the spent token from the address bar and history.
      window.location.replace('/');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not set the password.');
      setBusy(false);
    }
  };

  const newAccount = info !== null && !info.account_exists;

  return (
    <div className="sign-in-screen">
      <div className="sign-in-card">
        <h1 className="sign-in-title">{appName}</h1>
        <p className="sign-in-subtitle">
          {info === null ? 'Set your password' : newAccount ? 'Create your account' : 'Choose a new password'}
        </p>
        {loadError ? (
          <>
            <p className="sign-in-error">{loadError}</p>
            <a className="sign-in-link-button" href="/">Go to the sign-in page</a>
          </>
        ) : info === null ? (
          <p className="sign-in-loading">Loading...</p>
        ) : (
          <form className="password-sign-in" onSubmit={handleSubmit}>
            {error && <p className="sign-in-error">{error}</p>}
            <input
              type="email"
              className="dev-login-input"
              value={info.email}
              autoComplete="username"
              readOnly
            />
            {newAccount && (
              <input
                type="text"
                className="dev-login-input"
                placeholder="Your name"
                autoComplete="name"
                value={name}
                maxLength={255}
                onChange={(e) => setName(e.target.value)}
              />
            )}
            <input
              type="password"
              className="dev-login-input"
              placeholder={`Password (at least ${MIN_PASSWORD_LENGTH} characters)`}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoFocus
            />
            <input
              type="password"
              className="dev-login-input"
              placeholder="Repeat the password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
            />
            <button type="submit" className="sign-in-button password-submit" disabled={busy}>
              {busy ? 'Saving...' : newAccount ? 'Create account' : 'Set password'}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
