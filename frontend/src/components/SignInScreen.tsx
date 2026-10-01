import { useState, useEffect, useCallback } from 'react';
import './SignInScreen.css';
import { useAppConfig } from '../contexts/AppConfigContext';
import { passwordLogin, requestPasswordLink } from '../api/client';

interface DevAccount {
  email: string;
  name: string;
  is_admin: boolean;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

type PasswordView = 'sign-in' | 'request-link' | 'link-sent';

/**
 * Email + password sign-in (login_method "password"). With outgoing email
 * configured (`selfService`) the same screen offers "Forgot password?" and
 * "Create an account", both of which email a one-time set-password link;
 * without it, users get their link from an admin.
 */
function PasswordSignIn({ selfService }: { selfService: boolean }) {
  const [view, setView] = useState<PasswordView>('sign-in');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const switchView = (next: PasswordView) => {
    setView(next);
    setError(null);
  };

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await passwordLogin(email.trim(), password);
      window.location.reload();
    } catch (err) {
      setError(errorMessage(err, 'Sign-in failed.'));
      setBusy(false);
    }
  };

  const handleRequestLink = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await requestPasswordLink(email.trim());
      setNotice(result.message);
      switchView('link-sent');
    } catch (err) {
      setError(errorMessage(err, 'Could not send the link.'));
    } finally {
      setBusy(false);
    }
  };

  if (view === 'link-sent') {
    return (
      <div className="password-sign-in">
        <p className="password-notice">{notice}</p>
        <button type="button" className="sign-in-link-button" onClick={() => switchView('sign-in')}>
          Back to sign in
        </button>
      </div>
    );
  }

  if (view === 'request-link') {
    return (
      <form className="password-sign-in" onSubmit={handleRequestLink}>
        <p className="password-help">
          Enter your email address. We'll send a link to set your password: it
          resets the password of an existing account, or creates your account if
          you don't have one yet.
        </p>
        {error && <p className="sign-in-error">{error}</p>}
        <input
          type="email"
          className="dev-login-input"
          placeholder="Email address"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          autoFocus
        />
        <button type="submit" className="sign-in-button password-submit" disabled={busy}>
          {busy ? 'Sending...' : 'Email me a link'}
        </button>
        <button type="button" className="sign-in-link-button" onClick={() => switchView('sign-in')}>
          Back to sign in
        </button>
      </form>
    );
  }

  return (
    <form className="password-sign-in" onSubmit={handleSignIn}>
      {error && <p className="sign-in-error">{error}</p>}
      <input
        type="email"
        className="dev-login-input"
        placeholder="Email address"
        autoComplete="username"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        required
        autoFocus
      />
      <input
        type="password"
        className="dev-login-input"
        placeholder="Password"
        autoComplete="current-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        required
      />
      <button type="submit" className="sign-in-button password-submit" disabled={busy}>
        {busy ? 'Signing in...' : 'Sign in'}
      </button>
      {selfService ? (
        <div className="password-links">
          <button type="button" className="sign-in-link-button" onClick={() => switchView('request-link')}>
            Forgot password?
          </button>
          <button type="button" className="sign-in-link-button" onClick={() => switchView('request-link')}>
            Create an account
          </button>
        </div>
      ) : (
        <p className="password-help">
          Forgot your password or need an account? Ask an administrator for a sign-in link.
        </p>
      )}
    </form>
  );
}

export function SignInScreen() {
  const { appName, isDevMode, loginRestriction, loginMethod, passwordSelfService } = useAppConfig();
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [googleUnavailable, setGoogleUnavailable] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [devEmail, setDevEmail] = useState('');
  const [devLoginLoading, setDevLoginLoading] = useState(false);
  const [devAccounts, setDevAccounts] = useState<DevAccount[]>([]);

  useEffect(() => {
    // Google sign-in only; wait for the config fetch to say which method.
    if (loginMethod !== 'google') return;
    const fetchLoginUrl = async () => {
      try {
        const response = await fetch('/auth/login-url', { credentials: 'include' });
        if (response.ok) {
          const data = await response.json();
          setAuthUrl(data.auth_url);
        } else {
          // In local mode Google OAuth is typically unconfigured (500 from
          // the missing credentials file) — that's expected, not an error.
          setGoogleUnavailable(true);
        }
      } catch {
        setGoogleUnavailable(true);
      }
    };
    fetchLoginUrl();
  }, [loginMethod]);

  // Local mode: fetch the canned-account roster for one-click login.
  useEffect(() => {
    if (!isDevMode) return;
    const fetchAccounts = async () => {
      try {
        const response = await fetch('/auth/dev-accounts', { credentials: 'include' });
        if (response.ok) {
          const data = await response.json();
          setDevAccounts(data.accounts ?? []);
        }
      } catch {
        // Roster fetch failed; the free-text email form still works.
      }
    };
    fetchAccounts();
  }, [isDevMode]);

  const devLogin = useCallback(async (email: string) => {
    if (!email.trim()) return;
    setDevLoginLoading(true);
    setError(null);
    try {
      const response = await fetch('/auth/dev-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ email: email.trim() }),
      });
      if (response.ok) {
        window.location.reload();
      } else {
        const data = await response.json();
        setError(typeof data.detail === 'string' ? data.detail : 'Login failed');
        setDevLoginLoading(false);
      }
    } catch {
      setError('Failed to connect to server.');
      setDevLoginLoading(false);
    }
  }, []);

  const handleDevLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    await devLogin(devEmail);
  };

  return (
    <div className="sign-in-screen">
      <div className="sign-in-card">
        <h1 className="sign-in-title">{appName}</h1>
        <p className="sign-in-subtitle">AI-powered assistant</p>
        {error && <p className="sign-in-error">{error}</p>}
        {loginMethod === null ? (
          <p className="sign-in-loading">Loading...</p>
        ) : loginMethod === 'password' ? (
          <PasswordSignIn selfService={passwordSelfService} />
        ) : authUrl ? (
          <a href={authUrl} className="sign-in-button">
            Sign in with Google
          </a>
        ) : googleUnavailable ? (
          isDevMode
            ? <p className="sign-in-loading">Google sign-in not configured (fine in local mode)</p>
            : <p className="sign-in-error">Failed to initialize login. Please try again.</p>
        ) : (
          <p className="sign-in-loading">Loading...</p>
        )}
        {isDevMode && (
          <div className="dev-login-section">
            <div className="dev-login-divider">
              <span>{devAccounts.length > 0 ? 'pick an account' : 'or'}</span>
            </div>
            {devAccounts.length > 0 && (
              <div className="dev-accounts-list">
                {devAccounts.map((account) => (
                  <button
                    key={account.email}
                    className="dev-account-button"
                    disabled={devLoginLoading}
                    onClick={() => devLogin(account.email)}
                  >
                    <span className="dev-account-name">
                      {account.name || account.email}
                      {account.is_admin && <span className="dev-account-admin-badge">admin</span>}
                    </span>
                    <span className="dev-account-email">{account.email}</span>
                  </button>
                ))}
              </div>
            )}
            <form onSubmit={handleDevLogin} className="dev-login-form">
              <input
                type="email"
                value={devEmail}
                onChange={(e) => setDevEmail(e.target.value)}
                placeholder={devAccounts.length > 0 ? 'or any other email address' : 'Enter email address'}
                className="dev-login-input"
                required
              />
              <button type="submit" disabled={devLoginLoading} className="dev-login-button">
                {devLoginLoading ? 'Logging in...' : 'Dev Login'}
              </button>
            </form>
            <p className="dev-login-note">Local mode: any email, no Google account needed</p>
          </div>
        )}
        {!isDevMode && (
          <p className="sign-in-note">Access restricted to {loginRestriction}.</p>
        )}
      </div>
    </div>
  );
}
