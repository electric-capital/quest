/**
 * Modal for editing an existing routine's settings (name, prompt, guide, model,
 * schedule) and for deleting it.
 */

import { useState, useEffect, useLayoutEffect, useCallback, useRef } from 'react';
import {
  fetchRoutine,
  updateRoutine,
  deleteRoutine,
  fetchRoutineSchedule,
  createRoutineSchedule,
  updateRoutineSchedule,
  fetchRoutineAutoloadedSkillIds,
  setRoutineSkillAutoload,
  fetchSkills,
  fetchSharedWithMeSkills,
  ApiClientError,
} from '../api/client';
import type { Routine, Guide, RoutineSchedule, Skill } from '../api/types';
import { useConversationContext } from '../contexts/ConversationContext';
import { getSelectableModels, DEPRECATED_MODEL_MAP, getModelDisplayName } from '../constants/models';
import { ModalShell } from './ModalShell';
import { RoutineCostsSection } from './RoutineCostsSection';
import { useAnimatedHeight } from '../hooks/useAnimatedHeight';
import './RoutineSettingsModal.css';
import './settings/SkillsSection.css';

type RoutineSettingsSection = 'prompt' | 'schedule' | 'skills' | 'costs' | 'delete';
type RoutineSkillsTab = 'my-skills' | 'shared-with-me';

function renderRoutineSkillCard(
  skill: Skill,
  showCreator: boolean,
  autoloadedIds: Set<string>,
  onToggle: (skillId: string) => void,
) {
  return (
    <div key={skill.id} className="memory-card skill-card">
      <div className="skill-card-header">
        <span className="skill-card-name">{skill.name}</span>
        <span className={`skill-badge-${skill.visibility}`}>
          {skill.visibility}
        </span>
        <div className="skill-enabled-toggle">
          <label title="Auto-loaded skills are automatically included in every conversation this routine creates">
            <input
              type="checkbox"
              checked={autoloadedIds.has(skill.id)}
              onChange={() => onToggle(skill.id)}
            />
            {' '}Auto-load
          </label>
        </div>
      </div>
      {showCreator && (
        <div className="skill-card-creator">
          By {skill.creator_name || skill.creator_email || 'Unknown'}
        </div>
      )}
      {skill.description && (
        <div className="skill-card-description">{skill.description}</div>
      )}
      <div className="guide-card-preview">
        {skill.content
          ? (skill.content.length > 150 ? skill.content.substring(0, 150) + '...' : skill.content)
          : <span className="guide-empty-hint">No content</span>}
      </div>
    </div>
  );
}

interface RoutineSettingsModalProps {
  isOpen: boolean;
  projectId: string | null;
  routine: Routine | null;
  onClose: () => void;
  onRoutineUpdated: () => void;
  onRoutineDeleted: () => void;
  /** Open one of the runs listed in the Costs section (its conversation). */
  onOpenRunConversation?: (conversationId: string) => void;
}

export function RoutineSettingsModal({
  isOpen,
  projectId,
  routine,
  onClose,
  onRoutineUpdated,
  onRoutineDeleted,
  onOpenRunConversation,
}: RoutineSettingsModalProps) {
  const { guides, enabledFeatures } = useConversationContext();
  // While the admin `guides` feature gate is closed the guide list is not
  // loaded and a leftover override is ignored at run time; the block below
  // still shows so the user can clear it.
  const guidesEnabled = enabledFeatures.includes('guides');

  // Authoritative routine row for this modal session. The `routine` prop
  // comes from the Sidebar's cached routines list, which can be stale when
  // the routine was changed outside the UI (e.g. an approved edit_routine
  // action request). On open we seed from the prop for an instant paint,
  // then refetch the row and rehydrate from the server's copy.
  const [loadedRoutine, setLoadedRoutine] = useState<Routine | null>(null);

  // Form state
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [guideId, setGuideId] = useState<string | null>(null);
  const [model, setModel] = useState<string>('gemini-3.5-flash-lite');

  // Schedule state
  const [editingSchedule, setEditingSchedule] = useState<RoutineSchedule | null>(null);
  const [scheduleType, setScheduleType] = useState<'daily' | 'hourly' | 'every_n_minutes'>('daily');
  const [dailyTimeLocal, setDailyTimeLocal] = useState('09:00');
  const [hourlyMinute, setHourlyMinute] = useState(0);
  const [intervalMinutes, setIntervalMinutes] = useState(30);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleLoading, setScheduleLoading] = useState(false);

  // Section navigation state
  const [activeSection, setActiveSection] = useState<RoutineSettingsSection>('prompt');
  // The Costs section refetches on every visit (it remounts); false from
  // the moment it is opened until that fetch settles.
  const [costsLoaded, setCostsLoaded] = useState(false);

  // Skills state (auto-loaded skill ids on this routine, plus lazy-loaded lists for picker tabs)
  const [routineAutoloadedSkillIds, setRoutineAutoloadedSkillIds] = useState<Set<string>>(new Set());
  const [skillsTab, setSkillsTab] = useState<RoutineSkillsTab>('my-skills');
  const [mySkillsList, setMySkillsList] = useState<Skill[]>([]);
  const [mySkillsLoaded, setMySkillsLoaded] = useState(false);
  const [isLoadingMySkills, setIsLoadingMySkills] = useState(false);
  const [sharedSkillsList, setSharedSkillsList] = useState<Skill[]>([]);
  const [sharedSkillsLoaded, setSharedSkillsLoaded] = useState(false);
  const [isLoadingSharedSkills, setIsLoadingSharedSkills] = useState(false);

  // The dialog's height follows the active section's content, so switching
  // sections (or data landing in one) glides it instead of snapping. While a
  // section is showing a loading placeholder the height is held, so the
  // dialog doesn't shrink around the placeholder and grow again moments
  // later when the real content arrives.
  const modalRef = useRef<HTMLDivElement>(null);
  const sectionLoading =
    (activeSection === 'costs' && !costsLoaded)
    || (activeSection === 'schedule' && scheduleLoading)
    || (activeSection === 'skills' && (skillsTab === 'my-skills' ? isLoadingMySkills : isLoadingSharedSkills));
  const contentInnerRef = useAnimatedHeight(modalRef, {
    hold: sectionLoading,
    revision: `${activeSection}:${skillsTab}`,
  });

  // Save/delete state
  const [isSaving, setIsSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saved' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const saveTimeoutRef = useRef<number | null>(null);
  const promptTextareaRef = useRef<HTMLTextAreaElement>(null);

  // Optimistic-concurrency tokens captured at modal open / last successful
  // hydration. Stored in refs because handleSave reads them on every call
  // and we don't want save callers re-bound when tokens shift.
  const routineTokenRef = useRef<string | null>(null);
  const scheduleTokenRef = useRef<string | null>(null);

  // When a save hits 409 stale_update, we surface a discard/overwrite
  // dialog rather than the generic red error div. `current` is the server's
  // fresh row payload (delivered in the 409 body) so we can rehydrate the
  // form without a second GET.
  const [conflict, setConflict] = useState<
    | { kind: 'routine'; current: Routine; message: string }
    | { kind: 'schedule'; current: RoutineSchedule; message: string }
    | null
  >(null);

  // Load routine data and schedule when modal opens
  useEffect(() => {
    if (isOpen && routine && projectId) {
      const hydrateRoutine = (row: Routine) => {
        setLoadedRoutine(row);
        setName(row.name);
        setPrompt(row.prompt);
        setGuideId(row.guide_id);
        const rawModel = row.model || 'gemini-3.5-flash-lite';
        setModel(DEPRECATED_MODEL_MAP[rawModel] || rawModel);
        // Capture the routine's optimistic-concurrency baseline.
        routineTokenRef.current = row.updated_at ?? null;
      };

      hydrateRoutine(routine);
      setError(null);
      setSaveStatus('idle');
      setShowDeleteConfirm(false);
      setActiveSection('prompt');
      setConflict(null);

      // The prop row comes from the Sidebar's cached routines list and can
      // be stale (e.g. after an approved edit_routine action request), so
      // refetch the row and rehydrate from the server's copy.
      let cancelled = false;
      fetchRoutine(projectId, routine.id)
        .then((fresh) => {
          if (cancelled) return;
          hydrateRoutine(fresh);
          // The cached sidebar list drifted from the server -- refresh it so
          // the routine row matches what this dialog now shows.
          if (fresh.updated_at !== routine.updated_at) {
            onRoutineUpdated();
          }
        })
        .catch((err) => {
          console.error('Failed to refresh routine:', err);
        });

      // Reset skills picker state on each open so a previously cached
      // list from a different routine never leaks across rows.
      setRoutineAutoloadedSkillIds(new Set());
      setSkillsTab('my-skills');
      setMySkillsLoaded(false);
      setSharedSkillsLoaded(false);
      setMySkillsList([]);
      setSharedSkillsList([]);

      fetchRoutineAutoloadedSkillIds(projectId, routine.id)
        .then((response) => {
          setRoutineAutoloadedSkillIds(new Set(response.skill_ids));
        })
        .catch((err) => {
          console.error('Failed to load routine auto-loaded skills:', err);
        });

      // Reset schedule state to defaults first
      setEditingSchedule(null);
      setScheduleEnabled(false);
      setScheduleType('daily');
      setDailyTimeLocal('09:00');
      setHourlyMinute(0);
      setIntervalMinutes(30);
      scheduleTokenRef.current = null;

      // Load schedule
      setScheduleLoading(true);
      fetchRoutineSchedule(projectId, routine.id)
        .then((schedule) => {
          if (schedule) {
            setEditingSchedule(schedule);
            setScheduleEnabled(schedule.is_enabled);
            setScheduleType(schedule.schedule_type);
            if (schedule.daily_time_local) setDailyTimeLocal(schedule.daily_time_local);
            if (schedule.hourly_minute !== null) setHourlyMinute(schedule.hourly_minute);
            if (schedule.interval_minutes !== null) setIntervalMinutes(schedule.interval_minutes);
            scheduleTokenRef.current = schedule.updated_at ?? null;
          }
        })
        .catch((err) => {
          console.error('Failed to load routine schedule:', err);
        })
        .finally(() => setScheduleLoading(false));

      // Ignore an in-flight routine refetch that resolves after the modal
      // closed or switched to a different routine (it would clobber the
      // newer session's form state).
      return () => {
        cancelled = true;
      };
    }
  }, [isOpen, routine, projectId]);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (saveTimeoutRef.current) clearTimeout(saveTimeoutRef.current);
    };
  }, []);

  // Lazy-load the My Skills picker list when the Skills tab is opened.
  useEffect(() => {
    if (!isOpen) return;
    if (activeSection !== 'skills') return;
    if (skillsTab !== 'my-skills') return;
    if (mySkillsLoaded) return;

    setIsLoadingMySkills(true);
    fetchSkills(true)
      .then((response) => {
        setMySkillsList(response.skills);
        setMySkillsLoaded(true);
      })
      .catch((err) => {
        console.error('Failed to load my skills:', err);
      })
      .finally(() => setIsLoadingMySkills(false));
  }, [isOpen, activeSection, skillsTab, mySkillsLoaded]);

  // Lazy-load the Shared with Me picker list when that tab is opened.
  useEffect(() => {
    if (!isOpen) return;
    if (activeSection !== 'skills') return;
    if (skillsTab !== 'shared-with-me') return;
    if (sharedSkillsLoaded) return;

    setIsLoadingSharedSkills(true);
    fetchSharedWithMeSkills()
      .then((response) => {
        setSharedSkillsList(response.skills);
        setSharedSkillsLoaded(true);
      })
      .catch((err) => {
        console.error('Failed to load shared skills:', err);
      })
      .finally(() => setIsLoadingSharedSkills(false));
  }, [isOpen, activeSection, skillsTab, sharedSkillsLoaded]);

  const handleToggleRoutineAutoload = useCallback((skillId: string) => {
    if (!projectId || !routine) return;
    setRoutineAutoloadedSkillIds(prev => {
      const next = new Set(prev);
      const newEnabled = !next.has(skillId);
      if (newEnabled) {
        next.add(skillId);
      } else {
        next.delete(skillId);
      }
      setRoutineSkillAutoload(projectId, routine.id, skillId, newEnabled).catch(() => {});
      return next;
    });
  }, [projectId, routine]);

  // Auto-resize prompt textarea on input
  const handlePromptChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setPrompt(e.target.value);

    // Auto-resize to fit content, up to ~20 lines
    const target = e.target;
    target.style.height = 'auto';
    const maxHeight = 20 * 1.5 * 14; // ~20 lines * line-height(1.5) * font-size(14px) = 420px
    const newHeight = Math.min(target.scrollHeight, maxHeight);
    target.style.height = `${newHeight}px`;
  }, []);

  // Auto-size textarea on initial load and when navigating back to prompt
  // section. Layout effect so the dialog never paints a 4-row textarea and
  // then jumps to the grown one.
  useLayoutEffect(() => {
    if (isOpen && promptTextareaRef.current && prompt) {
      const el = promptTextareaRef.current;
      el.style.height = 'auto';
      const maxHeight = 20 * 1.5 * 14; // ~420px
      const newHeight = Math.min(el.scrollHeight, maxHeight);
      el.style.height = `${newHeight}px`;
    }
  }, [isOpen, prompt, activeSection]);

  const handleSave = useCallback(async () => {
    if (!projectId || !routine || isSaving) return;

    const trimmedName = name.trim();
    const trimmedPrompt = prompt.trim();
    if (!trimmedName || !trimmedPrompt) {
      setError('Name and prompt cannot be empty');
      return;
    }

    setIsSaving(true);
    setError(null);
    setConflict(null);
    setSaveStatus('idle');

    // Detect changes for guide and model, against the freshest row we have
    // (the refetched copy when available, else the prop).
    const baseline = loadedRoutine ?? routine;
    const guideChanged = baseline.guide_id !== guideId;
    const modelChanged = baseline.model !== model;

    const updates: {
      name?: string;
      prompt?: string;
      guide_id?: string | null;
      clear_guide?: boolean;
      model?: string | null;
      clear_model?: boolean;
      expected_updated_at?: string | null;
    } = {
      name: trimmedName,
      prompt: trimmedPrompt,
      expected_updated_at: routineTokenRef.current,
    };

    if (guideChanged) {
      if (guideId === null) {
        updates.clear_guide = true;
      } else {
        updates.guide_id = guideId;
      }
    }

    if (modelChanged) {
      if (model === null) {
        updates.clear_model = true;
      } else {
        updates.model = model;
      }
    }

    try {
      const updatedRoutine = await updateRoutine(projectId, routine.id, updates);
      // Refresh the baseline so a follow-up save in the same modal session
      // does not 409 against the row we just wrote.
      routineTokenRef.current = updatedRoutine.updated_at ?? null;
      setLoadedRoutine(updatedRoutine);

      // Save schedule changes
      if (scheduleEnabled) {
        // Schedule is enabled -- create or update the schedule record
        const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        const baseScheduleData = {
          schedule_type: scheduleType,
          ...(scheduleType === 'daily' ? { daily_time_local: dailyTimeLocal, timezone: userTimezone } : {}),
          ...(scheduleType === 'hourly' ? { hourly_minute: hourlyMinute } : {}),
          ...(scheduleType === 'every_n_minutes' ? { interval_minutes: intervalMinutes } : {}),
          is_enabled: true,
        };

        if (editingSchedule) {
          const updatedSchedule = await updateRoutineSchedule(projectId, routine.id, {
            ...baseScheduleData,
            expected_updated_at: scheduleTokenRef.current,
          });
          scheduleTokenRef.current = updatedSchedule.updated_at ?? null;
        } else {
          // POST: no expected_updated_at needed (the duplicate-schedule case
          // is already handled by the existing 400 path on the server).
          const createdSchedule = await createRoutineSchedule(projectId, routine.id, baseScheduleData);
          scheduleTokenRef.current = createdSchedule.updated_at ?? null;
        }
      } else if (editingSchedule) {
        // Schedule exists but user disabled it -- update with is_enabled: false
        const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        const updatedSchedule = await updateRoutineSchedule(projectId, routine.id, {
          schedule_type: scheduleType,
          ...(scheduleType === 'daily' ? { daily_time_local: dailyTimeLocal, timezone: userTimezone } : {}),
          ...(scheduleType === 'hourly' ? { hourly_minute: hourlyMinute } : {}),
          ...(scheduleType === 'every_n_minutes' ? { interval_minutes: intervalMinutes } : {}),
          is_enabled: false,
          expected_updated_at: scheduleTokenRef.current,
        });
        scheduleTokenRef.current = updatedSchedule.updated_at ?? null;
      }

      setSaveStatus('saved');
      if (saveTimeoutRef.current) clearTimeout(saveTimeoutRef.current);
      saveTimeoutRef.current = window.setTimeout(() => setSaveStatus('idle'), 2000);

      onRoutineUpdated();
      onClose();
    } catch (err) {
      // 409 stale_update gets the discard/overwrite dialog rather than a
      // red error banner. The server includes the fresh row in `current`
      // so we can rehydrate without a second GET.
      if (
        err instanceof ApiClientError &&
        err.statusCode === 409 &&
        err.errorCode === 'stale_update' &&
        err.details &&
        err.details['current']
      ) {
        const current = err.details['current'] as Record<string, unknown>;
        // The routine PUT runs first; if it succeeded we know the conflict
        // is on the schedule (it has a routine_id field, the routine row
        // does not).
        const isSchedule = 'routine_id' in current;
        setConflict(
          isSchedule
            ? {
                kind: 'schedule',
                current: current as unknown as RoutineSchedule,
                message: err.message,
              }
            : {
                kind: 'routine',
                current: current as unknown as Routine,
                message: err.message,
              },
        );
        setSaveStatus('error');
      } else if (err instanceof ApiClientError) {
        setError(err.message);
        setSaveStatus('error');
      } else {
        setError('Failed to save routine');
        setSaveStatus('error');
      }
    } finally {
      setIsSaving(false);
    }
  }, [projectId, routine, loadedRoutine, name, prompt, guideId, model, isSaving, scheduleType, dailyTimeLocal, hourlyMinute, intervalMinutes, scheduleEnabled, editingSchedule, onRoutineUpdated, onClose]);

  // Conflict handlers. Discard rehydrates form state from the server's
  // current row and clears the dialog. Overwrite adopts the server's token
  // as the new baseline and re-attempts the save -- which will succeed and
  // wipe the racing edit (the user's explicit choice).
  const handleConflictDiscard = useCallback(() => {
    if (!conflict) return;
    if (conflict.kind === 'routine') {
      const current = conflict.current;
      setLoadedRoutine(current);
      setName(current.name);
      setPrompt(current.prompt);
      setGuideId(current.guide_id);
      const rawModel = current.model || 'gemini-3.5-flash-lite';
      setModel(DEPRECATED_MODEL_MAP[rawModel] || rawModel);
      routineTokenRef.current = current.updated_at ?? null;
    } else {
      const current = conflict.current;
      setEditingSchedule(current);
      setScheduleEnabled(current.is_enabled);
      setScheduleType(current.schedule_type);
      if (current.daily_time_local) setDailyTimeLocal(current.daily_time_local);
      if (current.hourly_minute !== null) setHourlyMinute(current.hourly_minute);
      if (current.interval_minutes !== null) setIntervalMinutes(current.interval_minutes);
      scheduleTokenRef.current = current.updated_at ?? null;
    }
    setConflict(null);
    setSaveStatus('idle');
  }, [conflict]);

  const handleConflictOverwrite = useCallback(() => {
    if (!conflict) return;
    // Adopt the server's current token as the new baseline so the next PUT
    // is accepted. The user's in-memory form values are kept as-is.
    if (conflict.kind === 'routine') {
      routineTokenRef.current = conflict.current.updated_at ?? null;
    } else {
      scheduleTokenRef.current = conflict.current.updated_at ?? null;
    }
    setConflict(null);
    // handleSave reads tokens from refs, so this picks up the new value.
    void handleSave();
  }, [conflict, handleSave]);

  const handleDelete = useCallback(async () => {
    if (!projectId || !routine || isDeleting) return;

    setIsDeleting(true);
    setError(null);

    try {
      await deleteRoutine(projectId, routine.id);
      onRoutineDeleted();
      onClose();
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to delete routine');
      }
    } finally {
      setIsDeleting(false);
      setShowDeleteConfirm(false);
    }
  }, [projectId, routine, isDeleting, onRoutineDeleted, onClose]);

  const closeConflict = useCallback(() => setConflict(null), []);

  // While the conflict modal is layered on top, Escape closes only the
  // conflict modal (keeping the user's unsaved edits in the settings modal
  // underneath); otherwise it closes the settings modal.
  const handleEscape = useCallback(() => {
    if (conflict) {
      setConflict(null);
    } else {
      onClose();
    }
  }, [conflict, onClose]);

  if (!isOpen || !projectId || !routine) return null;

  return (
    <>
    <ModalShell
      isOpen
      onClose={onClose}
      onEscape={handleEscape}
      overlayClassName="routine-settings-overlay"
      modalClassName="routine-settings-modal"
      modalRef={modalRef}
    >
      <div className="routine-settings-header">
        <h2>Routine Settings</h2>
        <button className="routine-settings-close-button" onClick={onClose}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>

      <div className="routine-settings-body">
        {/* Left nav */}
        <nav className="routine-settings-nav">
          <div className="routine-settings-nav-top">
            <button
              className={`routine-settings-nav-item ${activeSection === 'prompt' ? 'active' : ''}`}
              onClick={() => setActiveSection('prompt')}
            >
              Prompt
            </button>
            <button
              className={`routine-settings-nav-item ${activeSection === 'schedule' ? 'active' : ''}`}
              onClick={() => setActiveSection('schedule')}
            >
              Schedule
            </button>
            <button
              className={`routine-settings-nav-item ${activeSection === 'skills' ? 'active' : ''}`}
              onClick={() => setActiveSection('skills')}
            >
              Skills
            </button>
            <button
              className={`routine-settings-nav-item ${activeSection === 'costs' ? 'active' : ''}`}
              onClick={() => {
                if (activeSection !== 'costs') setCostsLoaded(false);
                setActiveSection('costs');
              }}
            >
              Costs
            </button>
          </div>
          <div className="routine-settings-nav-bottom">
            <button
              className={`routine-settings-nav-item routine-settings-nav-danger ${activeSection === 'delete' ? 'active' : ''}`}
              onClick={() => setActiveSection('delete')}
            >
              Delete
            </button>
          </div>
        </nav>

        {/* Right content. The inner wrapper's box is the content's natural
            height (the scrolling outer is sized by the dialog), which is
            what the height glide watches. */}
        <div className="routine-settings-content">
          <div className="routine-settings-content-inner" ref={contentInnerRef}>
          {activeSection === 'prompt' ? (
            <>
              {/* Name */}
              <div className="routine-settings-field">
                <label htmlFor="routine-settings-name" className="routine-settings-label">
                  Name
                </label>
                <input
                  id="routine-settings-name"
                  type="text"
                  className="routine-settings-input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={100}
                  disabled={isSaving}
                />
              </div>

              {/* Prompt (auto-growing textarea) */}
              <div className="routine-settings-field">
                <label htmlFor="routine-settings-prompt" className="routine-settings-label">
                  Prompt
                </label>
                <textarea
                  ref={promptTextareaRef}
                  id="routine-settings-prompt"
                  className="routine-settings-textarea"
                  value={prompt}
                  onChange={handlePromptChange}
                  rows={4}
                  disabled={isSaving}
                />
              </div>

              {/* Guide Override (deprecated): read-only leftover display.
                  Guides can no longer be attached to routines; a routine
                  that still has one shows it here with a Clear button
                  (applied on save via clear_guide). */}
              {(loadedRoutine ?? routine).guide_id && (
                <div className="routine-settings-field">
                  <label className="routine-settings-label">
                    Guide Override
                    <span className="routine-settings-label-hint">
                      {guidesEnabled
                        ? 'Guides are deprecated; this routine still has one attached'
                        : 'Guides are disabled on this server; the attached guide is ignored when this routine runs'}
                    </span>
                  </label>
                  {guideId ? (
                    <div className="routine-settings-guide-readonly">
                      <span className="routine-settings-guide-name">
                        {guidesEnabled
                          ? guides.find((g: Guide) => g.id === guideId)?.name || 'Unknown guide'
                          : 'Guide (disabled)'}
                      </span>
                      <button
                        type="button"
                        className="routine-settings-guide-clear-button"
                        onClick={() => setGuideId(null)}
                        disabled={isSaving}
                      >
                        Clear
                      </button>
                    </div>
                  ) : (
                    <span className="routine-settings-label-hint">
                      Guide will be removed when you save.
                    </span>
                  )}
                </div>
              )}

              {/* Model */}
              <div className="routine-settings-field">
                <label htmlFor="routine-settings-model" className="routine-settings-label">
                  Model
                </label>
                <select
                  id="routine-settings-model"
                  className="routine-settings-select"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  disabled={isSaving}
                >
                  {/* Keep the routine's current model selectable when it is
                      deprecated (no longer offered for new picks) so saving
                      unrelated edits doesn't silently switch the model. */}
                  {model && !getSelectableModels().some((m) => m.id === model) && (
                    <option value={model}>{getModelDisplayName(model)} (deprecated)</option>
                  )}
                  {getSelectableModels().map((m) => (
                    <option key={m.id} value={m.id}>{m.name}</option>
                  ))}
                </select>
              </div>

              {/* Error */}
              {error && <div className="routine-settings-error">{error}</div>}

              {/* Save button */}
              <div className="routine-settings-actions">
                <button
                  className="routine-settings-save-button"
                  onClick={handleSave}
                  disabled={isSaving || !name.trim() || !prompt.trim()}
                >
                  {isSaving ? 'Saving...' : saveStatus === 'saved' ? 'Saved!' : 'Save Changes'}
                </button>
              </div>
            </>
          ) : activeSection === 'schedule' ? (
            <>
              {/* Schedule section content */}
              {scheduleLoading ? (
                <div className="routine-settings-loading">Loading schedule...</div>
              ) : (
                <>
                  {/* Single "Run on a schedule" checkbox */}
                  <div className="routine-settings-field">
                    <label className="routine-settings-label">
                      <input
                        type="checkbox"
                        checked={scheduleEnabled}
                        onChange={(e) => setScheduleEnabled(e.target.checked)}
                        disabled={isSaving}
                      />
                      {' '}Run on a schedule
                    </label>
                  </div>

                  {/* Schedule config -- always visible, disabled when schedule is off */}
                  <div className={`routine-settings-schedule-fields ${!scheduleEnabled ? 'disabled' : ''}`}>
                    <div className="routine-settings-field">
                      <label className="routine-settings-label">Schedule Type</label>
                      <select
                        className="routine-settings-select"
                        value={scheduleType}
                        onChange={(e) => setScheduleType(e.target.value as 'daily' | 'hourly' | 'every_n_minutes')}
                        disabled={isSaving || !scheduleEnabled}
                      >
                        <option value="daily">Daily</option>
                        <option value="hourly">Hourly</option>
                        <option value="every_n_minutes">Every N minutes</option>
                      </select>
                    </div>

                    {scheduleType === 'daily' && (
                      <div className="routine-settings-field">
                        <label className="routine-settings-label">
                          Time (your local timezone)
                        </label>
                        <input
                          type="time"
                          className="routine-settings-input"
                          value={dailyTimeLocal}
                          onChange={(e) => setDailyTimeLocal(e.target.value)}
                          disabled={isSaving || !scheduleEnabled}
                        />
                      </div>
                    )}

                    {scheduleType === 'hourly' && (
                      <div className="routine-settings-field">
                        <label className="routine-settings-label">
                          Minute of each hour (0-59)
                        </label>
                        <input
                          type="number"
                          className="routine-settings-input"
                          min={0}
                          max={59}
                          value={hourlyMinute}
                          onChange={(e) => setHourlyMinute(parseInt(e.target.value) || 0)}
                          disabled={isSaving || !scheduleEnabled}
                        />
                      </div>
                    )}

                    {scheduleType === 'every_n_minutes' && (
                      <div className="routine-settings-field">
                        <label className="routine-settings-label">
                          Run every N minutes
                        </label>
                        <input
                          type="number"
                          className="routine-settings-input"
                          min={1}
                          max={1440}
                          value={intervalMinutes}
                          onChange={(e) => setIntervalMinutes(parseInt(e.target.value) || 30)}
                          disabled={isSaving || !scheduleEnabled}
                        />
                        <span className="routine-settings-label-hint">
                          If the previous run is still in progress, the scheduled run will be skipped.
                        </span>
                      </div>
                    )}

                    {/* Last run info */}
                    {editingSchedule && editingSchedule.last_run_completed_at && (
                      <div className="routine-settings-last-run">
                        Last run: {new Date(editingSchedule.last_run_completed_at).toLocaleString()}
                        {editingSchedule.is_running && ' (currently running)'}
                      </div>
                    )}
                  </div>
                </>
              )}

              {/* Error */}
              {error && <div className="routine-settings-error">{error}</div>}

              {/* Save button */}
              <div className="routine-settings-actions">
                <button
                  className="routine-settings-save-button"
                  onClick={handleSave}
                  disabled={isSaving || !name.trim() || !prompt.trim()}
                >
                  {isSaving ? 'Saving...' : saveStatus === 'saved' ? 'Saved!' : 'Save Changes'}
                </button>
              </div>
            </>
          ) : activeSection === 'skills' ? (
            <div className="settings-section">
              <p className="settings-description">
                Auto-loaded skills are automatically included in every conversation this routine creates, on top of your user and project auto-loads.
              </p>

              <div className="skills-tab-bar">
                <button
                  className={`skills-tab-btn${skillsTab === 'my-skills' ? ' active' : ''}`}
                  onClick={() => setSkillsTab('my-skills')}
                >
                  My Skills
                </button>
                <button
                  className={`skills-tab-btn${skillsTab === 'shared-with-me' ? ' active' : ''}`}
                  onClick={() => setSkillsTab('shared-with-me')}
                >
                  Shared with Me
                </button>
              </div>

              {skillsTab === 'my-skills' && (
                <>
                  {isLoadingMySkills ? (
                    <div className="settings-loading">Loading skills...</div>
                  ) : mySkillsList.length === 0 ? (
                    <div className="memories-empty">
                      No personal skills. Create skills in Settings.
                    </div>
                  ) : (
                    <div className="skills-list">
                      {mySkillsList.map((skill) => renderRoutineSkillCard(
                        skill,
                        false,
                        routineAutoloadedSkillIds,
                        handleToggleRoutineAutoload,
                      ))}
                    </div>
                  )}
                </>
              )}

              {skillsTab === 'shared-with-me' && (
                <>
                  {isLoadingSharedSkills ? (
                    <div className="settings-loading">Loading shared skills...</div>
                  ) : sharedSkillsList.length === 0 ? (
                    <div className="memories-empty">
                      No skills have been shared with you yet.
                    </div>
                  ) : (
                    <div className="skills-list">
                      {sharedSkillsList.map((skill) => renderRoutineSkillCard(
                        skill,
                        true,
                        routineAutoloadedSkillIds,
                        handleToggleRoutineAutoload,
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          ) : activeSection === 'costs' ? (
            <RoutineCostsSection
              key={routine.id}
              projectId={projectId}
              routineId={routine.id}
              onOpenRun={onOpenRunConversation}
              onLoaded={() => setCostsLoaded(true)}
            />
          ) : activeSection === 'delete' ? (
            <>
              {/* Danger zone content */}
              <div className="routine-settings-danger-zone">
                <h3>Danger Zone</h3>
                <p>Deleting this routine will remove it permanently. Conversations created by this routine will not be deleted.</p>
                {showDeleteConfirm ? (
                  <div className="routine-settings-delete-confirm">
                    <span>Are you sure? This cannot be undone.</span>
                    <button
                      className="routine-settings-delete-confirm-button"
                      onClick={handleDelete}
                      disabled={isDeleting}
                    >
                      {isDeleting ? 'Deleting...' : 'Yes, Delete Routine'}
                    </button>
                    <button
                      className="routine-settings-delete-cancel-button"
                      onClick={() => setShowDeleteConfirm(false)}
                      disabled={isDeleting}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    className="routine-settings-delete-button"
                    onClick={() => setShowDeleteConfirm(true)}
                  >
                    Delete Routine
                  </button>
                )}
              </div>
            </>
          ) : null}
          </div>
        </div>
      </div>
    </ModalShell>
    {/* Conflict modal: a second portal layered on top of the settings modal
        (z-index in CSS). The settings modal stays open underneath so the
        user's unsaved edits survive; backdrop click and Escape here close
        ONLY the conflict modal (Escape via the settings shell's onEscape). */}
    <ModalShell
      isOpen={conflict !== null}
      onClose={closeConflict}
      onEscape={null}
      overlayClassName="routine-conflict-overlay"
      modalClassName="routine-conflict-modal"
    >
      {/* ModalShell evaluates children before its isOpen check, so the
          conflict fields must stay behind a null guard. */}
      {conflict && (
        <>
      <div className="routine-conflict-header">
        <h2>Someone else changed this routine</h2>
        <button
          className="routine-conflict-close-button"
          onClick={() => setConflict(null)}
          aria-label="Close"
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div className="routine-conflict-body">
        <p>
          {conflict.message} You can discard your changes and reload the
          latest version, or overwrite the other edit with what you have
          here.
        </p>
        <div className="routine-conflict-actions">
          <button
            className="routine-conflict-discard-button"
            onClick={handleConflictDiscard}
            disabled={isSaving}
          >
            Discard my changes and reload
          </button>
          <button
            className="routine-conflict-overwrite-button"
            onClick={handleConflictOverwrite}
            disabled={isSaving}
          >
            Overwrite anyway
          </button>
        </div>
      </div>
        </>
      )}
    </ModalShell>
    </>
  );
}
