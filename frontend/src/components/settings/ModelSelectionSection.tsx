import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { fetchModelSelection, updateModelSelection } from '../../api/client';
import type { ModelSelectionListResponse, ModelSelectionRow } from '../../api/types';
import type { ModelVisibility } from '../../constants/models';
import { useAppConfig } from '../../contexts/AppConfigContext';
import { SaveActions } from './ServiceCredentialsSection';
import type { SaveStatus } from './ServiceCredentialsSection';
import './ServiceCredentialsSection.css';
import './ModelSelectionSection.css';

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

/** The editable part of a row, keyed by model id in the draft. */
interface Draft {
  slot: number | null;
  publicSlot: number | null;
  descriptor: string;
  allowPrivate: boolean;
  allowPublic: boolean;
}

function draftOf(row: ModelSelectionRow): Draft {
  return {
    slot: row.slot,
    publicSlot: row.public_slot,
    descriptor: row.descriptor,
    allowPrivate: row.allow_private,
    allowPublic: row.allow_public,
  };
}

function sameDraft(a: Draft, b: Draft): boolean {
  return a.slot === b.slot
    && a.publicSlot === b.publicSlot
    && a.descriptor === b.descriptor
    && a.allowPrivate === b.allowPrivate
    && a.allowPublic === b.allowPublic;
}

function slotOf(draft: Draft, visibility: ModelVisibility): number | null {
  return visibility === 'public' ? draft.publicSlot : draft.slot;
}

function allowedIn(draft: Draft, visibility: ModelVisibility): boolean {
  return visibility === 'public' ? draft.allowPublic : draft.allowPrivate;
}

const UNAVAILABLE_LABELS: Record<NonNullable<ModelSelectionRow['unavailable_reason']>, string> = {
  not_configured: 'Not configured',
  failing: 'Failing health check',
};

/**
 * Static rendering of the composer's model menu for one visibility as the
 * draft would show it: the models slotted in that menu in slot order
 * (descriptor over model name, or the bare name when the descriptor is
 * empty) above the "All models" row. Reuses the real menu's class names
 * from ChatPanel.css so the preview is faithful; the wrapper undoes the
 * popover positioning.
 */
function ModelMenuPreview({
  title,
  visibility,
  rows,
  drafts,
}: {
  title: string;
  visibility: ModelVisibility;
  rows: ModelSelectionRow[];
  drafts: Record<string, Draft>;
}) {
  const picks = rows
    .filter((row) => drafts[row.id] && slotOf(drafts[row.id], visibility) !== null)
    .sort((a, b) => (slotOf(drafts[a.id], visibility) as number) - (slotOf(drafts[b.id], visibility) as number));
  return (
    <div className="model-selection-preview" aria-label={`${title} preview`}>
      <h5 className="model-selection-side-title">{title}</h5>
      <div className="model-menu" role="presentation">
        {picks.map((row) => {
          const draft = drafts[row.id];
          return (
            <div
              key={row.id}
              className={`model-menu-item${row.available ? '' : ' model-selection-preview-unavailable'}`}
              title={row.available ? undefined : `Hidden from users right now: ${UNAVAILABLE_LABELS[row.unavailable_reason ?? 'not_configured']}`}
            >
              <span className="model-menu-item-text">
                <span className="model-menu-item-label">{draft.descriptor || row.display_name}</span>
                {draft.descriptor && <span className="model-menu-item-sub">{row.display_name}</span>}
              </span>
            </div>
          );
        })}
        {picks.length > 0 && <div className="model-menu-divider" />}
        <div className="model-menu-item">
          <span className="model-menu-item-text">
            <span className="model-menu-item-label">All models</span>
          </span>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="9 18 15 12 9 6"></polyline>
          </svg>
        </div>
      </div>
      {picks.length === 0 && (
        <p className="model-selection-preview-note">
          No model has a slot — the menu opens straight onto "All models".
        </p>
      )}
    </div>
  );
}

/**
 * Admin-only table of every enabled model (one row each) with the per-model
 * selection settings persisted server-side in data/model_selection.json:
 * which top-level slot of the composer's model menu it occupies (if any)
 * and the descriptor shown there. While public projects are switched on
 * (Settings > Features), public-project conversations get their own top
 * level, so the table grows a second slot column plus the Private/Public
 * "allowed" checkboxes, and a second preview. Edits accumulate in a draft
 * and are saved as one full replacement; the previews render the draft.
 */
export function ModelSelectionSection() {
  const { refreshModelCatalog } = useAppConfig();
  const [data, setData] = useState<ModelSelectionListResponse | null>(null);
  const [loadError, setLoadError] = useState('');
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [saveError, setSaveError] = useState('');
  const statusTimeoutRef = useRef<number | null>(null);

  const applyResponse = useCallback((response: ModelSelectionListResponse) => {
    setData(response);
    setDrafts(Object.fromEntries(response.models.map((row) => [row.id, draftOf(row)])));
  }, []);

  useEffect(() => {
    const load = async () => {
      try {
        applyResponse(await fetchModelSelection());
      } catch (error) {
        console.error('Failed to load model selection:', error);
        setLoadError(errorMessage(error, 'Failed to load'));
      }
    };
    load();
    return () => {
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
    };
  }, [applyResponse]);

  const dirty = useMemo(
    () => data !== null && data.models.some((row) => !sameDraft(drafts[row.id], draftOf(row))),
    [data, drafts],
  );

  // Slots are unique per menu: giving a model a slot another model holds
  // swaps the two (the other model takes this one's previous slot, possibly
  // none), so nothing silently drops out of the top level.
  const setSlot = useCallback((modelId: string, visibility: ModelVisibility, slot: number | null) => {
    const field = visibility === 'public' ? 'publicSlot' : 'slot';
    setDrafts((prev) => {
      const previous = prev[modelId][field];
      const next = { ...prev, [modelId]: { ...prev[modelId], [field]: slot } };
      if (slot !== null) {
        const holder = Object.keys(prev).find((id) => id !== modelId && prev[id][field] === slot);
        if (holder) next[holder] = { ...prev[holder], [field]: previous };
      }
      return next;
    });
  }, []);

  const setDescriptor = useCallback((modelId: string, descriptor: string) => {
    setDrafts((prev) => ({ ...prev, [modelId]: { ...prev[modelId], descriptor } }));
  }, []);

  // Unticking a visibility also vacates that menu's slot (the server would
  // clear it anyway); re-ticking does not restore it.
  const setAllowed = useCallback((modelId: string, visibility: ModelVisibility, allowed: boolean) => {
    setDrafts((prev) => {
      const current = prev[modelId];
      const patch: Partial<Draft> = visibility === 'public'
        ? { allowPublic: allowed, publicSlot: allowed ? current.publicSlot : null }
        : { allowPrivate: allowed, slot: allowed ? current.slot : null };
      return { ...prev, [modelId]: { ...current, ...patch } };
    });
  }, []);

  const handleDiscard = useCallback(() => {
    if (data) applyResponse(data);
    setSaveError('');
    setSaveStatus('idle');
  }, [data, applyResponse]);

  const handleSave = useCallback(async () => {
    if (!data) return;
    setSaveStatus('saving');
    setSaveError('');
    try {
      const response = await updateModelSelection({
        models: data.models.map((row) => {
          const draft = drafts[row.id];
          return {
            id: row.id,
            slot: draft.slot,
            public_slot: draft.publicSlot,
            descriptor: draft.descriptor.trim(),
            allow_private: draft.allowPrivate,
            allow_public: draft.allowPublic,
          };
        }),
      });
      applyResponse(response);
      setSaveStatus('saved');
      // The composer menu in this tab reads the module-level catalog fed by
      // GET /app/api/config; re-fetch so the change shows without a reload.
      void refreshModelCatalog();
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      statusTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 2000);
    } catch (error) {
      console.error('Failed to save model selection:', error);
      setSaveStatus('error');
      setSaveError(errorMessage(error, 'Failed to save'));
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      statusTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 4000);
    }
  }, [data, drafts, applyResponse, refreshModelCatalog]);

  // Rows grouped by provider label, preserving server order (Vertex
  // families first, then each instance's models).
  const groups = useMemo(() => {
    if (!data) return [];
    const out: { label: string; rows: ModelSelectionRow[] }[] = [];
    for (const row of data.models) {
      const last = out[out.length - 1];
      if (last && last.label === row.provider_label) last.rows.push(row);
      else out.push({ label: row.provider_label, rows: [row] });
    }
    return out;
  }, [data]);

  const slotOptions = data ? Array.from({ length: data.max_slots }, (_, i) => i + 1) : [];
  const columnCount = 6;

  return (
    <div className="settings-section">
      <h3>Model Selection</h3>
      <p className="settings-description">
        How the enabled models are offered to users. Pin up to {data?.max_slots ?? 5} models
        to the top level of the composer's model menu with a descriptor of your choice
        (everything else stays under "All models"). Public-project conversations get their
        own top level, and each model can be allowed or blocked for private and for public
        conversations. Applies to all users. Admin only.
      </p>
      {loadError ? (
        <div className="svc-cred-load-error">{loadError}</div>
      ) : data === null ? (
        <div className="settings-loading">Loading...</div>
      ) : (
        <div className="model-selection-layout">
          <div className="model-selection-main">
            <table className="model-selection-table">
              <thead>
                <tr className="model-selection-header-groups">
                  <th className="model-selection-col-model" rowSpan={2}>Model</th>
                  <th className="model-selection-col-descriptor" rowSpan={2} title="Label shown for the model in the top level of either menu">Descriptor</th>
                  <th colSpan={2} className="model-selection-header-group" title="Standalone and project conversations, routines, Slack and API runs">Private conversations</th>
                  <th colSpan={2} className="model-selection-header-group" title="Public-project conversations (internet-enabled sandbox)">Public conversations</th>
                </tr>
                <tr>
                  <th className="model-selection-col-slot" title="Position in the top level of the private menu">Slot</th>
                  <th className="model-selection-col-check" title="May be used in private conversations">Allowed</th>
                  <th className="model-selection-col-slot" title="Position in the top level of the public menu">Slot</th>
                  <th className="model-selection-col-check" title="May be used in public-project conversations">Allowed</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => (
                  <GroupRows
                    key={group.label}
                    label={group.label}
                    rows={group.rows}
                    drafts={drafts}
                    columnCount={columnCount}
                    slotOptions={slotOptions}
                    maxDescriptorLength={data.max_descriptor_length}
                    busy={saveStatus === 'saving'}
                    onSlot={setSlot}
                    onDescriptor={setDescriptor}
                    onAllowed={setAllowed}
                  />
                ))}
              </tbody>
            </table>
            <p className="model-selection-note">
              A dot after a model's name means it is currently hidden from users — its
              provider is not configured (grey) or failing its health check (red); fix that
              in Inference Providers. Unticking Allowed also stops existing conversations of
              that kind from continuing on the model until they switch.
            </p>
            <div className="model-selection-actions">
              <SaveActions
                saveStatus={saveStatus}
                saveError={saveError}
                disabled={!dirty}
                onSave={handleSave}
              />
              {dirty && saveStatus !== 'saving' && (
                <button type="button" className="model-selection-discard-btn" onClick={handleDiscard}>
                  Discard changes
                </button>
              )}
            </div>
          </div>
          <aside className="model-selection-side">
            <ModelMenuPreview
              title={`Private menu${dirty ? ' (unsaved)' : ''}`}
              visibility="private"
              rows={data.models}
              drafts={drafts}
            />
            <ModelMenuPreview
              title={`Public menu${dirty ? ' (unsaved)' : ''}`}
              visibility="public"
              rows={data.models}
              drafts={drafts}
            />
          </aside>
        </div>
      )}
    </div>
  );
}

function SlotSelect({
  row,
  visibility,
  value,
  disabled,
  slotOptions,
  onChange,
}: {
  row: ModelSelectionRow;
  visibility: ModelVisibility;
  value: number | null;
  disabled: boolean;
  slotOptions: number[];
  onChange: (slot: number | null) => void;
}) {
  return (
    <select
      className="model-selection-slot-select"
      value={value ?? ''}
      disabled={disabled}
      aria-label={`${visibility === 'public' ? 'Public' : 'Private'} top-level slot for ${row.display_name}`}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
    >
      <option value="">—</option>
      {slotOptions.map((n) => (
        <option key={n} value={n}>{n}</option>
      ))}
    </select>
  );
}

function GroupRows({
  label,
  rows,
  drafts,
  columnCount,
  slotOptions,
  maxDescriptorLength,
  busy,
  onSlot,
  onDescriptor,
  onAllowed,
}: {
  label: string;
  rows: ModelSelectionRow[];
  drafts: Record<string, Draft>;
  columnCount: number;
  slotOptions: number[];
  maxDescriptorLength: number;
  busy: boolean;
  onSlot: (modelId: string, visibility: ModelVisibility, slot: number | null) => void;
  onDescriptor: (modelId: string, descriptor: string) => void;
  onAllowed: (modelId: string, visibility: ModelVisibility, allowed: boolean) => void;
}) {
  return (
    <>
      <tr className="model-selection-group-row">
        <th colSpan={columnCount} scope="rowgroup">{label}</th>
      </tr>
      {rows.map((row) => {
        const draft = drafts[row.id];
        const slotted = draft.slot !== null || draft.publicSlot !== null;
        const modelCell = (
          <td className="model-selection-col-model">
            <div className="model-selection-model-name">
              <span className="model-selection-model-name-text">{row.display_name}</span>
              {!row.available && row.unavailable_reason && (
                <span
                  className={`model-selection-status-dot ${row.unavailable_reason}`}
                  role="img"
                  aria-label={UNAVAILABLE_LABELS[row.unavailable_reason]}
                  title={`${UNAVAILABLE_LABELS[row.unavailable_reason]} — hidden from users until the provider works (see Inference Providers)`}
                />
              )}
            </div>
            <div className="model-selection-model-id" title={row.id}>{row.wire_id}</div>
          </td>
        );
        const descriptorCell = (
          <td className="model-selection-col-descriptor">
            <input
              type="text"
              className="svc-cred-input model-selection-descriptor-input"
              value={draft.descriptor}
              maxLength={maxDescriptorLength}
              disabled={busy}
              placeholder={slotted ? 'e.g. Smart ($$$)' : 'Shown when slotted'}
              aria-label={`Descriptor for ${row.display_name}`}
              onChange={(e) => onDescriptor(row.id, e.target.value)}
            />
          </td>
        );
        const slotCell = (visibility: ModelVisibility) => (
          <td className="model-selection-col-slot">
            <SlotSelect
              row={row}
              visibility={visibility}
              value={slotOf(draft, visibility)}
              disabled={busy || !allowedIn(draft, visibility)}
              slotOptions={slotOptions}
              onChange={(slot) => onSlot(row.id, visibility, slot)}
            />
          </td>
        );
        const allowedCell = (visibility: ModelVisibility) => (
          <td className="model-selection-col-check">
            <input
              type="checkbox"
              checked={allowedIn(draft, visibility)}
              disabled={busy}
              aria-label={`Allow ${row.display_name} in ${visibility} conversations`}
              onChange={(e) => onAllowed(row.id, visibility, e.target.checked)}
            />
          </td>
        );
        return (
          <tr key={row.id} className={`model-selection-row${row.available ? '' : ' unavailable'}`}>
            {modelCell}
            {descriptorCell}
            {slotCell('private')}
            {allowedCell('private')}
            {slotCell('public')}
            {allowedCell('public')}
          </tr>
        );
      })}
    </>
  );
}
