// Test-only builders for AutoProject component tests: an AutoProject, a run,
// a question and a runs view with every field filled, overridable per test.
import { vi } from 'vitest';
import type { AutoProject, AutoProjectQuestion, AutoProjectRun } from '../../api/types';
import type { AutoProjectRunsView } from '../../hooks/useAutoProjectRuns';

export function makeAutoProject(overrides: Partial<AutoProject> = {}): AutoProject {
  return {
    id: 'ap1',
    title: 'Quarterly report',
    title_is_default: false,
    archived: false,
    brief: '',
    brief_version: 0,
    brief_updated_at: null,
    brief_source: null,
    brief_run_seq: null,
    model: null,
    thinking_effort: null,
    run_count: 0,
    active_run: null,
    event_seq: 0,
    unread_seq: 0,
    read_seq: 0,
    unread: false,
    created_at: '2026-11-10T10:00:00',
    updated_at: '2026-11-10T10:00:00',
    last_activity_at: null,
    ...overrides,
  };
}

export function makeQuestion(overrides: Partial<AutoProjectQuestion> = {}): AutoProjectQuestion {
  return {
    handle_id: 'h1',
    question: 'Which quarter?',
    options: [],
    answer: null,
    status: 'pending',
    asked_at: null,
    answered_at: null,
    ...overrides,
  };
}

export function makeRun(seq: number, overrides: Partial<AutoProjectRun> = {}): AutoProjectRun {
  return {
    id: `run-${seq}`,
    seq,
    conversation_id: `conv-${seq}`,
    prompt: `Prompt ${seq}`,
    status: 'completed',
    summary: `Summary ${seq}`,
    result: `Result ${seq}`,
    notes: null,
    handoff_missing: false,
    error: null,
    model: null,
    started_at: '2026-11-10T10:00:00',
    finished_at: '2026-11-10T10:05:00',
    questions: [],
    open_requests: [],
    ...overrides,
  };
}

export function makeRunsView(runs: AutoProjectRun[], overrides: Partial<AutoProjectRunsView> = {}): AutoProjectRunsView {
  return {
    runs,
    loading: false,
    error: null,
    notFound: false,
    hasOlder: false,
    loadingOlder: false,
    loadOlder: vi.fn(),
    refresh: vi.fn(async () => {}),
    removeRun: vi.fn(),
    ...overrides,
  };
}
