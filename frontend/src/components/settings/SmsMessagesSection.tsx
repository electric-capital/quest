import { useState, useEffect, useCallback } from 'react';
import { fetchSmsTemplates, updateSmsTemplates } from '../../api/client';
import type { SmsTemplate } from '../../api/types';
import { useSaveStatus } from '../../hooks/useSaveStatus';
import { SaveActions } from './SaveActions';
import './SmsMessagesSection.css';

// Mirrors backend limits in plugins/twilio/templates.py + upstream.py.
const MAX_TEMPLATES = 50;
const MAX_NAME_LENGTH = 60;
const MAX_BODY_LENGTH = 1600;

/**
 * Settings > SMS Messages: the user's pre-written texts for the Twilio
 * plugin's no-approval `twilio_send_self_sms` tool. When the admin has
 * not marked SMS as a trusted channel, these are the ONLY things Quest
 * can text to the user's phone, sent verbatim. Reads/writes go to the
 * plugin's own `/auth/twilio/templates` routes; the section is only
 * reachable while the Twilio connector is available (SettingsModal).
 */
export function SmsMessagesSection() {
  const [templates, setTemplates] = useState<SmsTemplate[]>([]);
  const [trusted, setTrusted] = useState(false);
  const [phoneNumber, setPhoneNumber] = useState<string | null>(null);
  const [newName, setNewName] = useState('');
  const [newBody, setNewBody] = useState('');
  const [inputError, setInputError] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const { saveStatus, saveError, runSave } = useSaveStatus();

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      setLoadError('');
      try {
        const response = await fetchSmsTemplates();
        setTemplates(response.templates ?? []);
        setTrusted(response.trusted_channel === true);
        setPhoneNumber(response.phone_number ?? null);
      } catch (error) {
        console.error('Failed to load SMS messages:', error);
        setLoadError(error instanceof Error ? error.message : 'Failed to load');
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  const handleAdd = useCallback(() => {
    const name = newName.trim();
    const body = newBody.trim();
    if (!name || !body) return;
    if (name.length > MAX_NAME_LENGTH) {
      setInputError(`Names are limited to ${MAX_NAME_LENGTH} characters.`);
      return;
    }
    if (body.length > MAX_BODY_LENGTH) {
      setInputError(`Messages are limited to ${MAX_BODY_LENGTH} characters.`);
      return;
    }
    if (templates.some((t) => t.name.toLowerCase() === name.toLowerCase())) {
      setInputError(`A message named "${name}" already exists.`);
      return;
    }
    if (templates.length >= MAX_TEMPLATES) {
      setInputError(`At most ${MAX_TEMPLATES} messages are allowed.`);
      return;
    }
    setInputError('');
    setTemplates((prev) => [...prev, { name, body }]);
    setNewName('');
    setNewBody('');
  }, [newName, newBody, templates]);

  const handleRemove = useCallback((name: string) => {
    setTemplates((prev) => prev.filter((t) => t.name !== name));
  }, []);

  const handleSave = useCallback(async () => {
    await runSave(async () => {
      const response = await updateSmsTemplates(templates);
      setTemplates(response.templates ?? []);
    }, 'Failed to save SMS messages:');
  }, [templates, runSave]);

  const canAdd = newName.trim().length > 0 && newBody.trim().length > 0;

  return (
    <div className="settings-section">
      <h3>SMS Messages</h3>
      <p className="settings-description">
        Pre-written texts Quest may send to your own verified phone number without asking.
        Quest sends them word for word. Texts to anyone else always require your approval.
      </p>
      {loading ? (
        <div className="settings-loading">Loading...</div>
      ) : loadError ? (
        <p className="sms-templates-error">{loadError}</p>
      ) : (
        <>
          <p className={`sms-trust-banner ${trusted ? 'trusted' : 'untrusted'}`}>
            {trusted
              ? 'Your admin marked SMS as a trusted channel: Quest can also text you free-form messages.'
              : 'SMS is not a trusted channel: Quest can only text you the messages listed here.'}
            {phoneNumber
              ? ` Texts go to ${phoneNumber}.`
              : ' Verify your phone number under Data Connections to receive texts.'}
          </p>
          {templates.length === 0 ? (
            <p className="sms-templates-empty">
              No messages yet. {trusted ? '' : 'Quest cannot text you until you add one.'}
            </p>
          ) : (
            <ul className="sms-templates-list">
              {templates.map((t) => (
                <li key={t.name} className="sms-template-row">
                  <div className="sms-template-text">
                    <span className="sms-template-name">{t.name}</span>
                    <span className="sms-template-body">{t.body}</span>
                  </div>
                  <button
                    className="sms-template-remove"
                    onClick={() => handleRemove(t.name)}
                    aria-label={`Remove message ${t.name}`}
                    title="Remove message"
                  >
                    &times;
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="sms-template-add">
            <input
              type="text"
              className="sms-template-input"
              placeholder="Name (e.g. Deploy done)"
              value={newName}
              maxLength={MAX_NAME_LENGTH}
              onChange={(e) => {
                setNewName(e.target.value);
                if (inputError) setInputError('');
              }}
            />
            <textarea
              className="sms-template-textarea"
              placeholder="Message text, sent exactly as written"
              value={newBody}
              maxLength={MAX_BODY_LENGTH}
              rows={2}
              onChange={(e) => {
                setNewBody(e.target.value);
                if (inputError) setInputError('');
              }}
            />
            <div className="sms-template-add-actions">
              <span className="sms-template-count">
                {newBody.trim().length}/{MAX_BODY_LENGTH}
              </span>
              <button
                className="sms-template-add-button"
                onClick={handleAdd}
                disabled={!canAdd}
              >
                Add
              </button>
            </div>
          </div>
          {inputError && <p className="sms-templates-error">{inputError}</p>}
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
