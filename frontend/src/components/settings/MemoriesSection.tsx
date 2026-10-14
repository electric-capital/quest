// Settings > Memories: a compact table of the user's memories, filtered
// inline by a search box, with click-to-expand rows and in-row editing so
// dozens of memories fit on one screen.
import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import type { KeyboardEvent } from 'react';
import { Archive, ArchiveRestore, Pencil, Search, Trash2, X } from 'lucide-react';
import { fetchMemories, createMemory, updateMemory, archiveMemory, unarchiveMemory, deleteMemory } from '../../api/client';
import type { Memory } from '../../api/types';
import './MemoriesSection.css';

/** "Oct 14" within the current year, "Oct 14, 2025" otherwise. */
function shortDate(iso: string): string {
  const date = new Date(iso);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }),
  });
}

function fullDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit',
  });
}

function dateTooltip(memory: Memory): string {
  const created = `Created ${fullDate(memory.created_at)}`;
  return memory.updated_at ? `${created}\nUpdated ${fullDate(memory.updated_at)}` : created;
}

interface MemoryEditorProps {
  value: string;
  onChange: (value: string) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  placeholder?: string;
  label: string;
}

/** Textarea + Save/Cancel for a new or edited memory. Ctrl/Cmd+Enter saves,
 *  Escape cancels. */
function MemoryEditor({ value, onChange, onSave, onCancel, saving, placeholder, label }: MemoryEditorProps) {
  const canSave = value.trim().length > 0 && !saving;
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onCancel();
    } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && canSave) {
      e.preventDefault();
      onSave();
    }
  };
  return (
    <div className="memory-editor">
      <textarea
        className="memory-edit-textarea"
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        rows={3}
        autoFocus
      />
      <div className="memory-card-actions">
        <button className="memory-btn memory-btn-save" onClick={onSave} disabled={!canSave}>
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button className="memory-btn memory-btn-cancel" onClick={onCancel}>
          Cancel
        </button>
        <span className="memory-editor-hint">Markdown supported, max 4KB</span>
      </div>
    </div>
  );
}

export function MemoriesSection() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [isLoadingMemories, setIsLoadingMemories] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [filter, setFilter] = useState('');
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [editingContent, setEditingContent] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [newMemoryContent, setNewMemoryContent] = useState('');
  const [memorySaveStatus, setMemorySaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const memorySaveTimeoutRef = useRef<number | null>(null);

  // Load memories on mount and when showArchived changes
  useEffect(() => {
    const loadMemories = async () => {
      setIsLoadingMemories(true);
      try {
        const response = await fetchMemories(showArchived);
        setMemories(response.memories);
      } catch (error) {
        console.error('Failed to load memories:', error);
      } finally {
        setIsLoadingMemories(false);
      }
    };

    loadMemories();
  }, [showArchived]);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (memorySaveTimeoutRef.current) clearTimeout(memorySaveTimeoutRef.current);
    };
  }, []);

  const flashStatus = useCallback((status: 'saved' | 'error') => {
    setMemorySaveStatus(status);
    if (memorySaveTimeoutRef.current) clearTimeout(memorySaveTimeoutRef.current);
    memorySaveTimeoutRef.current = window.setTimeout(
      () => setMemorySaveStatus('idle'), status === 'saved' ? 2000 : 3000,
    );
  }, []);

  const handleCreateMemory = useCallback(async () => {
    if (!newMemoryContent.trim()) return;
    setMemorySaveStatus('saving');
    try {
      const memory = await createMemory(newMemoryContent);
      setMemories(prev => [memory, ...prev]);
      setNewMemoryContent('');
      setIsCreating(false);
      flashStatus('saved');
    } catch (error) {
      console.error('Failed to create memory:', error);
      flashStatus('error');
    }
  }, [newMemoryContent, flashStatus]);

  const handleUpdateMemory = useCallback(async (memoryId: string) => {
    if (!editingContent.trim()) return;
    setMemorySaveStatus('saving');
    try {
      const updated = await updateMemory(memoryId, editingContent);
      setMemories(prev => prev.map(m => m.id === memoryId ? updated : m));
      setEditingMemoryId(null);
      setEditingContent('');
      flashStatus('saved');
    } catch (error) {
      console.error('Failed to update memory:', error);
      flashStatus('error');
    }
  }, [editingContent, flashStatus]);

  const handleArchiveMemory = useCallback(async (memoryId: string) => {
    try {
      await archiveMemory(memoryId);
      if (showArchived) {
        setMemories(prev => prev.map(m => m.id === memoryId ? { ...m, archived: true } : m));
      } else {
        setMemories(prev => prev.filter(m => m.id !== memoryId));
      }
    } catch (error) {
      console.error('Failed to archive memory:', error);
    }
  }, [showArchived]);

  const handleUnarchiveMemory = useCallback(async (memoryId: string) => {
    try {
      const updated = await unarchiveMemory(memoryId);
      setMemories(prev => prev.map(m => m.id === memoryId ? updated : m));
    } catch (error) {
      console.error('Failed to unarchive memory:', error);
    }
  }, []);

  const handleDeleteMemory = useCallback(async (memoryId: string) => {
    if (!confirm('Are you sure you want to permanently delete this memory? This cannot be undone.')) {
      return;
    }
    try {
      await deleteMemory(memoryId);
      setMemories(prev => prev.filter(m => m.id !== memoryId));
    } catch (error) {
      console.error('Failed to delete memory:', error);
    }
  }, []);

  const startEditing = useCallback((memory: Memory) => {
    setEditingMemoryId(memory.id);
    setEditingContent(memory.content);
  }, []);

  const cancelEditing = useCallback(() => {
    setEditingMemoryId(null);
    setEditingContent('');
  }, []);

  const startCreating = useCallback(() => {
    setIsCreating(true);
    setNewMemoryContent('');
  }, []);

  const cancelCreating = useCallback(() => {
    setIsCreating(false);
    setNewMemoryContent('');
  }, []);

  const toggleExpanded = useCallback((memoryId: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev);
      if (next.has(memoryId)) next.delete(memoryId); else next.add(memoryId);
      return next;
    });
  }, []);

  // Inline filter: case-insensitive substring on the loaded list, so typing
  // narrows the table without a round trip.
  const visibleMemories = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return memories;
    return memories.filter(m => m.content.toLowerCase().includes(needle));
  }, [memories, filter]);

  const countLabel = filter.trim()
    ? `${visibleMemories.length} of ${memories.length}`
    : `${memories.length} ${memories.length === 1 ? 'memory' : 'memories'}`;

  return (
    <div className="settings-section">
      <h3>Memories</h3>
      <p className="settings-description">
        Notes and context the assistant uses to personalize your experience.
        Click a memory to expand it.
      </p>

      <div className="memories-toolbar">
        <label className="memories-search">
          <Search size={14} aria-hidden="true" />
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter memories..."
            aria-label="Filter memories"
          />
          {filter && (
            <button
              type="button"
              className="memories-search-clear"
              onClick={() => setFilter('')}
              aria-label="Clear filter"
            >
              <X size={12} />
            </button>
          )}
        </label>
        <span className="memories-count" aria-live="polite">{countLabel}</span>
        {memorySaveStatus === 'saved' && (
          <span className="settings-save-status saved">Saved</span>
        )}
        {memorySaveStatus === 'error' && (
          <span className="settings-save-status error">Failed to save</span>
        )}
        <label className="memories-archived-toggle">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
          />
          Show archived
        </label>
        <button
          className="memories-create-btn"
          onClick={startCreating}
          disabled={isCreating}
        >
          + New memory
        </button>
      </div>

      {isLoadingMemories ? (
        <div className="settings-loading">Loading memories...</div>
      ) : (
        <table className="memories-table">
          <thead>
            <tr>
              <th scope="col" className="memories-col-content">Memory</th>
              <th scope="col" className="memories-col-date">Updated</th>
              <th scope="col" className="memories-col-actions" aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {isCreating && (
              <tr className="memory-row memory-row-editing">
                <td colSpan={3}>
                  <MemoryEditor
                    label="New memory"
                    value={newMemoryContent}
                    onChange={setNewMemoryContent}
                    onSave={handleCreateMemory}
                    onCancel={cancelCreating}
                    saving={memorySaveStatus === 'saving'}
                    placeholder="What should the assistant remember?"
                  />
                </td>
              </tr>
            )}
            {visibleMemories.length === 0 && !isCreating && (
              <tr>
                <td colSpan={3} className="memories-empty">
                  {filter.trim()
                    ? `No memories match "${filter.trim()}".`
                    : showArchived
                      ? 'No memories found.'
                      : 'No memories yet. Click "+ New memory" to create one.'}
                </td>
              </tr>
            )}
            {visibleMemories.map((memory) => {
              const expanded = expandedIds.has(memory.id);
              if (editingMemoryId === memory.id) {
                return (
                  <tr key={memory.id} className="memory-row memory-row-editing">
                    <td colSpan={3}>
                      <MemoryEditor
                        label="Edit memory"
                        value={editingContent}
                        onChange={setEditingContent}
                        onSave={() => handleUpdateMemory(memory.id)}
                        onCancel={cancelEditing}
                        saving={memorySaveStatus === 'saving'}
                      />
                    </td>
                  </tr>
                );
              }
              return (
                <tr
                  key={memory.id}
                  className={`memory-row ${memory.archived ? 'memory-row-archived' : ''} ${expanded ? 'memory-row-expanded' : ''}`}
                >
                  <td className="memories-col-content">
                    <div
                      className="memory-row-content"
                      role="button"
                      tabIndex={0}
                      aria-expanded={expanded}
                      onClick={() => toggleExpanded(memory.id)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          toggleExpanded(memory.id);
                        }
                      }}
                    >
                      <span className="memory-row-text">{memory.content}</span>
                      {memory.archived && (
                        <span className="memory-badge-archived">Archived</span>
                      )}
                    </div>
                  </td>
                  <td className="memories-col-date" title={dateTooltip(memory)}>
                    {shortDate(memory.updated_at ?? memory.created_at)}
                  </td>
                  <td className="memories-col-actions">
                    <div className="memory-row-actions">
                      {!memory.archived && (
                        <button
                          className="memory-icon-btn"
                          onClick={() => startEditing(memory)}
                          aria-label="Edit"
                          title="Edit"
                        >
                          <Pencil size={14} />
                        </button>
                      )}
                      {memory.archived ? (
                        <button
                          className="memory-icon-btn memory-icon-btn-unarchive"
                          onClick={() => handleUnarchiveMemory(memory.id)}
                          aria-label="Unarchive"
                          title="Unarchive"
                        >
                          <ArchiveRestore size={14} />
                        </button>
                      ) : (
                        <button
                          className="memory-icon-btn memory-icon-btn-archive"
                          onClick={() => handleArchiveMemory(memory.id)}
                          aria-label="Archive"
                          title="Archive"
                        >
                          <Archive size={14} />
                        </button>
                      )}
                      <button
                        className="memory-icon-btn memory-icon-btn-delete"
                        onClick={() => handleDeleteMemory(memory.id)}
                        aria-label="Delete"
                        title="Delete"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
