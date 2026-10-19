/**
 * Search modal for searching across all conversation messages.
 * Uses createPortal for rendering outside the component tree.
 */

import { useState, useCallback, useRef, useEffect } from 'react';
import { searchConversations } from '../api/client';
import type { SearchResult } from '../api/types';
import { ModalShell } from './ModalShell';
import './SearchModal.css';

interface SearchModalProps {
  isOpen: boolean;
  onClose: () => void;
  onNavigate: (conversationId: string, projectId: string | null, messageIndex: number) => void;
}

/**
 * Format a timestamp for display in search results.
 */
function formatTimestamp(ts: string): string {
  try {
    const date = new Date(ts);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) {
      return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    } else if (diffDays === 1) {
      return 'Yesterday';
    } else if (diffDays < 7) {
      return date.toLocaleDateString(undefined, { weekday: 'short' });
    } else {
      return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }
  } catch {
    return '';
  }
}

export function SearchModal({ isOpen, onClose, onNavigate }: SearchModalProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [totalMatches, setTotalMatches] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasSearched, setHasSearched] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  // Routine runs are hidden by default: a daily routine would otherwise
  // repeat the same hit once per run and bury the user's own chats.
  const [includeRoutines, setIncludeRoutines] = useState(false);

  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const resultsRef = useRef<HTMLDivElement>(null);

  // Focus input when modal opens; reset state
  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setResults([]);
      setTotalMatches(0);
      setError(null);
      setHasSearched(false);
      setHighlightedIndex(-1);
      setIncludeRoutines(false);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [isOpen]);

  // Debounced search
  const performSearch = useCallback(async (searchQuery: string, withRoutines: boolean) => {
    if (searchQuery.length < 2) {
      setResults([]);
      setTotalMatches(0);
      setHasSearched(false);
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const response = await searchConversations(searchQuery, { includeRoutines: withRoutines });
      setResults(response.results);
      setTotalMatches(response.total_matches);
      setHasSearched(true);
      setHighlightedIndex(-1);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed');
      setResults([]);
      setTotalMatches(0);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setQuery(value);

    // Clear previous debounce timer
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }

    // Debounce the search call
    debounceRef.current = setTimeout(() => {
      performSearch(value.trim(), includeRoutines);
    }, 300);
  }, [performSearch, includeRoutines]);

  // Toggling the routines filter re-runs the current query right away.
  const handleIncludeRoutinesChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const checked = e.target.checked;
    setIncludeRoutines(checked);
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    performSearch(query.trim(), checked);
  }, [performSearch, query]);

  // Cleanup debounce timer on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);

  const handleResultClick = useCallback((result: SearchResult) => {
    onNavigate(result.conversation_id, result.project_id, result.message_index);
    onClose();
  }, [onNavigate, onClose]);

  // Keyboard navigation (Escape is handled by ModalShell)
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlightedIndex(prev => Math.min(prev + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlightedIndex(prev => Math.max(prev - 1, -1));
    } else if (e.key === 'Enter' && highlightedIndex >= 0 && highlightedIndex < results.length) {
      e.preventDefault();
      handleResultClick(results[highlightedIndex]);
    }
  }, [results, highlightedIndex, handleResultClick]);

  // Scroll highlighted item into view
  useEffect(() => {
    if (highlightedIndex >= 0 && resultsRef.current) {
      const items = resultsRef.current.querySelectorAll('.search-result-item');
      if (items[highlightedIndex]) {
        items[highlightedIndex].scrollIntoView({ block: 'nearest' });
      }
    }
  }, [highlightedIndex]);

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      onKeyDown={handleKeyDown}
      overlayClassName="search-modal-overlay"
      modalClassName="search-modal"
    >
      {/* Header */}
      <div className="search-modal-header">
        <h2>Search</h2>
        <button className="search-modal-close-button" onClick={onClose}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>

      {/* Search input */}
      <div className="search-modal-input-wrapper">
        <svg className="search-modal-input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="11" cy="11" r="8"></circle>
          <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
        </svg>
        <input
          ref={inputRef}
          type="text"
          className="search-modal-input"
          value={query}
          onChange={handleInputChange}
          placeholder="Search conversations..."
        />
      </div>

      {/* Filters */}
      <div className="search-modal-options">
        <label className="search-modal-option" title="Also search conversations created by routines">
          <input
            type="checkbox"
            checked={includeRoutines}
            onChange={handleIncludeRoutinesChange}
          />
          Include routine runs
        </label>
      </div>

      {/* Results */}
      <div className="search-modal-results" ref={resultsRef}>
        {loading && (
          <div className="search-modal-status">Searching...</div>
        )}

        {error && (
          <div className="search-modal-error">{error}</div>
        )}

        {!loading && !error && hasSearched && results.length === 0 && (
          <div className="search-modal-status">No matches found</div>
        )}

        {!loading && results.length > 0 && (
          <>
            <div className="search-modal-count">
              {totalMatches} match{totalMatches !== 1 ? 'es' : ''} found
            </div>
            {results.map((result, index) => (
              <div
                key={`${result.conversation_id}-${result.message_index}`}
                className={`search-result-item${index === highlightedIndex ? ' highlighted' : ''}`}
                onClick={() => handleResultClick(result)}
              >
                <div className="search-result-title">
                  <span>{result.conversation_title}</span>
                  {result.routine_id && (
                    <span className="search-result-badge">routine</span>
                  )}
                  {result.archived && (
                    <span className="search-result-badge">archived</span>
                  )}
                </div>
                <div
                  className="search-result-snippet"
                  dangerouslySetInnerHTML={{ __html: result.snippet }}
                />
                <div className="search-result-meta">
                  <span className="search-result-role">{result.message_role}</span>
                  <span>{formatTimestamp(result.timestamp)}</span>
                </div>
              </div>
            ))}
          </>
        )}
      </div>

      {/* Keyboard shortcut hint */}
      <div className="search-modal-shortcut-hint">
        <kbd>Esc</kbd> to close
        {results.length > 0 && (
          <> &middot; <kbd>&uarr;</kbd><kbd>&darr;</kbd> to navigate &middot; <kbd>Enter</kbd> to select</>
        )}
      </div>
    </ModalShell>
  );
}
