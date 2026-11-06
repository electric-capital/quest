/**
 * SetupScreen -- first-run setup of an unconfigured deployment.
 *
 * Rendered by <App> in place of the sign-in screen while GET /app/api/config
 * reports `setup_required` (no admin and no way to sign in yet; see
 * auth/first_run.py). The operator unlocks it with the encryption password
 * they entered when starting the server -- the one thing that proves they
 * are the operator -- then walks through the questions the command line
 * used to ask: sign-in method, admins and who may sign in, the public URL,
 * the admin password or Google OAuth client, and (optionally) Vertex AI
 * credentials. Everything is submitted at once at the end
 * (POST /app/api/setup/complete); a validation error names its field and
 * the flow jumps back to that step.
 *
 * Under password sign-in the response signs the first admin in, so the
 * reload lands in the app. Under Google sign-in it lands on the Google
 * sign-in screen.
 */
import { useCallback, useMemo, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { completeSetup, unlockSetup } from '../api/client';
import { ApiClientError } from '../api/request';
import type { SetupDefaults } from '../api/types';
import { useAppConfig } from '../contexts/AppConfigContext';
import './SetupScreen.css';

const MIN_PASSWORD_LENGTH = 8;

// Leaves the "setup complete" notice readable for a moment before the
// reload. A module-level hook so tests can stub it.
export const reloadHooks = {
  reloadAfterSetup: () => {
    window.setTimeout(() => window.location.replace('/'), 1200);
  },
};

function reloadAfterSetup() {
  reloadHooks.reloadAfterSetup();
}

type LoginMethod = 'password' | 'google';

// Steps after the unlock, in order.
const STEPS = ['method', 'access', 'url', 'credentials', 'models', 'review'] as const;
type Step = (typeof STEPS)[number];

// Which step a server-side validation error (`details.field`) belongs to.
const FIELD_STEPS: Record<string, Step> = {
  login_method: 'method',
  admin_emails: 'access',
  allowed_login_domain: 'access',
  allowed_login_emails: 'access',
  app_base_url: 'url',
  admin_password: 'credentials',
  google_client_id: 'credentials',
  google_client_secret: 'credentials',
  google_project_id: 'credentials',
  vertex_service_account: 'models',
  vertex_project_id: 'models',
};

interface Answers {
  loginMethod: LoginMethod;
  adminEmail: string;
  otherAdmins: string;
  domain: string;
  extraEmails: string;
  baseUrl: string;
  password: string;
  passwordRepeat: string;
  googleClientId: string;
  googleClientSecret: string;
  googleProjectId: string;
  vertexKeyText: string;
  vertexProjectId: string;
}

function splitEmails(raw: string): string[] {
  return raw.split(/[\s,;]+/).map((e) => e.trim()).filter(Boolean);
}

function domainOf(email: string): string {
  const at = email.lastIndexOf('@');
  return at === -1 ? '' : email.slice(at + 1).toLowerCase();
}

function errorInfo(err: unknown): { message: string; field?: string } {
  if (err instanceof ApiClientError) {
    const field = err.details?.field;
    return { message: err.message, field: typeof field === 'string' ? field : undefined };
  }
  if (err instanceof Error && err.message) return { message: err.message };
  return { message: 'Something went wrong. Try again.' };
}

function initialAnswers(defaults: SetupDefaults): Answers {
  const [adminEmail = '', ...others] = defaults.admin_emails;
  return {
    loginMethod: defaults.login_method,
    adminEmail,
    otherAdmins: others.join(', '),
    domain: defaults.allowed_login_domain,
    extraEmails: defaults.allowed_login_emails.join(', '),
    baseUrl: defaults.app_base_url,
    password: '',
    passwordRepeat: '',
    googleClientId: defaults.google_client_id,
    googleClientSecret: '',
    googleProjectId: defaults.google_project_id,
    vertexKeyText: '',
    vertexProjectId: defaults.vertex_project_id,
  };
}

function Field({
  label, hint, invalid, children, optional,
}: {
  label: string;
  hint?: React.ReactNode;
  invalid?: boolean;
  optional?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className={'setup-field' + (invalid ? ' invalid' : '')}>
      <span className="setup-label">
        {label}
        {optional && <span className="setup-muted"> (optional)</span>}
      </span>
      {children}
      {hint && <span className="setup-hint">{hint}</span>}
    </label>
  );
}

export function SetupScreen() {
  const { appName } = useAppConfig();
  const [token, setToken] = useState<string | null>(null);
  const [defaults, setDefaults] = useState<SetupDefaults | null>(null);
  const [encryptionPassword, setEncryptionPassword] = useState('');
  const [step, setStep] = useState<Step>('method');
  const [answers, setAnswers] = useState<Answers | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorField, setErrorField] = useState<string | null>(null);
  const [done, setDone] = useState<LoginMethod | null>(null);

  const set = useCallback(<K extends keyof Answers>(key: K, value: Answers[K]) => {
    setAnswers((current) => (current ? { ...current, [key]: value } : current));
    setError(null);
    setErrorField(null);
  }, []);

  const handleUnlock = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await unlockSetup(encryptionPassword);
      setToken(result.token);
      setDefaults(result.defaults);
      setAnswers(initialAnswers(result.defaults));
      setEncryptionPassword('');
    } catch (err) {
      setError(errorInfo(err).message);
    } finally {
      setBusy(false);
    }
  };

  const stepIndex = STEPS.indexOf(step);

  // The domain suggested from the admin's address, unless it is a public
  // mailbox provider (allowing gmail.com would admit every Gmail user).
  const suggestedDomain = useMemo(() => {
    if (!answers || !defaults) return '';
    const domain = domainOf(answers.adminEmail.trim());
    return defaults.public_email_domains.includes(domain) ? '' : domain;
  }, [answers, defaults]);

  // Client-side checks that stop "Next" on the current step; the server
  // re-validates everything on completion.
  const stepProblem = useMemo((): string | null => {
    if (!answers || !defaults) return null;
    switch (step) {
      case 'access':
        if (!answers.adminEmail.trim().includes('@')) return 'Enter your email address.';
        return null;
      case 'credentials':
        if (answers.loginMethod === 'password') {
          const existing = defaults.admin_emails[0] === answers.adminEmail.trim().toLowerCase();
          if (!answers.password && existing && defaults.login_method_configured) return null;
          if (answers.password.length < MIN_PASSWORD_LENGTH) {
            return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
          }
          if (answers.password !== answers.passwordRepeat) return 'The passwords do not match.';
          return null;
        }
        if (!answers.googleClientId.trim() && !defaults.google_oauth_configured) {
          return 'Enter the OAuth client id and secret.';
        }
        return null;
      case 'models':
        if (answers.vertexKeyText.trim()) {
          try {
            const parsed = JSON.parse(answers.vertexKeyText);
            if (!parsed || typeof parsed !== 'object' || parsed.type !== 'service_account') {
              return 'That is not a service-account key JSON.';
            }
          } catch {
            return 'The key is not valid JSON.';
          }
        }
        return null;
      default:
        return null;
    }
  }, [answers, defaults, step]);

  const next = () => {
    if (stepProblem) {
      setError(stepProblem);
      return;
    }
    setError(null);
    setStep(STEPS[Math.min(stepIndex + 1, STEPS.length - 1)]);
  };
  const back = () => {
    setError(null);
    setStep(STEPS[Math.max(stepIndex - 1, 0)]);
  };

  const handleKeyFile = async (file: File | undefined) => {
    if (!file) return;
    set('vertexKeyText', await file.text());
  };

  const handleComplete = async () => {
    if (!answers || !token) return;
    setBusy(true);
    setError(null);
    setErrorField(null);
    let vertexKey: Record<string, unknown> | null = null;
    if (answers.vertexKeyText.trim()) {
      try {
        vertexKey = JSON.parse(answers.vertexKeyText);
      } catch {
        setError('The service-account key is not valid JSON.');
        setErrorField('vertex_service_account');
        setStep('models');
        setBusy(false);
        return;
      }
    }
    try {
      const result = await completeSetup({
        token,
        login_method: answers.loginMethod,
        admin_emails: [answers.adminEmail.trim(), ...splitEmails(answers.otherAdmins)],
        allowed_login_domain: answers.domain.trim(),
        allowed_login_emails: splitEmails(answers.extraEmails),
        app_base_url: answers.baseUrl.trim(),
        admin_password: answers.loginMethod === 'password' ? answers.password : undefined,
        google_client_id: answers.loginMethod === 'google' ? answers.googleClientId.trim() : undefined,
        google_client_secret: answers.loginMethod === 'google' ? answers.googleClientSecret : undefined,
        google_project_id: answers.loginMethod === 'google' ? answers.googleProjectId.trim() : undefined,
        vertex_service_account: vertexKey,
        vertex_project_id: answers.vertexProjectId.trim(),
      });
      setDone(result.login_method);
      // Password sign-in: the response set the session cookie, so the
      // reload lands in the app. Google: it lands on the sign-in screen.
      reloadAfterSetup();
    } catch (err) {
      const info = errorInfo(err);
      setError(info.message);
      setErrorField(info.field ?? null);
      if (info.field && FIELD_STEPS[info.field]) setStep(FIELD_STEPS[info.field]);
      if (err instanceof ApiClientError && err.errorCode === 'setup_locked') {
        setToken(null);
      }
      setBusy(false);
    }
  };

  const invalid = (field: string) => errorField === field;

  const redirectUris = (answers?.baseUrl.trim() || 'https://<your-server>').replace(/\/+$/, '');

  let body: React.ReactNode;
  if (done) {
    body = (
      <>
        <h1 className="setup-title">Setup complete</h1>
        <p className="setup-notice" role="status">
          {done === 'password'
            ? 'You are signed in. Taking you to the app…'
            : 'Taking you to the sign-in screen. Sign in with Google to continue.'}
        </p>
      </>
    );
  } else if (!token || !answers || !defaults) {
    body = (
      <form className="setup-form" onSubmit={handleUnlock}>
        <h1 className="setup-title">Set up this deployment</h1>
        <p className="setup-text">
          This server has no administrator yet. To prove you are its operator, enter the
          encryption password you chose when starting it. Nothing else is needed on the command line.
        </p>
        {error && <p className="setup-error" role="alert">{error}</p>}
        <Field label="Encryption password">
          <input
            type="password"
            className="setup-input"
            autoComplete="off"
            value={encryptionPassword}
            onChange={(e) => setEncryptionPassword(e.target.value)}
            disabled={busy}
            autoFocus
            required
            aria-label="Encryption password"
          />
        </Field>
        <div className="setup-actions">
          <span />
          <button type="submit" className="setup-button" disabled={busy || !encryptionPassword.trim()}>
            {busy ? <><LoaderCircle size={14} className="setup-spin" /> Checking…</> : 'Unlock setup'}
          </button>
        </div>
      </form>
    );
  } else {
    const passwordMode = answers.loginMethod === 'password';
    let stepBody: React.ReactNode;
    switch (step) {
      case 'method':
        stepBody = (
          <>
            <h1 className="setup-title">How will people sign in?</h1>
            <div className="setup-choices" role="radiogroup" aria-label="Sign-in method">
              <button
                type="button" role="radio" aria-checked={passwordMode}
                className={'setup-choice' + (passwordMode ? ' selected' : '')}
                onClick={() => set('loginMethod', 'password')}
              >
                <span>
                  <span className="setup-choice-title">Email and password</span>
                  <span className="setup-choice-text">
                    Accounts managed in Quest; nothing to set up outside it. You invite users from
                    Settings › Sign-in. The easiest way to get going; you can switch to Google
                    sign-in later without losing accounts.
                  </span>
                </span>
              </button>
              <button
                type="button" role="radio" aria-checked={!passwordMode}
                className={'setup-choice' + (!passwordMode ? ' selected' : '')}
                onClick={() => set('loginMethod', 'google')}
              >
                <span>
                  <span className="setup-choice-title">Google sign-in</span>
                  <span className="setup-choice-text">
                    Users sign in with Google accounts on your domain. Needs an OAuth client from
                    Google Cloud Console; you enter it a few steps from now.
                  </span>
                </span>
              </button>
            </div>
          </>
        );
        break;
      case 'access':
        stepBody = (
          <>
            <h1 className="setup-title">Administrators and access</h1>
            <Field
              label="Your email address"
              invalid={invalid('admin_emails')}
              hint={passwordMode
                ? 'The first admin account. You choose its password in a moment.'
                : 'The first admin. Sign in with the Google account of this address.'}
            >
              <input
                type="email" className="setup-input" autoComplete="email" required autoFocus
                value={answers.adminEmail}
                onChange={(e) => set('adminEmail', e.target.value)}
                aria-label="Your email address"
              />
            </Field>
            <Field label="Other administrators" optional invalid={invalid('admin_emails')}
                   hint="Comma-separated. Admins manage credentials, models and users from Settings.">
              <input
                type="text" className="setup-input" autoComplete="off"
                value={answers.otherAdmins}
                onChange={(e) => set('otherAdmins', e.target.value)}
                placeholder="ops@example.com, eng@example.com"
                aria-label="Other administrators"
              />
            </Field>
            <Field
              label="Email domain allowed to sign in" optional invalid={invalid('allowed_login_domain')}
              hint={
                <>
                  Everyone with an address on this domain may {passwordMode ? 'have an account' : 'sign in'}.
                  Public mailbox domains such as gmail.com are refused: list those addresses individually
                  instead.{suggestedDomain && !answers.domain && (
                    <> Suggested: <button type="button" className="setup-link" onClick={() => set('domain', suggestedDomain)}>{suggestedDomain}</button></>
                  )}
                </>
              }
            >
              <input
                type="text" className="setup-input" autoComplete="off"
                value={answers.domain}
                onChange={(e) => set('domain', e.target.value)}
                placeholder="example.com"
                aria-label="Email domain allowed to sign in"
              />
            </Field>
            <Field label="Other email addresses allowed to sign in" optional invalid={invalid('allowed_login_emails')}
                   hint="Comma-separated. Admins outside the domain are added automatically.">
              <input
                type="text" className="setup-input" autoComplete="off"
                value={answers.extraEmails}
                onChange={(e) => set('extraEmails', e.target.value)}
                placeholder="friend@gmail.com"
                aria-label="Other email addresses allowed to sign in"
              />
            </Field>
          </>
        );
        break;
      case 'url':
        stepBody = (
          <>
            <h1 className="setup-title">Public address</h1>
            <p className="setup-text">
              The URL people browse to. It is the base for OAuth callback URLs and for links the
              app generates to itself. Use the HTTPS address of your reverse proxy, tunnel or
              Tailscale name; leave it empty to use whatever address each request comes in on.
            </p>
            <Field label="Public URL" optional invalid={invalid('app_base_url')}>
              <input
                type="text" className="setup-input" autoComplete="off" autoFocus
                value={answers.baseUrl}
                onChange={(e) => set('baseUrl', e.target.value)}
                placeholder="https://quest.example.com"
                aria-label="Public URL"
              />
            </Field>
          </>
        );
        break;
      case 'credentials':
        stepBody = passwordMode ? (
          <>
            <h1 className="setup-title">Your password</h1>
            <p className="setup-text">
              For <strong>{answers.adminEmail.trim()}</strong>. Other admins and users get invite links
              from Settings › Sign-in once you are in.
              {defaults.login_method_configured && defaults.admin_emails[0] === answers.adminEmail.trim().toLowerCase()
                && ' Leave both fields empty to keep the password this account already has.'}
            </p>
            <Field label="Password" invalid={invalid('admin_password')}
                   hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}>
              <input
                type="password" className="setup-input" autoComplete="new-password" autoFocus
                value={answers.password}
                onChange={(e) => set('password', e.target.value)}
                aria-label="Password"
              />
            </Field>
            <Field label="Repeat the password" invalid={invalid('admin_password')}>
              <input
                type="password" className="setup-input" autoComplete="new-password"
                value={answers.passwordRepeat}
                onChange={(e) => set('passwordRepeat', e.target.value)}
                aria-label="Repeat the password"
              />
            </Field>
          </>
        ) : (
          <>
            <h1 className="setup-title">Google OAuth client</h1>
            <p className="setup-text">
              In Google Cloud Console open APIs &amp; Services › Credentials › Create credentials ›
              OAuth client ID (type <em>Web application</em>) and register both redirect URIs:
            </p>
            <ul className="setup-uris">
              {defaults.redirect_paths.map((path) => (
                <li key={path}><code className="setup-uri">{redirectUris}{path}</code></li>
              ))}
            </ul>
            <Field label="Client id" invalid={invalid('google_client_id')}
                   hint={defaults.google_oauth_configured ? 'A client is already stored; leave this empty to keep it.' : undefined}>
              <input
                type="text" className="setup-input" autoComplete="off" autoFocus spellCheck={false}
                value={answers.googleClientId}
                onChange={(e) => set('googleClientId', e.target.value)}
                placeholder="….apps.googleusercontent.com"
                aria-label="Client id"
              />
            </Field>
            <Field label="Client secret" invalid={invalid('google_client_secret')}
                   hint={defaults.google_oauth_configured ? 'Empty keeps the stored secret.' : undefined}>
              <input
                type="password" className="setup-input" autoComplete="off"
                value={answers.googleClientSecret}
                onChange={(e) => set('googleClientSecret', e.target.value)}
                aria-label="Client secret"
              />
            </Field>
            <Field label="Google Cloud project id of the client" optional invalid={invalid('google_project_id')}>
              <input
                type="text" className="setup-input" autoComplete="off" spellCheck={false}
                value={answers.googleProjectId}
                onChange={(e) => set('googleProjectId', e.target.value)}
                aria-label="Google Cloud project id of the client"
              />
            </Field>
          </>
        );
        break;
      case 'models':
        stepBody = (
          <>
            <h1 className="setup-title">Models</h1>
            <p className="setup-text">
              Claude and Gemini run on Vertex AI: paste a GCP service-account key (Vertex AI User
              role) and the project id. Skip this to connect an API-key provider (Anthropic,
              OpenAI, OpenRouter, …) from the composer right after signing in instead.
              {defaults.llm_configured && ' Models are already configured on this deployment.'}
            </p>
            <Field label="Service-account key JSON" optional invalid={invalid('vertex_service_account')}
                   hint={defaults.vertex_key_present ? 'A key is already stored; a new one replaces it.' : undefined}>
              <textarea
                className="setup-input" spellCheck={false}
                value={answers.vertexKeyText}
                onChange={(e) => set('vertexKeyText', e.target.value)}
                placeholder='{ "type": "service_account", "project_id": "…", … }'
                aria-label="Service-account key JSON"
              />
              <input
                type="file" accept="application/json,.json"
                onChange={(e) => void handleKeyFile(e.target.files?.[0])}
                aria-label="Service-account key file"
              />
            </Field>
            <Field label="GCP project id for Vertex AI" optional invalid={invalid('vertex_project_id')}
                   hint="Defaults to the key's project.">
              <input
                type="text" className="setup-input" autoComplete="off" spellCheck={false}
                value={answers.vertexProjectId}
                onChange={(e) => set('vertexProjectId', e.target.value)}
                aria-label="GCP project id for Vertex AI"
              />
            </Field>
          </>
        );
        break;
      case 'review': {
        const admins = [answers.adminEmail.trim(), ...splitEmails(answers.otherAdmins)];
        stepBody = (
          <>
            <h1 className="setup-title">Review</h1>
            <dl className="setup-review">
              <dt>Sign-in</dt>
              <dd>{passwordMode ? 'Email and password' : 'Google'}</dd>
              <dt>Admins</dt>
              <dd>{admins.join(', ')}</dd>
              <dt>Allowed domain</dt>
              <dd>{answers.domain.trim() || <span className="setup-muted">none</span>}</dd>
              <dt>Allowed addresses</dt>
              <dd>{splitEmails(answers.extraEmails).join(', ') || <span className="setup-muted">none beyond the admins</span>}</dd>
              <dt>Public URL</dt>
              <dd>{answers.baseUrl.trim() || <span className="setup-muted">request host</span>}</dd>
              {!passwordMode && (
                <>
                  <dt>OAuth client</dt>
                  <dd>{answers.googleClientId.trim() || (defaults.google_oauth_configured ? 'stored client kept' : '')}</dd>
                </>
              )}
              <dt>Vertex AI</dt>
              <dd>
                {answers.vertexKeyText.trim()
                  ? `key + project ${answers.vertexProjectId.trim() || 'from the key'}`
                  : answers.vertexProjectId.trim()
                    ? `project ${answers.vertexProjectId.trim()} (credentials from the environment)`
                    : <span className="setup-muted">skipped; connect a provider after signing in</span>}
              </dd>
            </dl>
            <p className="setup-footnote">
              Writes <code>server_config.json</code> and the encrypted credential store on the server.
              Everything here can be changed later: credentials and sign-in from Settings,
              admins and the public URL in <code>server_config.json</code>.
            </p>
          </>
        );
        break;
      }
    }
    const last = step === 'review';
    body = (
      <form
        className="setup-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (last) void handleComplete();
          else next();
        }}
      >
        <ol className="setup-steps" aria-label={`Step ${stepIndex + 1} of ${STEPS.length}`}>
          {STEPS.map((s, i) => (
            <li key={s} className={'setup-step' + (i < stepIndex ? ' done' : i === stepIndex ? ' current' : '')} />
          ))}
        </ol>
        {stepBody}
        {error && <p className="setup-error" role="alert">{error}</p>}
        <div className="setup-actions">
          {stepIndex > 0
            ? <button type="button" className="setup-button secondary" onClick={back} disabled={busy}>Back</button>
            : <span />}
          <button type="submit" className="setup-button" disabled={busy}>
            {busy
              ? <><LoaderCircle size={14} className="setup-spin" /> Saving…</>
              : last ? 'Finish setup' : 'Next'}
          </button>
        </div>
      </form>
    );
  }

  return (
    <div className="setup-screen">
      <div className="setup-card" role="region" aria-label="First-run setup">
        <p className="setup-brand">{appName} · first-run setup</p>
        {body}
      </div>
    </div>
  );
}
