/**
 * The task thread's activity line: what the ACTIVE run of a Quest Task is
 * doing right now, as one short label, plus whether a model turn is
 * streaming on the run's conversation.
 *
 * The thread is not a ChatPanel: this hook follows the run conversation's
 * live events straight off the persistent socket and never touches
 * webSocketManager, conversationStore or useConversation. It keeps no
 * transcript, only the newest label.
 *
 * Subscription ownership. On a conversation id the hook attaches its event
 * handler first, then always calls `persistentWebSocket.subscribe(id)`: the
 * op is idempotent server side (it only pushes the TTL deadline forward), so
 * even a conversation some other caller already subscribed (the run view's
 * useConversation, which never unsubscribes) gets a fresh `subscribed` ack
 * with `run_active` and `current_seq`. The hook OWNS the subscription only
 * when the id was not in the client's subscribed set before that call; on
 * cleanup (id change, unmount) it detaches its handler and sends
 * `unsubscribe` only for an owned subscription, so anyone else's
 * subscription survives. Limitation: the client keeps a set, not a
 * refcount, so a caller that subscribes the same conversation WHILE this
 * hook owns it loses the subscription at this hook's cleanup. Route changes
 * are safe (React runs the old page's effect cleanups before the new page's
 * effects), and the 3-minute subscribe refresh is the singleton's job. A
 * `subscription_expired` for an owned subscription re-subscribes at once
 * (the ack's catch-up covers the gap).
 *
 * Catch-up. On every `subscribed` ack: `run_active` sets `streaming`; a
 * `catchup` ack's message bodies are applied directly; then, while no label
 * has been derived yet, the last TAIL_WINDOW rows are fetched
 * (`GET /conversations/{id}/tail?after_seq=current_seq - TAIL_WINDOW`), else
 * only the rows after the highest seq seen, when the ack reports newer
 * ones. Each `message_appended` (and a per-conversation `resync`) fetches
 * the tail after the highest seq seen; fetches are coalesced (one in
 * flight, plus one dirty re-run when it lands). A response for a
 * conversation that is no longer current is dropped.
 *
 * Label rules (newest relevant row or event wins):
 * - `tool_use` row: its `intent_message`, else `Running <tool name>` (the
 *   inner `tool_input.tool_name` for the `tool_call` dispatcher; snake_case
 *   turned into words, e.g. "Running web search").
 * - `tool_result` row, and an assistant text row: "Thinking".
 * - a pending `task_question`, or an open `action_request` /
 *   `connect_request` row: "Waiting for you" (a resolved one: "Thinking").
 * - the user's prompt row: "Starting".
 * - `text_delta` event: "Writing the reply".
 * - `sub_agent_tool_use` event: `Sub-agent: <intent_message or tool name>`.
 * - `resume_started` event: "Resuming".
 * - nothing known yet: "Starting". Other rows (stats, errors, nudges,
 *   markers) leave the label alone.
 *
 * "Newest" is arrival order, not fetch order: every event takes a stamp
 * from a local clock, a row takes the stamp of the `message_appended` that
 * announced it (or of the fetch that brought it, when none did), and a
 * label only replaces one with an older stamp. So a tail fetch that lands
 * after a later `text_delta` cannot put an older step back on the line.
 *
 * `streaming`: the ack's `run_active`; `resume_started`, `text_delta` and
 * `sub_agent_tool_use` set it true; `send_message_finished` sets it false
 * and keeps the last label (the thread re-renders the run from
 * `task_changed`). null until the first ack. The hook does not interpret
 * the run's task status: the thread decides what to show for `waiting`.
 */

import { useEffect, useState } from 'react';
import { fetchConversationTail } from '../api/client';
import { persistentWebSocket, type PersistentEvent } from '../services/PersistentWebSocket';

export interface TaskRunActivity {
  /** Short present-tense label of what the run is doing now, e.g. "Starting",
   *  "Thinking", "Writing the reply", a tool call's intent_message
   *  ("Searching for flights") or "Running <tool name>", "Sub-agent: <...>",
   *  "Resuming". Never empty while a conversation id is given. */
  label: string;
  /** Whether a model turn is streaming on the run conversation right now, from the
   *  subscribe ack's `run_active`, `resume_started` and `send_message_finished`;
   *  null until the first ack. */
  streaming: boolean | null;
}

export const TASK_ACTIVITY_STARTING = 'Starting';
export const TASK_ACTIVITY_THINKING = 'Thinking';
export const TASK_ACTIVITY_WRITING = 'Writing the reply';
export const TASK_ACTIVITY_WAITING = 'Waiting for you';
export const TASK_ACTIVITY_RESUMING = 'Resuming';

/** How many rows before the ack's current_seq the first label is read from. */
const TAIL_WINDOW = 4;

type Row = Record<string, unknown>;

interface ActivityState extends TaskRunActivity {
  id: string | null;
}

function initialState(id: string | null): ActivityState {
  return { id, label: TASK_ACTIVITY_STARTING, streaming: null };
}

function str(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

/** `web_search` -> "web search". */
function toolWords(name: string): string {
  return name.replace(/[_\s]+/g, ' ').trim();
}

/** Label of a top-level tool call (a durable `tool_use` row). */
const TASK_LOOP_TOOL_LABELS: Record<string, string> = {
  complete_task_run: 'Writing the result and handoff',
  ask_task_question: 'Asking you a question',
};

export function toolUseLabel(row: Row): string {
  const intent = str(row.intent_message);
  if (intent) return intent;
  let name = str(row.tool_name);
  if (name === 'tool_call') {
    const input = row.tool_input;
    const inner = input && typeof input === 'object' ? str((input as Row).tool_name) : '';
    if (inner) name = inner;
  }
  // The task run's own loop tools read better as steps than as tool names.
  const own = TASK_LOOP_TOOL_LABELS[name];
  if (own) return own;
  return name ? `Running ${toolWords(name)}` : TASK_ACTIVITY_THINKING;
}

/** Label of a sub-agent's tool call (a transient `sub_agent_tool_use`). */
function subAgentLabel(event: PersistentEvent): string {
  const what = str(event.intent_message) || toolWords(str(event.tool_name)) || 'working';
  return `Sub-agent: ${what}`;
}

/** The label a transcript row stands for, or null for a row that says nothing about the step. */
export function rowActivityLabel(row: Row): string | null {
  const type = str(row.type) || 'text';
  switch (type) {
    case 'tool_use':
      return toolUseLabel(row);
    case 'tool_result':
      return TASK_ACTIVITY_THINKING;
    case 'task_question':
      return row.status === 'pending' || row.status === undefined
        ? TASK_ACTIVITY_WAITING
        : TASK_ACTIVITY_THINKING;
    case 'action_request':
    case 'connect_request':
      return row.status === 'pending' || row.status === 'open' || row.status === undefined
        ? TASK_ACTIVITY_WAITING
        : TASK_ACTIVITY_THINKING;
    case 'text':
      if (row.role === 'user') return TASK_ACTIVITY_STARTING;
      if (row.role === 'assistant') return TASK_ACTIVITY_THINKING;
      return null;
    default:
      return null;
  }
}

function seqOf(row: Row): number | null {
  return typeof row.seq === 'number' ? row.seq : null;
}

/** Follows one run conversation's live events for the thread's activity line.
 *  Pass the ACTIVE run's conversation id (running or waiting), null otherwise. */
export function useTaskRunActivity(conversationId: string | null): TaskRunActivity {
  const [state, setState] = useState<ActivityState>(() => initialState(conversationId));

  useEffect(() => {
    if (conversationId === null) return;
    const id = conversationId;
    setState(initialState(id));

    // All per-conversation bookkeeping lives in this closure, so an id
    // change starts from scratch and a late callback of the old closure
    // sees `active === false`.
    let active = true;
    let clock = 0;
    // Stamp of the label on screen; 0 = nothing derived yet.
    let labelStamp = 0;
    // Highest transcript seq applied; null before the first rows.
    let seenSeq: number | null = null;
    // Highest seq the server announced (ack current_seq, message_appended).
    let knownSeq = 0;
    // Arrival stamp of each announced seq not applied yet.
    const announcedAt = new Map<number, number>();
    let fetching = false;
    let dirty = false;
    // This hook put the conversation in the subscribed set (see the module
    // comment), so it alone takes it out again.
    const owned = !persistentWebSocket.isSubscribed(id);

    const setLabel = (label: string, stamp: number) => {
      if (stamp < labelStamp) return;
      labelStamp = stamp;
      setState((prev) => {
        const base = prev.id === id ? prev : initialState(id);
        return base.label === label && base === prev ? prev : { ...base, label };
      });
    };

    const setStreaming = (streaming: boolean) => {
      setState((prev) => {
        const base = prev.id === id ? prev : initialState(id);
        return base.streaming === streaming && base === prev ? prev : { ...base, streaming };
      });
    };

    // Rows in seq order. Rows at or below the highest seq seen are skipped,
    // except for a window read (`rereadSeen`), which runs only while no
    // label has been derived from them.
    const applyRows = (rows: unknown, fallbackStamp: number, rereadSeen = false) => {
      if (!Array.isArray(rows)) return;
      const sorted = (rows as unknown[])
        .filter((row): row is Row => !!row && typeof row === 'object')
        .sort((a, b) => (seqOf(a) ?? 0) - (seqOf(b) ?? 0));
      // A row is at least as new as the row before it, even when its own
      // message_appended has not arrived yet.
      let runStamp = fallbackStamp;
      for (const row of sorted) {
        const seq = seqOf(row);
        if (seq !== null) {
          if (seenSeq !== null && seq <= seenSeq) {
            if (!rereadSeen) continue;
          } else {
            seenSeq = seq;
          }
        }
        const label = rowActivityLabel(row);
        const stamp = Math.max(runStamp, seq !== null ? announcedAt.get(seq) ?? 0 : 0);
        runStamp = stamp;
        if (label !== null) setLabel(label, stamp);
      }
      if (seenSeq !== null) {
        for (const seq of announcedAt.keys()) {
          if (seq <= seenSeq) announcedAt.delete(seq);
        }
      }
    };

    // Fetch the tail after the highest seq seen, or (`windowRead`, and before
    // any rows were seen) the last TAIL_WINDOW rows before the newest
    // announced seq. Coalesced: one fetch in flight, one dirty re-run.
    const pump = (windowRead = false) => {
      if (fetching) {
        dirty = true;
        return;
      }
      fetching = true;
      const windowStart = Math.max(0, knownSeq - TAIL_WINDOW);
      const afterSeq = windowRead || seenSeq === null ? windowStart : seenSeq;
      const stamp = ++clock;
      fetchConversationTail(id, afterSeq)
        .then((resp) => {
          if (!active) return;
          applyRows(resp.messages, stamp, windowRead);
          if (typeof resp.last_message_seq === 'number') {
            seenSeq = Math.max(seenSeq ?? 0, resp.last_message_seq);
          }
        })
        .catch(() => {
          // Keep the last label; the next event or ack fetches again.
        })
        .finally(() => {
          fetching = false;
          if (active && dirty) {
            dirty = false;
            pump();
          }
        });
    };

    const handleEvent = (event: PersistentEvent) => {
      if (!active) return;
      switch (event.type) {
        case 'subscribed': {
          if (typeof event.run_active === 'boolean') setStreaming(event.run_active);
          const stamp = ++clock;
          if (event.mode === 'catchup') applyRows(event.messages, stamp);
          if (typeof event.current_seq === 'number') {
            knownSeq = Math.max(knownSeq, event.current_seq);
          }
          if (labelStamp === 0) {
            // Nothing derived yet (first ack, or the rows so far said
            // nothing): read the last few rows.
            if (knownSeq > 0) pump(true);
          } else if (seenSeq === null || knownSeq > seenSeq) {
            pump();
          }
          return;
        }
        case 'message_appended': {
          if (typeof event.seq !== 'number') {
            pump();
            return;
          }
          knownSeq = Math.max(knownSeq, event.seq);
          if (seenSeq !== null && event.seq <= seenSeq) return;
          announcedAt.set(event.seq, ++clock);
          pump();
          return;
        }
        case 'resync':
          pump();
          return;
        case 'text_delta':
          setLabel(TASK_ACTIVITY_WRITING, ++clock);
          setStreaming(true);
          return;
        case 'sub_agent_tool_use':
          setLabel(subAgentLabel(event), ++clock);
          setStreaming(true);
          return;
        case 'resume_started':
          setLabel(TASK_ACTIVITY_RESUMING, ++clock);
          setStreaming(true);
          return;
        case 'send_message_finished':
          setStreaming(false);
          return;
        case 'subscription_expired':
          if (owned && persistentWebSocket.isSubscribed(id)) persistentWebSocket.subscribe(id);
          return;
        default:
          return;
      }
    };

    // Handler first, so the ack of the subscribe below cannot be missed.
    const detach = persistentWebSocket.onConversationEvent(id, handleEvent);
    persistentWebSocket.subscribe(id);

    return () => {
      active = false;
      detach();
      if (owned) persistentWebSocket.unsubscribe(id);
    };
  }, [conversationId]);

  if (conversationId === null) return { label: TASK_ACTIVITY_STARTING, streaming: null };
  const current = state.id === conversationId ? state : initialState(conversationId);
  return { label: current.label, streaming: current.streaming };
}
