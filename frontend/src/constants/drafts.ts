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

// The AutoProjects composers (components/autoprojects/AutoProjectComposer.tsx) never send on
// a conversation they are bound to: every send creates a fresh run
// conversation. The New AutoProject page binds its composer to AUTOPROJECT_DRAFT_KEY; an
// AutoProject's page binds its own to autoprojectDraftKey(autoprojectId), so a model pick, a
// queued skill or unsent text (store/autoprojectDrafts.ts) stays with that AutoProject.
// Like HOME_DRAFT_KEY these keys must never get a provider lock (the composer runs
// with skipSendLocks).
export const AUTOPROJECT_DRAFT_KEY = '__autoproject_draft__';

export function autoprojectDraftKey(autoprojectId: string): string {
  return `${AUTOPROJECT_DRAFT_KEY}:${autoprojectId}`;
}
