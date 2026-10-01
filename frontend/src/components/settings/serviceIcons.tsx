import type { ReactElement } from 'react';
import { Plug, CreditCard, Mail, Wifi, Laptop, Coins } from 'lucide-react';

/**
 * Brand icons for the Data Connections add picker (keyed by the row's
 * `service` id from GET /connectors) and the admin Service Credentials
 * section (keyed by the credential store's service id, e.g. `google_oauth`
 * or `smtp`). Purely cosmetic: unknown services (e.g. new plugins) fall
 * back to a generic plug icon, so both stay fully data-driven -- an entry
 * here is optional polish.
 *
 * Brand marks are inline SVG path data (simple-icons, CC0) in each
 * brand's colors so they stay recognizable at tile size.
 */

function GoogleIcon() {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
      <path fill="#FBBC05" d="M5.84 14.1c-.22-.66-.35-1.36-.35-2.1s.13-1.44.35-2.1V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.62z" />
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
    </svg>
  );
}

function SlackIcon() {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
      <path fill="#E01E5A" d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313z" />
      <path fill="#36C5F0" d="M8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312z" />
      <path fill="#2EB67D" d="M18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312z" />
      <path fill="#ECB22E" d="M15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z" />
    </svg>
  );
}

function TelegramIcon() {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
      <path fill="#26A5E4" d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z" />
    </svg>
  );
}

function XIcon() {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true" className="service-icon-mono">
      <path fill="currentColor" d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z" />
    </svg>
  );
}

function GithubIcon() {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true" className="service-icon-mono">
      <path fill="currentColor" d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" />
    </svg>
  );
}

function AirtableIcon() {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
      <path fill="#FCB400" d="M11.992 1.966c-.434 0-.87.086-1.28.257L1.779 5.917c-.503.208-.49.908.012 1.116l8.978 3.558a3.266 3.266 0 0 0 2.454 0l8.978-3.558c.503-.196.503-.908.012-1.116l-8.937-3.694a3.29 3.29 0 0 0-1.284-.257z" />
      <path fill="#18BFFF" d="M13.07 12.533v8.75a.62.62 0 0 0 .85.575l10.01-3.877a.614.614 0 0 0 .38-.564v-8.75a.62.62 0 0 0-.85-.575l-10.01 3.877a.612.612 0 0 0-.38.564z" />
      <path fill="#F82B60" d="M.618 8.087a.62.62 0 0 0-.618.62v8.75c0 .245.148.47.38.564l10.01 3.877a.62.62 0 0 0 .85-.576v-8.75a.612.612 0 0 0-.38-.563L.84 8.132a.589.589 0 0 0-.222-.045z" />
    </svg>
  );
}

function TwilioIcon() {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
      <path fill="#F22F46" d="M12 0C5.383 0 0 5.383 0 12s5.383 12 12 12 12-5.383 12-12S18.617 0 12 0zm0 20.8c-4.86 0-8.8-3.94-8.8-8.8S7.14 3.2 12 3.2s8.8 3.94 8.8 8.8-3.94 8.8-8.8 8.8zm2.24-13.04a2.496 2.496 0 1 1 0 4.992 2.496 2.496 0 0 1 0-4.992zm-4.48 0a2.496 2.496 0 1 1 0 4.992 2.496 2.496 0 0 1 0-4.992zm0 4.48a2.496 2.496 0 1 1 0 4.992 2.496 2.496 0 0 1 0-4.992zm4.48 0a2.496 2.496 0 1 1 0 4.992 2.496 2.496 0 0 1 0-4.992z" />
    </svg>
  );
}

function MicrosoftIcon() {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
      <path fill="#F25022" d="M1 1h10.5v10.5H1z" />
      <path fill="#7FBA00" d="M12.5 1H23v10.5H12.5z" />
      <path fill="#00A4EF" d="M1 12.5h10.5V23H1z" />
      <path fill="#FFB900" d="M12.5 12.5H23V23H12.5z" />
    </svg>
  );
}

const SERVICE_ICONS: Record<string, () => ReactElement> = {
  google_services: GoogleIcon,
  google_oauth: GoogleIcon,
  google_admin: GoogleIcon,
  m365: MicrosoftIcon,
  smtp: () => <Mail size={28} className="service-icon-mono" aria-hidden="true" />,
  unifi: () => <Wifi size={28} className="service-icon-mono" aria-hidden="true" />,
  iru: () => <Laptop size={28} className="service-icon-mono" aria-hidden="true" />,
  coingecko: () => <Coins size={28} className="service-icon-mono" aria-hidden="true" />,
  slack: SlackIcon,
  telegram: TelegramIcon,
  twitter: XIcon,
  github: GithubIcon,
  airtable: AirtableIcon,
  twilio: TwilioIcon,
  ramp: () => <CreditCard size={28} className="service-icon-mono" aria-hidden="true" />,
};

export function ServiceIcon({ service }: { service: string }) {
  const Icon = SERVICE_ICONS[service];
  if (Icon) return <Icon />;
  return <Plug size={28} className="service-icon-mono" aria-hidden="true" />;
}
