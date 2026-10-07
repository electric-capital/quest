"""Model Selection: admin-curated presentation and visibility rules for the
models the inference providers serve.

Inference Providers (``config/inference_providers.py``) decides WHICH models
exist and are enabled; this module layers per-model *selection* settings on
top, edited in the admin-only Settings > Model Selection table (one row per
enabled, non-deprecated model) and persisted as JSON in ``DATA_DIR /
"model_selection.json"``:

- ``slot`` -- ``1..MAX_TOP_LEVEL_SLOTS`` when the model is pinned to the top
  level of the composer's model menu (the part above the "All models"
  flyout) in private conversations, ``None`` otherwise. Slots are unique
  across models; the top level lists slotted models in slot order.
- ``public_slot`` -- the same for the menu shown in public-project
  conversations, which is a separate top level (a public sandbox usually
  wants cheaper or more locked-down picks). Only meaningful while public
  mode is on (see below).
- ``descriptor`` -- the free-text label shown for a slotted model in
  either top level ("Smart ($$$)"), with the model's own name as the
  sublabel. An empty descriptor shows the model name alone. Admins put
  cost markers or anything else they like in here; the app never parses
  it.
- ``allow_private`` / ``allow_public`` -- whether the model may be used in
  private conversations (everything that is not in a public project:
  standalone chats, project chats, routines, Slack, subagent and inference
  API runs) and in public-project conversations (internet-enabled sandbox,
  see docs/architecture/public-projects.md). Unlike the Inference
  Providers enable checkbox -- which only hides a model from the picker --
  these are *usage* rules: the composer hides disallowed models from the
  menu and ``run_conversation_turn`` refuses a turn on a disallowed model,
  so an existing conversation on a model that was later disallowed gets a
  durable error until the user switches models. A model that is not
  allowed for a visibility cannot hold that visibility's slot
  (normalization clears it).

Models absent from the file get :data:`UNSET_ENTRY` (no slot, empty
descriptor, allowed everywhere). A MISSING file means the app's historical
defaults (:data:`DEFAULT_MODEL_SELECTION`: the three curated "Smart /
Faster / Fastest" picks the composer menu used to hardcode); once an admin
saves, the file is the whole truth -- clearing every slot stays cleared.

The frontend gets the same per-model fields on every catalog entry of
``GET /app/api/config`` (``public_model_catalog()`` in chat/llm/config.py)
and derives the top-level menu from them, so nothing here is mirrored by
hand on the client.

Standard library only (like ``config/feature_gates.py``): imported from the
conversation loop and the unauthenticated config endpoint.
"""

import copy
import json
import logging
import os
import tempfile

from config.paths import MODEL_SELECTION_FILE

logger = logging.getLogger(__name__)

# Number of top-level slots in the composer's model menu.
MAX_TOP_LEVEL_SLOTS = 5

# Descriptor length cap (the menu row is narrow; the UI ellipsizes anyway).
MAX_DESCRIPTOR_LENGTH = 60

# The two slot fields, keyed by the visibility whose menu they shape.
SLOT_FIELDS: dict[str, str] = {"private": "slot", "public": "public_slot"}

# Selection settings for a model the file does not mention.
UNSET_ENTRY: dict = {
    "slot": None,
    "public_slot": None,
    "descriptor": "",
    "allow_private": True,
    "allow_public": True,
}

# What a fresh install (no model_selection.json yet) shows at the top of
# both model menus: the curated picks the frontend used to hardcode in
# RECOMMENDED_MODELS. Only consulted while the file is absent.
DEFAULT_MODEL_SELECTION: dict[str, dict] = {
    "claude-opus-4-8": {**UNSET_ENTRY, "slot": 1, "public_slot": 1, "descriptor": "Smart ($$$)"},
    "claude-sonnet-5": {**UNSET_ENTRY, "slot": 2, "public_slot": 2, "descriptor": "Faster ($$)"},
    "gemini-3.8-flash": {**UNSET_ENTRY, "slot": 3, "public_slot": 3, "descriptor": "Fastest ($)"},
}


def _normalize_slot(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_TOP_LEVEL_SLOTS else None


def normalize_entry(value) -> dict:
    """Normalize one persisted/submitted entry into the canonical shape.

    Out-of-range slots become ``None``, non-string descriptors become
    ``""`` (strings are trimmed and truncated to ``MAX_DESCRIPTOR_LENGTH``),
    non-bool allow flags fall back to allowed, and a slot for a visibility
    the model is not allowed in is cleared. Never raises.
    """
    if not isinstance(value, dict):
        return dict(UNSET_ENTRY)
    allow_private = value.get("allow_private", True) is not False
    allow_public = value.get("allow_public", True) is not False
    descriptor = value.get("descriptor")
    descriptor = descriptor.strip()[:MAX_DESCRIPTOR_LENGTH] if isinstance(descriptor, str) else ""
    return {
        "slot": _normalize_slot(value.get("slot")) if allow_private else None,
        "public_slot": _normalize_slot(value.get("public_slot")) if allow_public else None,
        "descriptor": descriptor,
        "allow_private": allow_private,
        "allow_public": allow_public,
    }


def _is_unset(entry: dict) -> bool:
    return entry == UNSET_ENTRY


def read_model_selection() -> dict[str, dict]:
    """Read the persisted selection as ``{model_id: entry}``.

    A missing file yields a copy of :data:`DEFAULT_MODEL_SELECTION`; a
    malformed one yields an empty mapping (every model unset) with a
    warning, never the defaults -- an admin who cleared everything must not
    see the hardcoded picks come back because of a bad write. Entries are
    normalized; a duplicate slot in the file (only possible by hand-editing)
    is resolved by dropping the later occurrence's slot.
    """
    if not MODEL_SELECTION_FILE.exists():
        return copy.deepcopy(DEFAULT_MODEL_SELECTION)
    try:
        with open(MODEL_SELECTION_FILE, "r") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable model selection file: %s", MODEL_SELECTION_FILE)
        return {}
    models = loaded.get("models") if isinstance(loaded, dict) else None
    if not isinstance(models, dict):
        logger.warning("Ignoring malformed model selection file: %s", MODEL_SELECTION_FILE)
        return {}
    entries: dict[str, dict] = {}
    taken: dict[str, set[int]] = {field: set() for field in SLOT_FIELDS.values()}
    for model_id, raw in models.items():
        if not isinstance(model_id, str) or not model_id:
            continue
        entry = normalize_entry(raw)
        for field in SLOT_FIELDS.values():
            slot = entry[field]
            if slot is None:
                continue
            if slot in taken[field]:
                logger.warning(
                    "Duplicate %s %s in %s; dropping it from %s",
                    field, slot, MODEL_SELECTION_FILE, model_id,
                )
                entry[field] = None
            else:
                taken[field].add(slot)
        entries[model_id] = entry
    return entries


def validate_model_selection(entries: dict[str, dict]) -> dict[str, dict]:
    """Normalize a full ``{model_id: entry}`` mapping and check slot uniqueness.

    Raises ``ValueError`` when two models claim the same slot of the same
    menu (private and public slots are independent). Entries equal to
    :data:`UNSET_ENTRY` are dropped (they carry no information).
    """
    normalized: dict[str, dict] = {}
    owners: dict[str, dict[int, str]] = {field: {} for field in SLOT_FIELDS.values()}
    for model_id, raw in entries.items():
        if not isinstance(model_id, str) or not model_id:
            continue
        entry = normalize_entry(raw)
        for visibility, field in SLOT_FIELDS.items():
            slot = entry[field]
            if slot is None:
                continue
            if slot in owners[field]:
                raise ValueError(
                    f"{visibility.capitalize()} top-level slot {slot} is assigned "
                    f"to both {owners[field][slot]} and {model_id}"
                )
            owners[field][slot] = model_id
        if not _is_unset(entry):
            normalized[model_id] = entry
    return normalized


def save_model_selection(entries: dict[str, dict]) -> dict[str, dict]:
    """Persist a full-replacement selection atomically; returns what was stored.

    Same temp-file + ``os.replace`` scheme as the other data-dir stores.
    Raises ``ValueError`` on duplicate slots (nothing is written).
    """
    normalized = validate_model_selection(entries)
    MODEL_SELECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=MODEL_SELECTION_FILE.parent, prefix=".model_selection.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "models": normalized}, f, indent=2)
        os.replace(tmp_path, MODEL_SELECTION_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return normalized


def selection_for(model_id: str, entries: dict[str, dict] | None = None) -> dict:
    """The selection entry for a stored model id (``UNSET_ENTRY`` when absent).

    A bare legacy OpenRouter id is looked up under its qualified form so the
    rule set on ``openrouter:vendor/model`` also covers rows stored before
    ids were qualified.
    """
    from config.inference_providers import canonical_model_id

    if entries is None:
        entries = read_model_selection()
    if not isinstance(model_id, str) or not model_id:
        return dict(UNSET_ENTRY)
    entry = entries.get(model_id)
    if entry is None:
        entry = entries.get(canonical_model_id(model_id))
    return dict(entry) if entry is not None else dict(UNSET_ENTRY)


def is_model_allowed(model_id: str, *, public: bool) -> bool:
    """Whether ``model_id`` may run a turn in a public (``True``) or private
    (``False``) conversation.

    Unknown / unlisted models are allowed -- the provider layer decides
    whether they exist.
    """
    entry = selection_for(model_id)
    return entry["allow_public"] if public else entry["allow_private"]
