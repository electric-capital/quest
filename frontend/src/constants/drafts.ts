// Draft-key constants shared between the composers that run before a real
// conversation exists and the conversation context.
//
// The home composer (root "/" screen) has no real conversation yet, so it binds
// the shared <Composer> to this stable in-memory key for the context-keyed
// model/skills lookups. The value must stay byte-identical to the historical
// '__home_draft__' string: ConversationModelsContext's lazy initializer use it to
// strip legacy guide/provider lock entries that older builds persisted to
// localStorage under this key (see devplan 00127).

export const HOME_DRAFT_KEY = '__home_draft__';

// The Quest Tasks composers (components/tasks/TaskComposer.tsx) never send on
// a conversation they are bound to: every send creates a fresh run
// conversation. The New Task page binds its composer to TASK_DRAFT_KEY; a
// task thread binds its own to taskDraftKey(taskId), so a model pick or a
// queued skill stays with that task. Like HOME_DRAFT_KEY these keys must never
// get a provider lock (the composer runs with skipSendLocks).
export const TASK_DRAFT_KEY = '__task_draft__';

export function taskDraftKey(taskId: string): string {
  return `${TASK_DRAFT_KEY}:${taskId}`;
}
