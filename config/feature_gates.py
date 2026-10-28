"""Server-global feature gates: admin on/off switches for optional features.

Every optional feature is OFF by default. Admins flip gates from the
Settings > Features section (GET/PUT ``/admin/feature-gates`` in
``chat/routes/admin.py``); the state is persisted as a JSON object in
``DATA_DIR / "feature_gates.json"``. A missing or malformed file means
"all features off".

Each feature's persisted value is either a legacy plain bool (enabled for
every user) or an object ``{"enabled": bool, "allowed_users": [emails]}``
restricting an enabled feature to specific users. ``allowed_users``
absent/null means "all users"; the restriction is only honored for
features in :data:`PER_USER_ACCESS_FEATURES`. Reads normalize both shapes
into ``{"enabled": bool, "allowed_users": list[str] | None}``.

For features that ride on a per-conversation flag (see
``chat/conversation_flags.py``) the gate is a second, server-wide switch
ON TOP of the flag: the flag still opts an individual conversation in,
but only while the admin gate is open. The feature key deliberately
matches the flag name so :func:`filter_gated_flags` can drop
globally-disabled flags at the send-message chokepoint.

A gate can require another gate (:data:`FEATURE_REQUIRES`): it is then
open for a user only while the gate it requires is open for that same
user. The per-user checks (:func:`is_feature_enabled_for_user` and
everything built on it) apply the requirement; the raw switch state
(:func:`read_feature_gates`, :func:`is_feature_enabled`, the admin panel)
stays each gate's own.

This module intentionally uses only the Python standard library
(mirroring ``config/paths.py`` and ``config/service_credentials.py``) so
it can be imported cheaply from the WebSocket layer and the conversation
loop.
"""

import json
import logging
import os
import tempfile

from config.paths import FEATURE_GATES_FILE

logger = logging.getLogger(__name__)

# Cross-user subagents: conversations started with the ``user_subagents``
# flag may propose run_user_subagent action requests. The feature key
# matches the flag name in chat/conversation_flags.py.
FEATURE_USER_SUBAGENTS = "user_subagents"

# Legacy guides (see docs/architecture/guides.md): named system-prompt
# presets deprecated in favor of skills. The gate exists so an existing
# install can keep guides alive while its users migrate; new setups leave
# it off and guides are fully inert -- the /guides API 403s, the Settings
# Guides section and routine guide-override picker are hidden, routines
# cannot be given a guide override, and neither an explicit routine
# guide_id nor an already-snapshotted guide is applied to any turn.
# Nothing is deleted: the guide rows, routine guide_id references, and
# conversation snapshots are preserved, so reopening the gate restores
# the previous behavior. Per-user access lets an admin keep guides on
# only for the people who have not migrated yet.
FEATURE_GUIDES = "guides"

# Voice input (see docs/architecture/voice-input.md): a microphone button in
# the chat composer records a short clip in the browser and POSTs it to
# ``/app/api/transcribe``, which transcribes it server-side with a Gemini
# model on Vertex AI and hands the text back for the user to edit and send.
# Nothing leaves the deployment's own Vertex project. The admin endpoint
# refuses to turn the gate on while no Gemini Vertex model is configured
# (chat/transcription.py ``transcription_availability()``), and the
# transcribe route re-checks availability per request. Access can be
# restricted to specific users (PER_USER_ACCESS_FEATURES).
FEATURE_VOICE_INPUT = "voice_input"

# Quest Docs (see docs/architecture/quest-docs.md): user- and project-owned
# markdown documents that conversations read and write only through the
# dedicated doc tools (list/search/read/create/edit/append/add image), with
# one access rule (chat/docs/access.py) deciding per conversation whether a
# write is free, needs a write_doc action request, or is refused. Closed for
# a user = the doc tools and the system skill are hidden from the prompt
# (connected-services pseudo-key "docs"), every doc tool returns the
# structured ``docs_disabled`` error before touching the DB, and the
# /app/api/docs routes 403. Nothing is deleted: the rows and files are
# preserved, so reopening the gate restores them. Access can be restricted
# to specific users (PER_USER_ACCESS_FEATURES).
FEATURE_DOCS = "docs"

# Quest Doc notes (see docs/architecture/doc-notes.md): per-doc instructions
# and memories kept beside a Quest Doc's body. The doc tools deliver them
# the first time a conversation touches a doc (read_doc attaches them, the
# first write refuses once with them), and models only suggest changes,
# each reviewed by a person. Requires Quest Docs (FEATURE_REQUIRES): open
# for a user only while FEATURE_DOCS is also open for them, and always
# evaluated for the caller, never for the doc's owner. Closed for a user =
# every doc tool returns exactly what it returns without notes (no block,
# no counts, no refusal), the notes tools and the system skill are hidden
# from the prompt (connected-services pseudo-key "doc_notes") and refuse
# with ``doc_notes_disabled``, the notes routes 403 and the Notes panel is
# hidden. Suggestions already filed stay reviewable. Nothing is deleted:
# reopening the gate restores everything. Access can be restricted to
# specific users (PER_USER_ACCESS_FEATURES).
FEATURE_DOC_NOTES = "doc_notes"

# Quest Tables (see spec issue #110): user- and project-owned typed tables
# stored one SQLite file per table (``<data_dir>/tables/<id>/table.db``,
# resolved only via ``ChatStorage.get_table_dir``) that conversations read
# and write only through the dedicated table tools. Schema changes
# (create_table / change_table_schema) always need a user-approved action
# request or the UI; no model-facing tool mutates a schema. Closed for a
# user = the table tools and the system skill are hidden from the prompt
# (connected-services pseudo-key "tables"), every table tool returns the
# structured ``tables_disabled`` error before touching the DB, the
# /app/api/tables routes 403 and the sidebar section is hidden. Nothing is
# deleted: the rows and files are preserved, so reopening the gate restores
# them. Access can be restricted to specific users
# (PER_USER_ACCESS_FEATURES).
FEATURE_TABLES = "tables"

# Browser for public projects (see docs/architecture/public-browser.md):
# conversations in public projects get the browser_* tools, which drive a
# headless Chromium in a long-lived container per conversation with the
# public sandbox's network posture (internet only, no internal access, no
# credential). Needs podman and the quest-browser-sidecar image (built by
# run.py at startup); the admin endpoint refuses to turn the gate on while
# either is missing (chat/browser_sessions.py ``browser_availability()``).
# Closed for a user = the tools are hidden from the public prompt and
# refuse with ``browser_disabled``. Access can be restricted to specific
# users (PER_USER_ACCESS_FEATURES).
FEATURE_BROWSER = "browser"

# All admin-gateable features. Extend this tuple (and FEATURE_LABELS) when a
# new optional feature gets a gate. Every feature is off until an admin
# enables it.
KNOWN_FEATURES = (
    FEATURE_USER_SUBAGENTS,
    FEATURE_GUIDES,
    FEATURE_VOICE_INPUT,
    FEATURE_DOCS,
    FEATURE_DOC_NOTES,
    FEATURE_TABLES,
    FEATURE_BROWSER,
)

# Features whose gate can be narrowed to specific users via
# ``allowed_users``. user_subagents deliberately stays all-or-nothing: a
# run involves two users (caller and target) and a per-user list would be
# ambiguous about which side it restricts.
PER_USER_ACCESS_FEATURES = frozenset({
    FEATURE_GUIDES,
    FEATURE_VOICE_INPUT,
    FEATURE_DOCS,
    FEATURE_DOC_NOTES,
    FEATURE_TABLES,
    FEATURE_BROWSER,
})

# Gates that depend on another gate: ``{feature: required feature}``. A
# feature listed here is open for a user only while the feature it requires
# is also open for that same user (:func:`is_feature_enabled_for_user`, so
# also ``enabled_features()``, which feeds the frontend's enabledFeatures,
# and every ``*_enabled_for()`` wrapper). The admin switch itself stays
# independent: a dependent gate can be turned on while its prerequisite is
# off, it just has no effect until the prerequisite opens, and its
# FEATURE_LABELS description says what it requires.
FEATURE_REQUIRES: dict[str, str] = {
    FEATURE_DOC_NOTES: FEATURE_DOCS,
}

# Human text for the admin Settings > Features section, keyed by feature.
FEATURE_LABELS: dict[str, dict[str, str]] = {
    FEATURE_USER_SUBAGENTS: {
        "label": "Cross-user subagents",
        "description": (
            "Lets conversations started with the 'user_subagents' flag "
            "propose running approved read-only subagents in other users' "
            "accounts (run_user_subagent)."
        ),
    },
    FEATURE_GUIDES: {
        "label": "Guides (legacy)",
        "description": (
            "Keeps the deprecated Guides feature available while users "
            "migrate to Skills: the Settings > Guides section, the "
            "convert-to-skill flow, and routine guide overrides. Off by "
            "default for new installs. While off, existing guides are kept "
            "but never applied to conversations, and routines cannot be "
            "given a guide override. Access can be granted to all users or "
            "only to specific users."
        ),
    },
    FEATURE_VOICE_INPUT: {
        "label": "Voice input",
        "description": (
            "Adds a microphone button to the chat composer. Recordings are "
            "transcribed server-side by a Gemini model on this server's "
            "Vertex AI project (audio never goes to a third-party speech "
            "service and is not stored) and the text is placed in the "
            "composer for the user to edit before sending. Requires a "
            "configured Gemini Vertex model. Access can be granted to all "
            "users or only to specific users."
        ),
    },
    FEATURE_DOCS: {
        "label": "Quest Docs",
        "description": (
            "Lets users keep markdown documents inside Quest that "
            "conversations read and write through dedicated doc tools "
            "(list, search, read, create, search/replace edit, append, add "
            "image). Access can be granted to all users or only to specific "
            "users. Turning it off hides the tools and the Docs API; nothing "
            "is deleted."
        ),
    },
    FEATURE_DOC_NOTES: {
        "label": "Quest Doc notes",
        "description": (
            "Lets each Quest Doc carry instructions and memories, edited in "
            "a Notes panel beside the doc. Models receive them the first "
            "time a conversation touches the doc and can only suggest "
            "changes, which a person reviews. Requires Quest Docs: it has "
            "no effect for a user while Quest Docs is off for them. Access "
            "can be granted to all users or only to specific users. "
            "Turning it off hides the notes; nothing is deleted."
        ),
    },
    FEATURE_TABLES: {
        "label": "Quest Tables",
        "description": (
            "Lets users keep typed tables inside Quest that conversations "
            "read and write through dedicated table tools (list, query, "
            "add, update and delete rows). Schema changes, including "
            "creating a table, always need the user's approval. Access can "
            "be granted to all users or only to specific users. Turning it "
            "off hides the tools and the Tables API; nothing is deleted."
        ),
    },
    FEATURE_BROWSER: {
        "label": "Browser in public projects",
        "description": (
            "Gives conversations in public projects a real headless web "
            "browser (browser_* tools): one Chromium container per "
            "conversation that keeps its tabs and logins between tool "
            "calls, with the public sandbox's network rules (public "
            "internet only, no internal access, no credentials) and "
            "stopped after a few minutes of inactivity. Requires podman and "
            "the browser container image. Access can be granted to all "
            "users or only to specific users."
        ),
    },
}


def docs_enabled_for(user_email: str) -> bool:
    """Whether Quest Docs is on for this user.

    Convenience wrapper over :func:`is_feature_enabled_for_user` for the
    doc enforcement points (the doc service behind the tools and routes,
    the write_doc action request, the connected-services prompt gating).
    """
    return is_feature_enabled_for_user(FEATURE_DOCS, user_email)


def doc_notes_enabled_for(user_email: str) -> bool:
    """Whether Quest Doc notes are on for this user.

    True only while Quest Docs is also on for the same user: the gate
    requires ``docs`` (:data:`FEATURE_REQUIRES`), which
    :func:`is_feature_enabled_for_user` applies. Convenience wrapper for
    the doc-notes enforcement points (the notes block, counts and write
    refusal in the doc tools, the notes tools and routes, the
    connected-services prompt gating). Callers pass the caller's email,
    never the doc owner's: the gate is evaluated per caller.
    """
    return is_feature_enabled_for_user(FEATURE_DOC_NOTES, user_email)


def tables_enabled_for(user_email: str) -> bool:
    """Whether Quest Tables is on for this user.

    Convenience wrapper over :func:`is_feature_enabled_for_user` for the
    table enforcement points (the table tools and routes, the table action
    requests, the connected-services prompt gating).
    """
    return is_feature_enabled_for_user(FEATURE_TABLES, user_email)


def browser_enabled_for(user_email: str) -> bool:
    """Whether the public-project browser is on for this user.

    Convenience wrapper over :func:`is_feature_enabled_for_user` for the
    browser enforcement points (the browser tool handlers and the public
    prompt).
    """
    return is_feature_enabled_for_user(FEATURE_BROWSER, user_email)


def guides_enabled_for(user_email: str) -> bool:
    """Whether the legacy guides feature is on for this user.

    Convenience wrapper over :func:`is_feature_enabled_for_user` for the
    many guide enforcement points (guide routes, routine guide overrides,
    the conversation-loop guide resolution, the settings sync).
    """
    return is_feature_enabled_for_user(FEATURE_GUIDES, user_email)


def _normalize_email(email: str) -> str:
    """Canonical email form for allowed-user comparisons (case-insensitive)."""
    return str(email).strip().lower()


def _normalize_gate(value) -> dict:
    """Normalize one persisted gate value into the canonical read shape.

    Accepts the legacy plain-bool form and the object form. Returns
    ``{"enabled": bool, "allowed_users": list[str] | None}`` where ``None``
    means "all users". A present-but-malformed ``allowed_users`` value
    fails closed (an empty list -- nobody) rather than opening the gate to
    everyone.
    """
    if isinstance(value, dict):
        allowed = value.get("allowed_users")
        if isinstance(allowed, list):
            allowed_users = sorted({
                _normalize_email(e) for e in allowed
                if isinstance(e, str) and e.strip()
            })
        elif allowed is None:
            allowed_users = None
        else:
            allowed_users = []
        return {"enabled": bool(value.get("enabled")), "allowed_users": allowed_users}
    return {"enabled": bool(value), "allowed_users": None}


def read_feature_gates() -> dict[str, dict]:
    """Read the persisted gate state as ``{feature: gate}``.

    Each gate is ``{"enabled": bool, "allowed_users": list[str] | None}``
    (``None`` = all users). Unknown keys in the file are ignored; known
    features missing from the file default to off. A missing or malformed
    file means all off. This is each gate's own switch, as the admin panel
    shows it: :data:`FEATURE_REQUIRES` is not applied here.
    """
    raw: dict = {}
    if FEATURE_GATES_FILE.exists():
        try:
            with open(FEATURE_GATES_FILE, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                raw = loaded
            else:
                logger.warning(
                    "Ignoring malformed feature gates file: %s", FEATURE_GATES_FILE
                )
        except (OSError, json.JSONDecodeError):
            logger.warning("Unreadable feature gates file: %s", FEATURE_GATES_FILE)
    return {feature: _normalize_gate(raw.get(feature)) for feature in KNOWN_FEATURES}


def is_feature_enabled(feature: str) -> bool:
    """Whether an admin has enabled ``feature`` at all (for anyone).

    Ignores any per-user restriction and any :data:`FEATURE_REQUIRES`
    dependency: this is the gate's own switch. Use
    :func:`is_feature_enabled_for_user` at enforcement points that act on
    behalf of a specific user. Unknown features are off.
    """
    gate = read_feature_gates().get(feature)
    return bool(gate and gate["enabled"])


def _gate_open_for(gates: dict[str, dict], feature: str, user_email: str) -> bool:
    """Whether ``feature``'s own gate is open for the user: on, and either
    open to all users or the email is in the allowed list
    (case-insensitive). Does not look at :data:`FEATURE_REQUIRES`."""
    gate = gates.get(feature)
    if not gate or not gate["enabled"]:
        return False
    if gate["allowed_users"] is None:
        return True
    return _normalize_email(user_email) in gate["allowed_users"]


def _enabled_for_user(gates: dict[str, dict], feature: str, user_email: str) -> bool:
    """:func:`is_feature_enabled_for_user` over an already-read gate state:
    the feature's own gate and, along :data:`FEATURE_REQUIRES`, every gate
    it requires must be open for the user. The ``seen`` set only guards
    against a cycle in the mapping."""
    seen: set[str] = set()
    current: str | None = feature
    while current is not None and current not in seen:
        if not _gate_open_for(gates, current, user_email):
            return False
        seen.add(current)
        current = FEATURE_REQUIRES.get(current)
    return True


def is_feature_enabled_for_user(feature: str, user_email: str) -> bool:
    """Whether ``feature`` is enabled for the user with ``user_email``.

    True when the gate is on and either open to all users or the email is
    in the allowed list (case-insensitive), and, for a feature listed in
    :data:`FEATURE_REQUIRES`, the gate it requires is enabled for the same
    user too. Unknown features are off.
    """
    return _enabled_for_user(read_feature_gates(), feature, user_email)


def enabled_features(user_email: str) -> list[str]:
    """The feature keys enabled for this user, in KNOWN_FEATURES order.

    Same rule as :func:`is_feature_enabled_for_user` (dependencies
    included), over one read of the gates file. This is GET /me's
    ``enabled_features``, so the frontend never sees a feature whose
    required gate is closed for the user.
    """
    gates = read_feature_gates()
    return [
        feature for feature in KNOWN_FEATURES
        if _enabled_for_user(gates, feature, user_email)
    ]


def _write_gates(gates: dict[str, dict]) -> None:
    """Persist the full normalized gate state atomically.

    Features open to all users are written in the compact legacy bool form;
    features with an allowed-user list are written in the object form.
    Mirrors the ``write_service_credentials`` atomic-write pattern: a
    same-directory temp file moved into place with ``os.replace`` so a crash
    mid-write can never leave a truncated file behind.
    """
    serialized = {
        feature: (
            gate["enabled"] if gate["allowed_users"] is None
            else {"enabled": gate["enabled"], "allowed_users": gate["allowed_users"]}
        )
        for feature, gate in gates.items()
    }
    FEATURE_GATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=FEATURE_GATES_FILE.parent, prefix=".feature_gates.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(serialized, f, indent=2)
        os.replace(tmp_path, FEATURE_GATES_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def set_feature_enabled(feature: str, enabled: bool) -> dict[str, dict]:
    """Persist one gate's on/off state and return the full updated state.

    Any allowed-user list on the gate is preserved, so toggling a feature
    off and back on does not lose the admin's user selection.
    """
    if feature not in KNOWN_FEATURES:
        raise ValueError(f"Unknown feature: {feature!r}")
    gates = read_feature_gates()
    gates[feature]["enabled"] = bool(enabled)
    _write_gates(gates)
    return gates


def set_feature_allowed_users(
    feature: str, allowed_users: list[str] | None
) -> dict[str, dict]:
    """Persist one gate's allowed-user list and return the full updated state.

    ``None`` opens the gate to all users; a list restricts it to those
    emails (normalized: trimmed, lowercased, deduplicated). Only features
    in :data:`PER_USER_ACCESS_FEATURES` accept a list.
    """
    if feature not in KNOWN_FEATURES:
        raise ValueError(f"Unknown feature: {feature!r}")
    if allowed_users is not None and feature not in PER_USER_ACCESS_FEATURES:
        raise ValueError(
            f"Feature {feature!r} does not support per-user access"
        )
    gates = read_feature_gates()
    if allowed_users is None:
        gates[feature]["allowed_users"] = None
    else:
        gates[feature]["allowed_users"] = sorted({
            _normalize_email(e) for e in allowed_users if str(e).strip()
        })
    _write_gates(gates)
    return gates


def filter_gated_flags(flags: list[str]) -> list[str]:
    """Drop conversation flags whose matching feature gate is closed.

    Flags that are not feature-gated (no matching KNOWN_FEATURES key) pass
    through untouched. Dropping is silent-but-logged, matching the forgiving
    unknown-flag behavior of the ``%%flags`` parser. (No feature-gated flag
    supports per-user access or requires another gate today, so only the
    on/off state matters here.)
    """
    gates = read_feature_gates()
    kept: list[str] = []
    for name in flags:
        if name in gates and not gates[name]["enabled"]:
            logger.info(
                "Dropping conversation flag '%s': feature disabled server-wide",
                name,
            )
            continue
        kept.append(name)
    return kept
