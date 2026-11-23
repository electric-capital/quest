import { useState, useEffect, useCallback } from 'react';
import { fetchSettings, updateSettings } from '../../api/client';
import { useSaveStatus } from '../../hooks/useSaveStatus';
import { SaveActions } from './SaveActions';
import './GmailSection.css';

// Mirrors backend limits in api/gmail/quest_labels.py.
const MAX_LABELS = 50;
const MAX_LABEL_NAME_LENGTH = 80;

export function GmailSection() {
  const [labels, setLabels] = useState<string[]>([]);
  const [newLabel, setNewLabel] = useState('');
  const [inputError, setInputError] = useState('');
  const [loading, setLoading] = useState(true);
  const { saveStatus, saveError, runSave } = useSaveStatus();

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        const response = await fetchSettings();
        setLabels(response.settings.gmail_labels ?? []);
      } catch (error) {
        console.error('Failed to load settings:', error);
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  const handleAdd = useCallback(() => {
    const name = newLabel.trim();
    if (!name) return;
    if (name.includes('/')) {
      setInputError("Label names cannot contain '/' — labels are nested under [Quest]/ automatically.");
      return;
    }
    if (name.length > MAX_LABEL_NAME_LENGTH) {
      setInputError(`Label names are limited to ${MAX_LABEL_NAME_LENGTH} characters.`);
      return;
    }
    if (labels.some((l) => l.toLowerCase() === name.toLowerCase())) {
      setInputError(`Label "${name}" is already in the list.`);
      return;
    }
    if (labels.length >= MAX_LABELS) {
      setInputError(`At most ${MAX_LABELS} labels are allowed.`);
      return;
    }
    setInputError('');
    setLabels((prev) => [...prev, name]);
    setNewLabel('');
  }, [newLabel, labels]);

  const handleRemove = useCallback((name: string) => {
    setLabels((prev) => prev.filter((l) => l !== name));
  }, []);

  const handleSave = useCallback(async () => {
    await runSave(async () => {
      await updateSettings({ gmail_labels: labels });
    }, 'Failed to save Gmail settings:');
  }, [labels, runSave]);

  return (
    <div className="settings-section">
      <h3>Gmail</h3>
      <p className="settings-description">
        Labels Quest is allowed to add to or remove from your emails. Each label appears in
        Gmail as <code>[Quest]/&lt;name&gt;</code> and is created automatically the first time
        Quest applies it. Quest can always archive messages (using <code>[Quest]/archived</code>),
        independent of this list.
      </p>
      {loading ? (
        <div className="settings-loading">Loading...</div>
      ) : (
        <>
          {labels.length === 0 ? (
            <p className="gmail-labels-empty">
              No labels configured. Quest will not be able to label emails until you add some.
            </p>
          ) : (
            <ul className="gmail-labels-list">
              {labels.map((name) => (
                <li key={name} className="gmail-label-row">
                  <span className="gmail-label-name">
                    <span className="gmail-label-prefix">[Quest]/</span>
                    {name}
                  </span>
                  <button
                    className="gmail-label-remove"
                    onClick={() => handleRemove(name)}
                    aria-label={`Remove label ${name}`}
                    title="Remove label"
                  >
                    &times;
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="gmail-label-add-row">
            <input
              type="text"
              className="gmail-label-input"
              placeholder="New label name (e.g. receipts)"
              value={newLabel}
              maxLength={MAX_LABEL_NAME_LENGTH}
              onChange={(e) => {
                setNewLabel(e.target.value);
                if (inputError) setInputError('');
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  handleAdd();
                }
              }}
            />
            <button
              className="gmail-label-add-button"
              onClick={handleAdd}
              disabled={!newLabel.trim()}
            >
              Add
            </button>
          </div>
          {inputError && <p className="gmail-label-input-error">{inputError}</p>}
          <SaveActions
            saveStatus={saveStatus}
            saveError={saveError}
            onSave={handleSave}
          />
        </>
      )}
    </div>
  );
}
