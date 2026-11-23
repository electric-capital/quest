export type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

/** The Save button of a settings section with its "Saved" / error note. */
export function SaveActions({
  saveStatus,
  saveError,
  disabled = false,
  onSave,
}: {
  saveStatus: SaveStatus;
  saveError: string;
  disabled?: boolean;
  onSave: () => void;
}) {
  return (
    <div className="settings-actions">
      <button
        className="settings-save-button"
        onClick={onSave}
        disabled={saveStatus === 'saving' || disabled}
      >
        {saveStatus === 'saving' ? 'Saving...' : 'Save'}
      </button>
      {saveStatus === 'saved' && (
        <span className="settings-save-status saved">Saved</span>
      )}
      {saveStatus === 'error' && (
        <span className="settings-save-status error">
          {saveError || 'Failed to save'}
        </span>
      )}
    </div>
  );
}
