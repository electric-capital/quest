/**
 * Quest Docs viewer (/docs/<id>): a view-only page for one doc.
 *
 * DocHeader (title menu, mode badge, project chip, Show source toggle) over
 * the body -- the markdown rendered through the chat renderer's shared
 * `markdownComponents`, with `assets/<name>` images resolved against the
 * doc's asset route via MarkdownWorkspaceContext.assetBase, or the raw
 * markdown in a <pre> -- and a footer naming the last writer and the update
 * time. Data comes from useDoc, which follows the realtime doc events.
 */

import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import { docAssetBase } from '../../api/docsApi';
import type { DocDetail } from '../../api/types';
import { useDoc } from '../../hooks/useDoc';
import { docsListPath } from '../../utils/docsRoute';
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters';
import remarkMathCurrencyGuard from '../../utils/remarkMathCurrencyGuard';
import { MarkdownWorkspaceContext, markdownComponents } from '../Message';
import { DocHeader } from './DocHeader';
import './DocViewer.css';

const CONVERSATION_SOURCE_PREFIX = 'conversation:';
const ACTION_REQUEST_SOURCE_RE = /^action_request:(.+)$/;

/**
 * "Last written by" subject for `last_write_source`, or null to leave the
 * phrase out (no source or an unknown one). The writing conversation's
 * title and project come with the doc (`last_write_conversation`, resolved
 * by GET /docs/{id} from one metadata row); a conversation that no longer
 * exists reads "a deleted conversation".
 */
function lastWriter(doc: DocDetail): ReactNode {
  const source = doc.last_write_source;
  if (source === 'ui') return 'you';
  const actionRequest = source ? ACTION_REQUEST_SOURCE_RE.exec(source) : null;
  if (actionRequest) return `action request #${actionRequest[1]}`;
  if (!source?.startsWith(CONVERSATION_SOURCE_PREFIX)) return null;
  const writer = doc.last_write_conversation;
  if (!writer) return 'a deleted conversation';
  const id = encodeURIComponent(writer.id);
  const path = writer.project_id
    ? `/projects/${encodeURIComponent(writer.project_id)}/${id}`
    : `/chats/${id}`;
  return (
    <Link to={path} className="doc-viewer-footer-link">
      {writer.title || 'a conversation'}
    </Link>
  );
}

function DocFooter({ doc }: { doc: DocDetail }) {
  const writer = lastWriter(doc);
  const updated = parseUTCTimestamp(doc.updated_at);
  return (
    <footer className="doc-viewer-footer">
      {writer !== null && (
        <>
          <span className="doc-viewer-footer-writer">Last written by {writer}</span>
          <span className="doc-viewer-footer-sep" aria-hidden="true"> · </span>
        </>
      )}
      <span className="doc-viewer-footer-updated" title={updated.toLocaleString()}>
        Updated {formatRelativeTimestamp(doc.updated_at)}
      </span>
    </footer>
  );
}

export function DocViewer({ docId }: { docId: string }) {
  const { doc, loading, error, notFound, refresh, applyRow } = useDoc(docId);
  const navigate = useNavigate();

  // Show source is per doc: tagged with the doc it was turned on for, so
  // opening another doc starts rendered again.
  const [sourceDocId, setSourceDocId] = useState<string | null>(null);
  const showSource = sourceDocId === docId;
  const toggleSource = useCallback(() => {
    setSourceDocId((current) => (current === docId ? null : docId));
  }, [docId]);

  const [retrying, setRetrying] = useState(false);
  const retry = useCallback(async () => {
    setRetrying(true);
    try {
      await refresh();
    } finally {
      setRetrying(false);
    }
  }, [refresh]);

  const markdownCtx = useMemo(() => ({ assetBase: docAssetBase(docId) }), [docId]);

  const projectId = doc?.project_id ?? null;
  const handleDeleted = useCallback(() => {
    navigate(docsListPath(projectId));
  }, [navigate, projectId]);

  if (loading) {
    return <div className="doc-viewer-state">Loading...</div>;
  }

  if (notFound) {
    return (
      <div className="doc-viewer-state">
        <p className="doc-viewer-state-text">This doc no longer exists.</p>
        <Link to={docsListPath()} className="doc-viewer-state-link">
          All docs
        </Link>
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="doc-viewer-state">
        <p className="doc-viewer-state-text">{error?.message || 'Failed to load the doc.'}</p>
        <button
          type="button"
          className="doc-viewer-retry"
          onClick={() => void retry()}
          disabled={retrying}
        >
          {retrying ? 'Retrying...' : 'Retry'}
        </button>
      </div>
    );
  }

  const isEmpty = doc.content.trim() === '';

  return (
    <div className="doc-viewer">
      <div className="doc-viewer-column">
        <DocHeader
          key={doc.id}
          doc={doc}
          showSource={showSource}
          onToggleSource={toggleSource}
          onRowApplied={applyRow}
          onDeleted={handleDeleted}
        />
        <div className="doc-viewer-body">
          {isEmpty ? (
            <p className="doc-viewer-empty">This doc is empty. Ask Quest to add to it.</p>
          ) : showSource ? (
            <pre className="doc-viewer-source">{doc.content}</pre>
          ) : (
            // `message-content` = the chat markdown typography; DocViewer.css
            // strips the bubble chrome so it reads like an assistant reply.
            <div className="message-content doc-viewer-markdown">
              <MarkdownWorkspaceContext.Provider value={markdownCtx}>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard]}
                  rehypePlugins={[rehypeHighlight, rehypeKatex]}
                  components={markdownComponents}
                >
                  {doc.content}
                </ReactMarkdown>
              </MarkdownWorkspaceContext.Provider>
            </div>
          )}
        </div>
        <DocFooter doc={doc} />
      </div>
    </div>
  );
}
