import { useCallback, useEffect, useRef, useState } from 'react';
import type { SaveStatus } from '../components/settings/SaveActions';

const SAVED_MS = 2000;
const ERROR_MS = 3000;

/**
 * The status of a settings section's Save button: `saving` while the save
 * runs, then `saved` for two seconds or `error` (with the failure's message)
 * for three, then back to `idle`.
 *
 * `runSave(save, failureLog)` runs one save through that cycle. A failure is
 * logged to the console under `failureLog` and shown, never thrown.
 */
export function useSaveStatus() {
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [saveError, setSaveError] = useState('');
  const timeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  const runSave = useCallback(async (save: () => Promise<void>, failureLog: string) => {
    const settle = (status: SaveStatus, ms: number) => {
      setSaveStatus(status);
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      timeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), ms);
    };
    setSaveStatus('saving');
    setSaveError('');
    try {
      await save();
      settle('saved', SAVED_MS);
    } catch (error) {
      console.error(failureLog, error);
      setSaveError(error instanceof Error ? error.message : 'Failed to save');
      settle('error', ERROR_MS);
    }
  }, []);

  return { saveStatus, saveError, runSave };
}
