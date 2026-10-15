/**
 * TableFooter -- the line under the table viewer's grid:
 * "Last written by <who> · Updated <time> · <N> rows (<M> matching) · Schema v<K>".
 *
 * "Last written by" follows `last_write_source`, which the server sends to
 * the owner only (null for everyone else, and then the clause is left out):
 * - `conversation:<id>` -> a link to the conversation (title and project
 *   from `last_write_conversation`), or plain "a conversation" when gone;
 * - `ui:<user id>` -> "you" when it is the owner (the only viewer who sees a
 *   source), else that person's name (`last_write_user`), or "a deleted user";
 * - `action_request:<id>` -> "an approved card" (with the proposer's name
 *   when `last_write_user` names a share recipient);
 * - `revert:<change id>` -> "a restore";
 * - anything else -> the clause is left out.
 */

import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { TableDetail, TableUserRef } from '../../api/types';
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters';

const UI_SOURCE_RE = /^ui:(\d+)$/;

function personName(user: TableUserRef): string {
  return user.name || user.email || 'a deleted user';
}

function conversationPath(id: string, projectId: string | null): string {
  const encoded = encodeURIComponent(id);
  return projectId ? `/projects/${encodeURIComponent(projectId)}/${encoded}` : `/chats/${encoded}`;
}

/** The "Last written by" subject, or null to leave the clause out. */
export function tableLastWriter(table: TableDetail): ReactNode {
  const source = table.last_write_source;
  if (!source) return null;
  const user = table.last_write_user;
  const ui = UI_SOURCE_RE.exec(source);
  if (ui) {
    if (Number(ui[1]) === table.owner_id) return 'you';
    return user ? personName(user) : 'a deleted user';
  }
  if (source.startsWith('conversation:')) {
    const conversation = table.last_write_conversation;
    if (!conversation) return 'a conversation';
    return (
      <Link to={conversationPath(conversation.id, conversation.project_id)} className="table-footer-link">
        {conversation.title || 'a conversation'}
      </Link>
    );
  }
  if (source.startsWith('action_request:')) {
    return user && user.id !== table.owner_id
      ? `an approved card from ${personName(user)}`
      : 'an approved card';
  }
  if (source.startsWith('revert:')) return 'a restore';
  return null;
}

function rowsLabel(count: number): string {
  return `${count} ${count === 1 ? 'row' : 'rows'}`;
}

export interface TableFooterProps {
  table: TableDetail;
  /** Rows matching the active filter; null / undefined = no filter applied. */
  matchingCount?: number | null;
}

export function TableFooter({ table, matchingCount }: TableFooterProps) {
  const writer = tableLastWriter(table);
  const updated = parseUTCTimestamp(table.updated_at);
  const parts: ReactNode[] = [];
  if (writer !== null) {
    parts.push(<span key="writer" className="table-footer-writer">Last written by {writer}</span>);
  }
  parts.push(
    <span key="updated" className="table-footer-updated" title={updated.toLocaleString()}>
      Updated {formatRelativeTimestamp(table.updated_at)}
    </span>,
  );
  parts.push(
    <span key="rows" className="table-footer-rows">
      {rowsLabel(table.row_count)}
      {matchingCount !== null && matchingCount !== undefined ? ` (${matchingCount} matching)` : ''}
    </span>,
  );
  parts.push(
    <span key="schema" className="table-footer-schema">
      Schema v{table.schema_version}
    </span>,
  );
  return (
    <footer className="table-footer">
      {parts.map((part, index) => (
        <span key={index} className="table-footer-part">
          {index > 0 && <span className="table-footer-sep" aria-hidden="true"> · </span>}
          {part}
        </span>
      ))}
    </footer>
  );
}
