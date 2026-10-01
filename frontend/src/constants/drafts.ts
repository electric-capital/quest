// Draft-key constants shared between the home composer and the conversation
// context.
//
// The home composer (root "/" screen) has no real conversation yet, so it binds
// the shared <Composer> to this stable in-memory key for the context-keyed
// model/skills lookups. The value must stay byte-identical to the historical
// '__home_draft__' string: ConversationModelsContext's lazy initializer use it to
// strip legacy guide/provider lock entries that older builds persisted to
// localStorage under this key (see devplan 00127).

export const HOME_DRAFT_KEY = '__home_draft__';
