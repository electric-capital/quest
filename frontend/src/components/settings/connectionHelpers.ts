import { ConnectionApiError } from '../../api/client';
import type { ConnectionRow, ConnectorRow, SchemaFormField } from '../../api/types';

/**
 * Helpers for the multi-connection parts of Data Connections (issue #84):
 * which rows are multi-connection, which fields a connection form shows,
 * and how a failed connection-route call reads to the user.
 */

/** A row rendered as per-connection cards (form kind is always multi). */
export function isMultiConnection(connector: ConnectorRow): boolean {
  return connector.multi_connection === true || connector.kind === 'form';
}

/** The connections of a multi-connection row, in server order (default first). */
export function connectionsOf(connector: ConnectorRow): ConnectionRow[] {
  return connector.connections ?? [];
}

/** True while the user may add another connection to a multi-connection row. */
export function canAddConnection(connector: ConnectorRow): boolean {
  const limit = connector.connection_limit ?? Number.POSITIVE_INFINITY;
  return connectionsOf(connector).length < limit;
}

export const UNNAMED_CONNECTION = '(unnamed connection)';

export function accountLabel(connection: ConnectionRow): string {
  return connection.account || UNNAMED_CONNECTION;
}

/** Max description length (spec 14: 200 characters, single line). */
export const DESCRIPTION_MAX_LENGTH = 200;

/** Collapse whitespace (incl. newlines) and trim; '' means "no description". */
export function normalizeDescription(raw: string): string {
  return raw.replace(/\s+/g, ' ').trim();
}

/** The single secret field an api_key-kind connection form shows. */
export function apiKeyField(connector: ConnectorRow): SchemaFormField {
  return {
    key: 'api_key',
    label: 'API key',
    type: 'secret',
    required: true,
    required_if: null,
    visible_if: null,
    placeholder: connector.key_placeholder || `Paste ${connector.label} API key`,
    options: [],
  };
}

/**
 * The fields of a connection form.
 *
 * Adding: the row's form_fields (form kind) or one API key field.
 *
 * Re-entering an existing connection: the server never returns stored
 * values, but PUT {values} merges the submitted values over the stored ones
 * and an empty secret keeps the stored secret. So the re-entry form shows
 * only the secret fields, with their own required flags -- every other
 * field (the ones that determine the account) stays exactly as stored,
 * untouched and unseen. A secret's required_if / visible_if names a bool
 * field the re-entry form does not show (its stored state is unknown), so
 * such a conditional secret is shown, and optional. A form without secret
 * fields shows every non-bool field, all optional (an unticked box cannot
 * mean "keep as stored", so bool fields are not offered). Blank fields are
 * omitted from the PUT and keep their stored values; the server refuses a
 * change that resolves to another account (account_mismatch). An empty
 * list (an all-bool form) means the connection cannot be re-entered.
 */
export function connectionFormFields(
  connector: ConnectorRow,
  mode: 'add' | 'reconnect',
): SchemaFormField[] {
  if (connector.kind !== 'form') return [apiKeyField(connector)];
  const fields = connector.form_fields ?? [];
  if (mode === 'add') return fields;
  const secrets = fields.filter((field) => field.type === 'secret');
  const shown = secrets.length > 0 ? secrets : fields.filter((field) => field.type !== 'bool');
  return shown.map((field) => ({
    ...field,
    required: secrets.length > 0 && !field.required_if && field.required,
    required_if: null,
    visible_if: null,
  }));
}

/** True when the re-entry form shows only the secret fields. */
export function reconnectsSecretsOnly(connector: ConnectorRow): boolean {
  return connector.kind === 'form' && (connector.form_fields ?? []).some((f) => f.type === 'secret');
}

/**
 * Whether a connection offers Reconnect. For oauth kind only the default
 * connection: the oauth callbacks still write the default row (no
 * per-connection connect URL yet). Otherwise not for a legacy row without
 * an account (the server refuses re-entry with account_mismatch: it has to
 * be deleted and added again) nor for a form whose re-entry would show no
 * field.
 */
export function canReconnect(connector: ConnectorRow, connection: ConnectionRow): boolean {
  if (connector.kind === 'oauth') return connection.is_default;
  if (!connection.account) return false;
  return connectionFormFields(connector, 'reconnect').length > 0;
}

/**
 * Whether the add picker offers the service. Multi-connection rows stay
 * addable below their connection_limit -- except oauth kind, whose callback
 * still overwrites the default row, so it is addable only while it has no
 * connection. Single-connection rows: while not connected.
 */
export function isAddable(connector: ConnectorRow): boolean {
  if (!isMultiConnection(connector)) return !connector.connected;
  if (connector.kind === 'oauth') return connectionsOf(connector).length === 0;
  return canAddConnection(connector);
}

export const GENERIC_ERROR = 'Something went wrong. Please try again.';

/** The user-facing message for a failed connection-route call. */
export function connectionErrorMessage(error: unknown, serviceLabel: string): string {
  if (!(error instanceof ConnectionApiError)) {
    return error instanceof Error && error.message ? error.message : GENERIC_ERROR;
  }
  if (error.status === 0 || error.code === 'network_error') {
    return 'Could not reach the server. Check your network and try again.';
  }
  if (error.status === 429) {
    const seconds = error.retryAfter ?? 60;
    return `Too many attempts; try again in ${seconds} second${seconds === 1 ? '' : 's'}.`;
  }
  switch (error.code) {
    case 'invalid_values':
      return 'Some fields need attention.';
    case 'connection_exists':
      return 'This account is already connected.';
    case 'connection_limit':
      return error.limit
        ? `You can connect at most ${error.limit} ${serviceLabel} accounts.`
        : `You have reached the ${serviceLabel} connection limit.`;
    default:
      return error.message && error.message !== 'Unknown error' ? error.message : GENERIC_ERROR;
  }
}
