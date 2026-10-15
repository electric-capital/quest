/**
 * ToolUseMessage component for displaying tool invocations
 */

import { useMemo, useState } from 'react';
import { useCodeAndMath } from '../hooks/useCodeAndMath';
import { formatTimestamp } from '../utils/formatters';
import { JsonView, tryParseJsonContainer } from './JsonView';
import type { ToolUseMessage as ToolUseMessageType, ToolResultMessage as ToolResultMessageType, SubAgentToolCallInfo, SubAgentFinishedInfo } from '../api/types';
import './ToolUseMessage.css';

/**
 * For run_python calls (direct or through the tool_call meta tool), pull the
 * inline Python source out of the input so it can be rendered as real code
 * instead of a JSON-escaped one-liner. Returns null for everything else.
 */
function extractPythonScript(
  toolName: string,
  toolInput: Record<string, unknown>,
): { script: string; otherParams: Record<string, unknown> } | null {
  let container: Record<string, unknown> | null = null;
  if (toolName === 'run_python') {
    container = toolInput;
  } else if (toolName === 'tool_call' && toolInput.tool_name === 'run_python') {
    const args = toolInput.arguments;
    if (args && typeof args === 'object') container = args as Record<string, unknown>;
  }
  if (!container || typeof container.script !== 'string') return null;
  const { script, ...otherParams } = container;
  return { script: script as string, otherParams };
}

/**
 * Renders a tool's Input section content: run_python scripts as a
 * syntax-highlighted Python block (remaining params, e.g. args/timeout,
 * as JSON underneath), everything else as pretty-printed JSON. The script
 * shows as plain text until the lazy code-and-math chunk (highlight.js) is in.
 */
function ToolInputContent({ toolName, toolInput }: { toolName: string; toolInput: Record<string, unknown> }) {
  const extracted = extractPythonScript(toolName, toolInput);
  const codeAndMath = useCodeAndMath(extracted !== null);
  if (!extracted) {
    return <JsonView value={toolInput} />;
  }

  let highlighted = '';
  try {
    highlighted = codeAndMath ? codeAndMath.highlightPython(extracted.script) : '';
  } catch {
    // fall through to plain text
  }

  return (
    <>
      <pre className="tool-section-content tool-script-code">
        {highlighted ? (
          <code dangerouslySetInnerHTML={{ __html: highlighted }} />
        ) : (
          <code>{extracted.script}</code>
        )}
      </pre>
      {Object.keys(extracted.otherParams).length > 0 && (
        <div className="tool-script-params">
          <JsonView value={extracted.otherParams} />
        </div>
      )}
    </>
  );
}

/**
 * Renders a tool's Output section content: outputs that parse as a JSON
 * object/array get the interactive JsonView tree, everything else stays a
 * plain preformatted text block.
 */
function ToolOutputContent({ toolOutput }: { toolOutput: string }) {
  const parsed = useMemo(() => tryParseJsonContainer(toolOutput), [toolOutput]);
  if (parsed !== null) {
    return <JsonView value={parsed} />;
  }
  return (
    <pre className="tool-section-content tool-output">
      {toolOutput}
    </pre>
  );
}

interface ToolUseMessageProps {
  toolUse: ToolUseMessageType;
  toolResult?: ToolResultMessageType;
  childToolCalls?: SubAgentToolCallInfo[];
  // Per-agent terminal state for this parent tool_id, keyed by agent_name.
  // Present only for agent_task / agent_task_parallel rows.
  subAgentReturned?: Map<string, SubAgentFinishedInfo>;
}

/**
 * Renders a single sub-agent tool call entry with expandable detail.
 */
function SubAgentToolCallEntry({ call }: { call: SubAgentToolCallInfo }) {
  const [isDetailExpanded, setIsDetailExpanded] = useState(false);

  const displayName = call.toolUse.intent_message || call.toolUse.tool_name;
  const displayIntent = call.toolUse.intent_message ? call.toolUse.tool_name : '';

  return (
    <div className={`sub-agent-tool-item ${isDetailExpanded ? 'expanded' : ''}`}>
      <div
        className="sub-agent-tool-item-header"
        onClick={() => setIsDetailExpanded(!isDetailExpanded)}
      >
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
        </svg>
        <span className={`sub-agent-tool-name ${call.toolUse.intent_message ? 'sub-agent-tool-name-description' : ''}`}>{displayName}</span>
        {displayIntent && (
          <span className="sub-agent-tool-intent">{displayIntent}</span>
        )}
        {call.toolResult ? (
          <span className="tool-status completed">completed</span>
        ) : (
          <span className="tool-status running">running...</span>
        )}
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          className={`sub-agent-tool-chevron ${isDetailExpanded ? 'expanded' : ''}`}
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </div>
      {isDetailExpanded && (
        <div className="sub-agent-tool-detail">
          <div className="tool-section">
            <div className="tool-section-header">Input</div>
            <ToolInputContent toolName={call.toolUse.tool_name} toolInput={call.toolUse.tool_input} />
          </div>
          {call.toolResult && (
            <div className="tool-section">
              <div className="tool-section-header">Output</div>
              <ToolOutputContent toolOutput={call.toolResult.tool_output} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Partition an agent's child tool calls into plain leaf calls, nested-agent
 * NODE calls (the `agent_task_nested` invocations, identified by
 * `nested_agent_id`), and the grandchild calls produced by those nested
 * agents (identified by `nested_parent_id`, grouped under their node).
 *
 * `leafCalls` keeps only the calls that are this agent's own direct tool
 * calls (no nested provenance). Each entry in `nodes` is a nested-agent node
 * plus the grandchild calls grouped under it.
 */
interface NestedAgentNodeInfo {
  node: SubAgentToolCallInfo;
  nestedAgentId: string;
  grandchildCalls: SubAgentToolCallInfo[];
}

function partitionNestedCalls(calls: SubAgentToolCallInfo[]): {
  leafCalls: SubAgentToolCallInfo[];
  nodes: NestedAgentNodeInfo[];
} {
  const leafCalls: SubAgentToolCallInfo[] = [];
  const nodeCalls: SubAgentToolCallInfo[] = [];
  const grandchildrenByNode: Record<string, SubAgentToolCallInfo[]> = {};

  for (const call of calls) {
    const nestedAgentId = call.toolUse.nested_agent_id;
    const nestedParentId = call.toolUse.nested_parent_id;
    if (nestedAgentId) {
      nodeCalls.push(call);
    } else if (nestedParentId) {
      (grandchildrenByNode[nestedParentId] ||= []).push(call);
    } else {
      leafCalls.push(call);
    }
  }

  const nodes: NestedAgentNodeInfo[] = nodeCalls.map((node) => {
    const nestedAgentId = node.toolUse.nested_agent_id as string;
    return {
      node,
      nestedAgentId,
      grandchildCalls: grandchildrenByNode[nestedAgentId] || [],
    };
  });

  return { leafCalls, nodes };
}

/**
 * Renders the flat list of an agent's direct (leaf) tool calls plus any
 * nested-agent nodes (each with its own grandchild tool calls indented one
 * extra level). `nestedReturned` carries the per-nested-agent finished state
 * keyed by `nested_agent_id`. Reused by both the single-agent_task path and
 * inside a parallel SubAgentSection.
 */
function SubAgentToolCallList({
  calls,
  nestedReturned,
}: {
  calls: SubAgentToolCallInfo[];
  nestedReturned?: Map<string, SubAgentFinishedInfo>;
}) {
  const { leafCalls, nodes } = partitionNestedCalls(calls);
  return (
    <div className="sub-agent-tool-list">
      {leafCalls.map((call) => (
        <SubAgentToolCallEntry key={call.toolUse.tool_id} call={call} />
      ))}
      {nodes.map((n) => (
        <NestedAgentNode
          key={n.nestedAgentId}
          info={n}
          returned={nestedReturned?.get(n.nestedAgentId)}
        />
      ))}
    </div>
  );
}

/**
 * Renders a nested (2nd-level) agent node: a child agent row (name + model +
 * RUNNING/COMPLETED/ERRORED badge) with the grandchild's own tool calls
 * nested one extra indent level under it. The node's terminal state comes
 * from its NODE `sub_agent_tool_result` (success/error via
 * `nested_agent_status`); `returned` (the grandchild's sub_agent_finished,
 * keyed by `nested_agent_id`) is a fallback so the badge resolves on reload.
 */
function NestedAgentNode({ info, returned }: { info: NestedAgentNodeInfo; returned?: SubAgentFinishedInfo }) {
  const [isExpanded, setIsExpanded] = useState(false);

  const nestedName =
    (info.node.toolUse.nested_agent_name as string)
    || (info.node.toolUse.tool_input?.name as string)
    || 'Nested Sub-Agent';
  const nestedModel = info.node.toolUse.nested_agent_model as string | undefined;

  // Terminal state: the NODE tool_result carries nested_agent_status; fall
  // back to the grandchild's sub_agent_finished entry (reload path).
  const nodeStatus = info.node.toolResult?.nested_agent_status;
  const finished = nodeStatus != null || returned != null;
  const errored = nodeStatus === 'error' || (returned != null && returned.status === 'error');
  const count = info.grandchildCalls.length;

  return (
    <div className={`sub-agent-section nested-agent-node ${isExpanded ? 'expanded' : ''}`}>
      <div className="sub-agent-header" onClick={() => setIsExpanded(!isExpanded)}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
        <span className="sub-agent-name">{nestedName}</span>
        {nestedModel && <span className="nested-agent-model">{nestedModel}</span>}
        <span className="tool-child-count">{count} tool{count !== 1 ? 's' : ''}</span>
        {errored ? (
          <span className="tool-status errored" title="Nested sub-agent failed">errored</span>
        ) : finished ? (
          <span className="tool-status completed">completed</span>
        ) : (
          <span className="tool-status running">running...</span>
        )}
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          className={`sub-agent-section-chevron ${isExpanded ? 'expanded' : ''}`}
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </div>
      {isExpanded && (
        <div className="nested-agent-detail">
          {/* Grandchild tool calls (one level deeper than this node). */}
          {count > 0 ? (
            <div className="sub-agent-tool-list">
              {info.grandchildCalls.map((call) => (
                <SubAgentToolCallEntry key={call.toolUse.tool_id} call={call} />
              ))}
            </div>
          ) : (
            <div className="sub-agent-tool-list nested-agent-empty">
              <span className="sub-agent-tool-intent">No tool calls</span>
            </div>
          )}

          {/* Prompt + response, mirroring the outer sub-agent's detail. */}
          <div className="sub-agent-tool-detail">
            <div className="tool-section">
              <div className="tool-section-header">Input</div>
              <ToolInputContent toolName={info.node.toolUse.tool_name} toolInput={info.node.toolUse.tool_input} />
            </div>
            {info.node.toolResult && (
              <div className="tool-section">
                <div className="tool-section-header">Output</div>
                <ToolOutputContent toolOutput={info.node.toolResult.tool_output} />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Renders a sub-agent section (for parallel tasks) with expandable detail.
 *
 * `returned` is the canonical "this sub-agent has finished" signal, emitted
 * by the backend as a sub_agent_finished event. COMPLETED / ERRORED only
 * flip when that event has been seen; otherwise RUNNING is shown, even if
 * the last inner tool call already has a result (the sub-agent might still
 * be mid-turn, e.g. summarizing before calling agent_task_response).
 *
 * `nestedReturned` is the full per-parent finished map (keyed by agent name
 * AND by nested_agent_id) so nested-agent nodes inside this section can
 * resolve their own badge.
 */
function SubAgentSection({ agentName, calls, returned, nestedReturned }: { agentName: string; calls: SubAgentToolCallInfo[]; returned?: SubAgentFinishedInfo; nestedReturned?: Map<string, SubAgentFinishedInfo> }) {
  const [isSectionExpanded, setIsSectionExpanded] = useState(false);

  const finished = returned != null;
  const errored = finished && returned!.status === 'error';

  // Only this agent's direct tool calls count toward the badge; nested-agent
  // nodes and grandchild calls are rendered (and counted) inside the node.
  const directCount = calls.filter(
    (c) => !c.toolUse.nested_parent_id,
  ).length;

  return (
    <div className={`sub-agent-section ${isSectionExpanded ? 'expanded' : ''}`}>
      <div
        className="sub-agent-header"
        onClick={() => setIsSectionExpanded(!isSectionExpanded)}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
        <span className="sub-agent-name">{agentName}</span>
        <span className="tool-child-count">{directCount} tool{directCount !== 1 ? 's' : ''}</span>
        {errored ? (
          <span className="tool-status errored" title={returned!.error || 'Sub-agent failed'}>errored</span>
        ) : finished ? (
          <span className="tool-status completed">completed</span>
        ) : (
          <span className="tool-status running">running...</span>
        )}
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          className={`sub-agent-section-chevron ${isSectionExpanded ? 'expanded' : ''}`}
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </div>
      {isSectionExpanded && (
        <SubAgentToolCallList calls={calls} nestedReturned={nestedReturned} />
      )}
    </div>
  );
}

export function ToolUseMessage({ toolUse, toolResult, childToolCalls, subAgentReturned }: ToolUseMessageProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  const toggleExpanded = () => setIsExpanded(!isExpanded);

  // For agent_task and agent_task_parallel (and the template variant), derive a better display from the input
  const isAgentTask = toolUse.tool_name === 'agent_task';
  const isAgentTaskParallel = toolUse.tool_name === 'agent_task_parallel';
  const isAgentTaskParallelTemplate = toolUse.tool_name === 'agent_task_parallel_template';
  const isAnyParallel = isAgentTaskParallel || isAgentTaskParallelTemplate;
  const isSubAgent = isAgentTask || isAnyParallel;
  const hasChildren = childToolCalls && childToolCalls.length > 0;

  let displayName: string;
  let displayIntent: string;

  if (isAgentTask) {
    displayName = (toolUse.tool_input.name as string) || 'Sub-Agent';
    displayIntent = (toolUse.tool_input.description as string) || '';
  } else if (isAgentTaskParallel) {
    const tasks = (toolUse.tool_input.tasks as Array<Record<string, unknown>>) || [];
    const count = tasks.length;
    const names = tasks.map(t => t.name as string).filter(Boolean);
    displayName = `${count} parallel sub-agent${count !== 1 ? 's' : ''}`;
    displayIntent = names.length > 0 ? names.join(', ') : '';
  } else if (isAgentTaskParallelTemplate) {
    const agents = (toolUse.tool_input.agents as Array<Record<string, unknown>>) || [];
    const count = agents.length;
    const names = agents.map(a => a.name as string).filter(Boolean);
    displayName = `${count} parallel sub-agent${count !== 1 ? 's' : ''}`;
    displayIntent = names.length > 0 ? names.join(', ') : '';
  } else {
    // For meta tool_call, show the inner tool name instead of "tool_call"
    const effectiveToolName = toolUse.tool_name === 'tool_call' && toolUse.tool_input.tool_name
      ? toolUse.tool_input.tool_name as string
      : toolUse.tool_name;

    // wait_for_handles is an internal mechanism users do not understand. The
    // model is required to pass a `reason` describing what it is waiting for;
    // surface that as the friendly label so the chat reads "Waiting for: ..."
    // instead of leaking the tool name.
    let waitReason = '';
    if (effectiveToolName === 'wait_for_handles') {
      const meta = toolUse.tool_input as Record<string, unknown>;
      const innerArgs = (meta.arguments as Record<string, unknown> | undefined) ?? meta;
      const candidate = innerArgs?.reason;
      if (typeof candidate === 'string' && candidate.trim()) {
        waitReason = candidate.trim();
      }
    }

    if (waitReason) {
      displayName = `Waiting for: ${waitReason}`;
      displayIntent = '';
    } else if (toolUse.intent_message) {
      // Show intent_message as primary if available; fall back to tool name
      displayName = toolUse.intent_message;
      displayIntent = effectiveToolName;
    } else {
      displayName = effectiveToolName;
      displayIntent = '';
    }
  }

  // Group child tool calls by 1st-level agent name for parallel display.
  // Nested-agent NODE calls (carrying nested_agent_id) keep their 1st-level
  // agentName and group normally. Grandchild calls (carrying nested_parent_id)
  // are NOT grouped here -- they are rendered one level deeper inside their
  // node by SubAgentToolCallList -- but they must still be attached to the
  // SAME 1st-level agent section so the node and its grandchildren land
  // together. We attribute each grandchild to the agent that owns its node.
  const nodeAgentById: Record<string, string> = {};
  if (hasChildren) {
    for (const info of childToolCalls) {
      const nid = info.toolUse.nested_agent_id;
      if (nid) nodeAgentById[nid] = info.agentName || 'Sub-Agent';
    }
  }
  const childrenByAgent = hasChildren
    ? childToolCalls.reduce<Record<string, SubAgentToolCallInfo[]>>((acc, info) => {
        const nestedParentId = info.toolUse.nested_parent_id;
        const name = nestedParentId
          ? (nodeAgentById[nestedParentId] || info.agentName || 'Sub-Agent')
          : (info.agentName || 'Sub-Agent');
        if (!acc[name]) acc[name] = [];
        acc[name].push(info);
        return acc;
      }, {})
    : {};

  // Determine the expected set of sub-agent names for the outer group badge.
  // For agent_task_parallel, the truth is the parent tool's `tasks` input --
  // using `childrenByAgent` alone would mis-flag the group as "complete"
  // as soon as every agent that has started has finished, even if some
  // agents haven't produced any events yet.
  // For the template variant, the truth is the `agents` array.
  // For the single agent_task, it's the one `name` in the input.
  let expectedAgentNames: string[] = [];
  if (isAgentTask) {
    const n = (toolUse.tool_input.name as string) || '';
    if (n) expectedAgentNames = [n];
  } else if (isAgentTaskParallel) {
    const tasks = (toolUse.tool_input.tasks as Array<Record<string, unknown>>) || [];
    const agents = (toolUse.tool_input.agents as Array<Record<string, unknown>>) || [];
    const source = tasks.length > 0 ? tasks : agents;
    expectedAgentNames = source.map(t => t.name as string).filter(Boolean);
  } else if (isAgentTaskParallelTemplate) {
    const agents = (toolUse.tool_input.agents as Array<Record<string, unknown>>) || [];
    expectedAgentNames = agents.map(a => a.name as string).filter(Boolean);
  }

  // Fold in any agent names we've actually seen in case the input didn't
  // enumerate them (defensive — keeps RUNNING sticky until every seen agent
  // has emitted sub_agent_finished).
  if (hasChildren) {
    for (const name of Object.keys(childrenByAgent)) {
      if (!expectedAgentNames.includes(name)) expectedAgentNames.push(name);
    }
  }

  // New "all returned" rule: every expected sub-agent must have a terminal
  // entry in subAgentReturned. Empty expected list (plain non-sub-agent
  // tools) falls back to toolResult-based logic below.
  const liveFinishedSignal = isSubAgent && subAgentReturned && expectedAgentNames.length > 0
    ? expectedAgentNames.every(name => subAgentReturned.has(name))
    : false;
  const anyErrored = isSubAgent && subAgentReturned
    ? expectedAgentNames.some(name => subAgentReturned.get(name)?.status === 'error')
    : false;

  // Fallback for pre-change persisted conversations that never carried
  // sub_agent_finished entries: if the parent tool_result is present,
  // historical conversations were already "done" when saved, so treat
  // them as completed even though subAgentReturned is empty.
  const historicalFallbackFinished = isSubAgent && toolResult != null
    && (!subAgentReturned || subAgentReturned.size === 0);

  const subAgentAllFinished = liveFinishedSignal || historicalFallbackFinished;

  // For the historical-fallback case (old persisted conversations predating
  // sub_agent_finished), synthesize a per-agent "success" map so that each
  // SubAgentSection row also renders as completed. In the live case we use
  // the real map exactly as provided.
  const effectiveSubAgentReturned: Map<string, SubAgentFinishedInfo> | undefined =
    isSubAgent && historicalFallbackFinished
      ? (() => {
          const synth = new Map<string, SubAgentFinishedInfo>();
          for (const name of expectedAgentNames) synth.set(name, { status: 'success' });
          if (hasChildren) {
            for (const name of Object.keys(childrenByAgent)) {
              if (!synth.has(name)) synth.set(name, { status: 'success' });
            }
          }
          return synth;
        })()
      : subAgentReturned;

  return (
    <div className={`tool-use-message ${isExpanded ? 'expanded' : 'collapsed'} ${hasChildren ? 'has-children' : ''}`}>
      <div className="tool-use-header" onClick={toggleExpanded}>
        <div className="tool-use-icon">
          {isSubAgent ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
              <circle cx="12" cy="7" r="4" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
            </svg>
          )}
        </div>
        <div className="tool-use-info">
          <span className={`tool-name ${!isSubAgent && toolUse.intent_message ? 'tool-name-description' : ''}`}>{displayName}</span>
          {displayIntent && (
            <span className="tool-intent">{displayIntent}</span>
          )}
          {hasChildren && (
            <span className="tool-child-count">{childToolCalls.length} tool{childToolCalls.length !== 1 ? 's' : ''}</span>
          )}
          {isSubAgent ? (
            // For agent_task / agent_task_parallel, "completed" means every
            // expected sub-agent has emitted sub_agent_finished (not merely
            // that the last inner tool_result has arrived). If any finished
            // with status=error, surface an "errored" badge.
            anyErrored && subAgentAllFinished ? (
              <span className="tool-status errored">errored</span>
            ) : subAgentAllFinished ? (
              <span className="tool-status completed">completed</span>
            ) : (
              <span className="tool-status running">running...</span>
            )
          ) : (
            <>
              {toolResult && (
                <span className="tool-status completed">completed</span>
              )}
              {!toolResult && (
                <span className="tool-status running">running...</span>
              )}
            </>
          )}
        </div>
        <div className="tool-use-toggle">
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className={`chevron ${isExpanded ? 'expanded' : ''}`}
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </div>
        {isExpanded && (
          <span className="tool-timestamp">
            {formatTimestamp(toolUse.timestamp)}
          </span>
        )}
      </div>

      {/* Sub-agent child tool calls (shown when collapsed, as a summary tree) */}
      {hasChildren && !isExpanded && (
        <div className="sub-agent-children">
          {isAnyParallel ? (
            // For parallel (and template): group by agent name, each expandable
            Object.entries(childrenByAgent).map(([agentName, calls]) => (
              <SubAgentSection
                key={agentName}
                agentName={agentName}
                calls={calls}
                returned={effectiveSubAgentReturned?.get(agentName)}
                nestedReturned={effectiveSubAgentReturned}
              />
            ))
          ) : (
            // For single agent_task: show this agent's direct tool calls, plus
            // any nested-agent node (with its grandchildren) one level deeper.
            <SubAgentToolCallList calls={childToolCalls} nestedReturned={effectiveSubAgentReturned} />
          )}
        </div>
      )}

      {isExpanded && (
        <div className="tool-use-details">
          {/* Sub-agent child tool calls in expanded view */}
          {hasChildren ? (
            <div className="sub-agent-children sub-agent-children-expanded">
              {isAnyParallel ? (
                Object.entries(childrenByAgent).map(([agentName, calls]) => (
                  <SubAgentSection
                    key={agentName}
                    agentName={agentName}
                    calls={calls}
                    returned={effectiveSubAgentReturned?.get(agentName)}
                    nestedReturned={effectiveSubAgentReturned}
                  />
                ))
              ) : (
                <SubAgentToolCallList calls={childToolCalls} nestedReturned={effectiveSubAgentReturned} />
              )}
            </div>
          ) : null}

          <div className="tool-section">
            <div className="tool-section-header">Input</div>
            <ToolInputContent toolName={toolUse.tool_name} toolInput={toolUse.tool_input} />
          </div>

          {toolResult && (
            <div className="tool-section">
              <div className="tool-section-header">Output</div>
              <ToolOutputContent toolOutput={toolResult.tool_output} />
            </div>
          )}
        </div>
      )}

    </div>
  );
}
