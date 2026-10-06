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

# Public projects: internet-enabled sandbox, no internal data access (see
# docs/architecture/public-projects.md). Closing the gate hides EXISTING
# public projects from the API surface (list filter + 404 on direct access)
# and blocks new public-project creation and new turns in their
# conversations; the rows and workspaces are preserved, so reopening the
# gate brings everything back. This is a project-row gate, not a
# conversation flag -- filter_gated_flags never sees it. Access can be
# restricted to specific users (PER_USER_ACCESS_FEATURES): for a user
# outside the allowed list the gate behaves exactly as if it were closed.
FEATURE_PUBLIC_PROJECTS = "public_projects"

# Routines in public projects: a sub-gate of FEATURE_PUBLIC_PROJECTS. A
# routine in a public project runs in the internet-enabled sandbox, and a
# scheduled one does so unattended, so public projects have no routines
# unless an admin opts in here. Closed for a user = the Routines section is
# hidden in their public projects, the routine/schedule API refuses those
# projects, one-click runs are refused, the scheduler skips their schedules
# and run_conversation_turn refuses turns in routine-created conversations.
# Nothing is deleted: the routine and schedule rows are preserved, so
# reopening the gate restores them. Only effective while the user also has
# FEATURE_PUBLIC_PROJECTS (see public_project_routines_enabled_for).
FEATURE_PUBLIC_PROJECT_ROUTINES = "public_project_routines"

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

# All admin-gateable features. Extend this tuple (and FEATURE_LABELS) when a
# new optional feature gets a gate. Every feature is off until an admin
# enables it.
KNOWN_FEATURES = (
    FEATURE_USER_SUBAGENTS,
    FEATURE_PUBLIC_PROJECTS,
    FEATURE_PUBLIC_PROJECT_ROUTINES,
    FEATURE_GUIDES,
    FEATURE_VOICE_INPUT,
    FEATURE_DOCS,
)

# Features whose gate can be narrowed to specific users via
# ``allowed_users``. user_subagents deliberately stays all-or-nothing: a
# run involves two users (caller and target) and a per-user list would be
# ambiguous about which side it restricts.
PER_USER_ACCESS_FEATURES = frozenset({
    FEATURE_PUBLIC_PROJECTS,
    FEATURE_PUBLIC_PROJECT_ROUTINES,
    FEATURE_GUIDES,
    FEATURE_VOICE_INPUT,
    FEATURE_DOCS,
})

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
    FEATURE_PUBLIC_PROJECTS: {
        "label": "Public projects",
        "description": (
            "Lets users create public projects: conversations get internet "
            "access from the code sandbox but no access to internal data or "
            "connected services. Access can be granted to all users or only "
            "to specific users. Turning this off (or removing a user from "
            "the list) hides the affected public projects and blocks new "
            "turns in their conversations until access is restored."
        ),
    },
    FEATURE_PUBLIC_PROJECT_ROUTINES: {
        "label": "Routines in public projects",
        "description": (
            "Lets users create, run and schedule routines in public "
            "projects. Scheduled routines there run unattended in the "
            "internet-enabled sandbox. Only applies to users who also have "
            "access to Public projects. Access can be granted to all users "
            "or only to specific users. Turning this off (or removing a "
            "user from the list) hides the routines in the affected public "
            "projects and stops them from running, including scheduled "
            "runs, until access is restored."
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
}


def docs_enabled_for(user_email: str) -> bool:
    """Whether Quest Docs is on for this user.

    Convenience wrapper over :func:`is_feature_enabled_for_user` for the
    doc enforcement points (the doc service behind the tools and routes,
    the write_doc action request, the connected-services prompt gating).
    """
    return is_feature_enabled_for_user(FEATURE_DOCS, user_email)


def guides_enabled_for(user_email: str) -> bool:
    """Whether the legacy guides feature is on for this user.

    Convenience wrapper over :func:`is_feature_enabled_for_user` for the
    many guide enforcement points (guide routes, routine guide overrides,
    the conversation-loop guide resolution, the settings sync).
    """
    return is_feature_enabled_for_user(FEATURE_GUIDES, user_email)


def public_project_routines_enabled_for(user_email: str) -> bool:
    """Whether this user's public projects may have (and run) routines.

    Requires both gates: a user without public-project access has no
    visible public projects, so their routines must not run either. Shared
    by every enforcement point (routine/schedule routes, one-click runs,
    the scheduler, run_conversation_turn).
    """
    return (
        is_feature_enabled_for_user(FEATURE_PUBLIC_PROJECTS, user_email)
        and is_feature_enabled_for_user(
            FEATURE_PUBLIC_PROJECT_ROUTINES, user_email
        )
    )


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
    file means all off.
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

    Ignores any per-user restriction -- use
    :func:`is_feature_enabled_for_user` at enforcement points that act on
    behalf of a specific user. Unknown features are off.
    """
    gate = read_feature_gates().get(feature)
    return bool(gate and gate["enabled"])


def is_feature_enabled_for_user(feature: str, user_email: str) -> bool:
    """Whether ``feature`` is enabled for the user with ``user_email``.

    True when the gate is on and either open to all users or the email is
    in the allowed list (case-insensitive). Unknown features are off.
    """
    gate = read_feature_gates().get(feature)
    if not gate or not gate["enabled"]:
        return False
    if gate["allowed_users"] is None:
        return True
    return _normalize_email(user_email) in gate["allowed_users"]


def enabled_features(user_email: str) -> list[str]:
    """The feature keys enabled for this user, in KNOWN_FEATURES order."""
    return [
        feature for feature in KNOWN_FEATURES
        if is_feature_enabled_for_user(feature, user_email)
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
    supports per-user access today, so only the on/off state matters here.)
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
