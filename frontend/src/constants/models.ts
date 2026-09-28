import type { AppModelInfo } from '../api/types';

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  /** Picker sublabel: the Vertex family or the admin's instance label. */
  providerLabel: string;
  maxInputTokens: number;
  /**
   * Deprecated models stay in the catalog so existing conversations/routines
   * keep resolving display names, providers, and context sizes, but they are
   * excluded from getSelectableModels() (and from the server's
   * available_models) so they cannot be picked for new work. Mirrors the
   * `deprecated` flag in MODEL_REGISTRY (chat/llm/config.py).
   */
  deprecated?: boolean;
  /**
   * Admin Model Selection (Settings > Model Selection, persisted server-side
   * in data/model_selection.json and delivered on every catalog entry of
   * GET /app/api/config): the composer menu's top-level slot for private
   * and for public-project conversations (null = only listed under "All
   * models"), the label shown for a slotted model, and whether the model
   * may be used in private / public-project conversations (both true, and
   * publicSlot null, while public projects are off server-wide).
   */
  slot: number | null;
  publicSlot: number | null;
  descriptor: string;
  allowPrivate: boolean;
  allowPublic: boolean;
}

/** Conversation visibility a model list is being offered for. */
export type ModelVisibility = 'private' | 'public';

/**
 * Pre-config-load fallback catalog: the fixed Vertex models, hand-mirrored
 * from MODEL_REGISTRY (chat/llm/config.py). The live catalog -- which also
 * carries the admin-configured OpenRouter instance models under their
 * `<instance>:<wire_id>` ids -- arrives with GET /app/api/config (`models`)
 * and replaces this list via setModelCatalog(); nothing else should read
 * BUILTIN_MODELS directly.
 */
const UNSET_SELECTION = { slot: null, publicSlot: null, descriptor: '', allowPrivate: true, allowPublic: true };

const BUILTIN_MODELS: ModelInfo[] = ([
  { id: 'gemini-3.1-pro-preview', name: 'Gemini 3.1 Pro', provider: 'gemini', providerLabel: 'Gemini on Vertex', maxInputTokens: 1_000_000, deprecated: true },
  { id: 'gemini-3-flash-preview', name: 'Gemini 3 Flash', provider: 'gemini', providerLabel: 'Gemini on Vertex', maxInputTokens: 1_000_000, deprecated: true },
  { id: 'gemini-3.1-flash-lite-preview', name: 'Gemini 3.1 Flash-Lite', provider: 'gemini', providerLabel: 'Gemini on Vertex', maxInputTokens: 1_000_000, deprecated: true },
  { id: 'gemini-3.5-flash', name: 'Gemini 3.5 Flash', provider: 'gemini', providerLabel: 'Gemini on Vertex', maxInputTokens: 1_000_000, deprecated: true },
  { id: 'gemini-3.5-flash-lite', name: 'Gemini 3.5 Flash-Lite', provider: 'gemini', providerLabel: 'Gemini on Vertex', maxInputTokens: 1_000_000 },
  { id: 'gemini-3.6-flash', name: 'Gemini 3.6 Flash', provider: 'gemini', providerLabel: 'Gemini on Vertex', maxInputTokens: 1_000_000 },
  { id: 'gemini-3.7-flash', name: 'Gemini 3.7 Flash', provider: 'gemini', providerLabel: 'Gemini on Vertex', maxInputTokens: 1_000_000 },
  { id: 'gemini-3.8-flash', name: 'Gemini 3.8 Flash', provider: 'gemini', providerLabel: 'Gemini on Vertex', maxInputTokens: 1_000_000 },
  { id: 'claude-haiku-4.5', name: 'Claude Haiku 4.5', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 200_000 },
  { id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 200_000 },
  { id: 'claude-sonnet-5', name: 'Claude Sonnet 5', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 1_000_000 },
  { id: 'claude-sonnet-5-5', name: 'Claude Sonnet 5.5', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 1_000_000 },
  { id: 'claude-opus-4-6', name: 'Claude Opus 4.6', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 200_000 },
  { id: 'claude-opus-4-7', name: 'Claude Opus 4.7', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 200_000 },
  { id: 'claude-opus-4-8', name: 'Claude Opus 4.8', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 1_000_000 },
  { id: 'claude-opus-5', name: 'Claude Opus 5', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 1_000_000 },
  { id: 'claude-opus-5-5', name: 'Claude Opus 5.5', provider: 'anthropic', providerLabel: 'Claude on Vertex', maxInputTokens: 1_000_000 },
] as Omit<ModelInfo, keyof typeof UNSET_SELECTION>[]).map((m) => ({
  ...m,
  ...UNSET_SELECTION,
  // The server's absent-file defaults (DEFAULT_MODEL_SELECTION in
  // config/model_selection.py), so the pre-config paint matches.
  ...(m.id === 'claude-opus-4-8' ? { slot: 1, publicSlot: 1, descriptor: 'Smart ($$$)' }
    : m.id === 'claude-sonnet-5' ? { slot: 2, publicSlot: 2, descriptor: 'Faster ($$)' }
    : m.id === 'gemini-3.8-flash' ? { slot: 3, publicSlot: 3, descriptor: 'Fastest ($)' }
    : {}),
}));

const VERTEX_PROVIDERS = new Set(['gemini', 'anthropic']);

let catalog: ModelInfo[] = BUILTIN_MODELS;

/**
 * Replace the catalog with the server's model list (GET /app/api/config
 * `models`). Instance-served models get their instance label folded into the
 * display name ("DeepSeek V4 Flash (OpenRouter)") so pickers, message
 * footers and settings dropdowns all tell two instances of the same model
 * apart. Called from ConversationContext when config arrives; a malformed
 * payload keeps the previous catalog.
 */
export function setModelCatalog(models: AppModelInfo[] | undefined | null): void {
  if (!Array.isArray(models) || models.length === 0) return;
  const next: ModelInfo[] = [];
  for (const m of models) {
    if (!m || typeof m.id !== 'string' || !m.id) continue;
    const isVertex = VERTEX_PROVIDERS.has(m.provider);
    next.push({
      id: m.id,
      name: isVertex || !m.provider_label
        ? m.display_name || m.id
        : `${m.display_name || m.id} (${m.provider_label})`,
      provider: m.provider,
      providerLabel: m.provider_label || m.provider,
      maxInputTokens: m.max_input_tokens || 0,
      deprecated: m.deprecated === true,
      slot: typeof m.slot === 'number' ? m.slot : null,
      publicSlot: typeof m.public_slot === 'number' ? m.public_slot : null,
      descriptor: typeof m.descriptor === 'string' ? m.descriptor : '',
      allowPrivate: m.allow_private !== false,
      allowPublic: m.allow_public !== false,
    });
  }
  if (next.length > 0) catalog = next;
}

/**
 * Every known model, incl. deprecated / admin-disabled ones. Use this for
 * lookups so old conversations/routines still display properly; use
 * getSelectableModels() for pickers.
 */
export function getKnownModels(): ModelInfo[] {
  return catalog;
}

/** Whether the admin allows a model in conversations of this visibility. */
export function isModelAllowedFor(model: ModelInfo, visibility: ModelVisibility): boolean {
  return visibility === 'public' ? model.allowPublic : model.allowPrivate;
}

/**
 * Models offerable for new selection in a conversation of the given
 * visibility: everything known except deprecated entries and models the
 * admin has unticked for that visibility (Settings > Model Selection).
 * Defaults to private, which covers every picker outside a public project
 * (composer, routines, Slack default). Callers still intersect with the
 * server's `available_models` (credentialed, enabled, healthy) before
 * offering them.
 */
export function getSelectableModels(visibility: ModelVisibility = 'private'): ModelInfo[] {
  return catalog.filter((m) => !m.deprecated && isModelAllowedFor(m, visibility));
}

/** The slot field that shapes the menu for a conversation visibility. */
export function slotFor(model: ModelInfo, visibility: ModelVisibility): number | null {
  return visibility === 'public' ? model.publicSlot : model.slot;
}

/**
 * The admin's top-level composer menu picks for a conversation visibility
 * (models with a slot in that menu), in slot order. The menu shows each
 * one's descriptor (or its name when the descriptor is empty) with the
 * model name as the sublabel; the full list lives behind the "All models"
 * submenu. Entries whose model is not offerable in the current context are
 * hidden by the menu, not disabled.
 */
export function getTopLevelModels(visibility: ModelVisibility = 'private'): ModelInfo[] {
  return catalog
    .filter((m) => !m.deprecated && slotFor(m, visibility) !== null)
    .sort((a, b) => (slotFor(a, visibility) as number) - (slotFor(b, visibility) as number));
}

/** Catalog entry for a model id, if known. */
export function getModelInfo(modelId: string): ModelInfo | undefined {
  return catalog.find((m) => m.id === modelId);
}

/**
 * Fallback default conversation model when the per-user server-side default
 * (users.settings.default_model, surfaced on GET /me) is unset/unknown. Single
 * source of truth for the FE default; applied after reading /me. Opus 4.8.
 *
 * Only used directly when the credentialed-model list is unknown; otherwise go
 * through resolveFallbackModel so a server without Anthropic credentials falls
 * back to a credentialed (e.g. Gemini or OpenRouter) model instead.
 */
export const DEFAULT_MODEL_ID = 'claude-opus-4-8';

/**
 * Whether a model id can be offered for NEW selection in a conversation of
 * the given visibility: known, not deprecated, allowed by the admin for that
 * visibility, and credentialed (available_models from GET /app/api/config;
 * a null list -- config not loaded yet -- skips the credential check).
 */
export function isModelSelectableFor(
  modelId: string,
  visibility: ModelVisibility,
  availableIds: string[] | null,
): boolean {
  const entry = getModelInfo(modelId);
  if (entry === undefined || entry.deprecated) return false;
  if (!isModelAllowedFor(entry, visibility)) return false;
  return availableIds === null || availableIds.includes(modelId);
}

/**
 * Pick the fallback default model for a conversation visibility given the
 * server's credentialed-model list (available_models from GET
 * /app/api/config; null while unknown). Prefers DEFAULT_MODEL_ID, but when
 * its backend credentials are missing -- or the admin disallowed it for this
 * visibility (Settings > Model Selection) -- falls back to the admin's first
 * top-level menu pick for the visibility, then to the first offerable model
 * in catalog order (e.g. Gemini 3.5 Flash-Lite on a Gemini-only local
 * instance; never a deprecated model). With nothing offerable at all
 * (sending is disabled anyway) returns DEFAULT_MODEL_ID.
 */
export function resolveFallbackModel(
  availableIds: string[] | null,
  visibility: ModelVisibility = 'private',
): string {
  if (isModelSelectableFor(DEFAULT_MODEL_ID, visibility, availableIds)) {
    return DEFAULT_MODEL_ID;
  }
  const candidates = [...getTopLevelModels(visibility), ...getSelectableModels(visibility)];
  const first = candidates.find((m) => isModelSelectableFor(m.id, visibility, availableIds));
  return first ? first.id : DEFAULT_MODEL_ID;
}

/** Look up the provider for a model ID. */
export function getProviderForModel(modelId: string): string {
  return getModelInfo(modelId)?.provider ?? 'gemini';
}

/** Look up a human-readable display name for a model ID. */
export function getModelDisplayName(modelId: string): string {
  return getModelInfo(modelId)?.name ?? modelId;
}

/** True when the model ID is a known-but-deprecated entry. */
export function isDeprecatedModel(modelId: string): boolean {
  return getModelInfo(modelId)?.deprecated === true;
}

/**
 * Deprecated model IDs that should be silently remapped to their replacement.
 * Used when loading old conversations or routines that reference retired models.
 */
export const DEPRECATED_MODEL_MAP: Record<string, string> = {
  'gemini-3-pro-preview': 'gemini-3.1-pro-preview',
};
