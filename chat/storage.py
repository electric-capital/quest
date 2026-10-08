"""Chat storage management for conversation persistence.

Conversation metadata (user ownership, timestamps) is stored in SQLite via
db/conversation_store.py.  The actual conversation content lives on disk:

    data/chats/{conversation_id}/
        chat_history.json   — message history + guide snapshot + per-conversation
                              notice flags (see get_conversation_flags)
        sdk_history.json    — Gemini SDK session history (API mode only)
        workspace/          — the conversation workspace: files uploaded by the
                              user or written by the agent, for standalone AND
                              project conversations
                              (``ChatStorage.get_conversation_workspace_root``)

    data/projects/{project_id}/
        project.db          — per-project SQLite database
        workspace/
            workspace/      — the project workspace shared by every conversation
                              of the project
                              (``ChatStorage.get_project_workspace_root``)

User subdirectories (data/chats/{user_id}/) are no longer used after the
b3f9a1c2d4e5 Alembic migration.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

from config.paths import CHATS_DIR, DOCS_DIR, PROJECTS_DIR

logger = logging.getLogger(__name__)


# Server-set, persisted-once booleans at the top level of chat_history.json
# (see ChatStorage.get_conversation_flags). ``legacy_shared_workspace``: a
# project conversation whose earlier files live in the shared project
# workspace; ``converted_from_standalone``: turned into a project's first
# conversation by POST /projects/from-conversation, files left in place;
# ``own_workspace``: the explicit post-cutover marker -- set when a project
# conversation is created (create_project_conversation) or converted
# (from-conversation), i.e. the conversation has always had its own
# conversation workspace.
#
# Legacy detection (run_conversation_turn, via
# chat.gemini_api.conversation._resolve_workspace_notice_flags): on a project
# conversation's first turn without ``own_workspace``, a history with at least
# one ``role: "assistant"`` message marks it a legacy project conversation --
# its earlier files live in the shared project workspace -- and sets
# ``legacy_shared_workspace``; either way ``own_workspace`` is then set, so
# the check runs once per conversation. Standalone conversations are never
# touched. The presence of ``chats/{cid}/workspace/`` must NOT be used for
# this: file_storage.validate_path and the tool handlers'
# conversation_workspace_dir mkdir that dir as a side effect of merely
# opening the conversation or running any tool.
CONVERSATION_NOTICE_FLAGS = (
    "legacy_shared_workspace", "converted_from_standalone", "own_workspace",
)


def utc_timestamp() -> str:
    """Return current UTC time as ISO 8601 string with Z suffix."""
    return datetime.utcnow().isoformat() + "Z"


def _parse_datetime(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string into a datetime (UTC-aware).

    Handles strings with a trailing 'Z' (produced by utc_timestamp()) as
    well as plain '+00:00' offset strings.
    """
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        # Fall back: treat as naive UTC
        return datetime.fromisoformat(ts.split("+")[0]).replace(tzinfo=timezone.utc)


def _compute_auto_title(content: str) -> str:
    """Derive the cached sidebar title from a user message body.

    Mirrors the slicing rule that ``list_conversations`` previously applied
    inline: first 50 chars, with a trailing ``...`` ellipsis if the message
    is longer. Empty / non-string content yields ``""`` so the caller can
    skip persisting (no useful title to cache).
    """
    if not isinstance(content, str) or not content:
        return ""
    if len(content) > 50:
        return content[:50] + "..."
    return content[:50]


def _publish_appended_to_bus(
    conversation_id: str,
    appended: list[tuple[int, dict]],
) -> None:
    """Best-effort fan-out of newly-appended messages to persistent-WS
    subscribers.

    Mirrors the publish step in ``chat/_flush_helper.py:make_flush_callback``
    so callers that go through the single-message ``append_message`` path
    (the realtime socket's user-message append, the Slack socket-mode
    user-message append, the routine scheduler) ALSO push their bubbles
    to onlooker tabs. Without this, a second tab viewing the same
    conversation never learned about the user message and only saw the
    assistant's reply.

    Failures here never roll back the disk write -- the publish is purely
    a UI-liveness optimisation.
    """
    if not appended:
        return
    try:
        from chat.realtime.bus import bus
        from chat.realtime.events import make_message_appended
        from chat.realtime.replay_buffer import replay_buffer
    except Exception:
        # Realtime package not importable (shouldn't happen in normal
        # runtime). Silently skip; the next durable event recovers the tab.
        return

    for seq, msg in appended:
        try:
            replay_buffer.append(conversation_id, seq, msg)
        except Exception:
            logger.debug(
                "[storage] replay_buffer.append failed "
                "(conversation=%s, seq=%s)",
                conversation_id, seq, exc_info=True,
            )
        try:
            bus.publish_to_conversation(
                conversation_id,
                make_message_appended(conversation_id, seq),
            )
        except Exception:
            logger.debug(
                "[storage] bus.publish_to_conversation failed "
                "(conversation=%s, seq=%s)",
                conversation_id, seq, exc_info=True,
            )


class InvalidStorageIdError(ValueError):
    """A conversation, project or doc id cannot be turned into an on-disk path.

    Raised by the central path resolvers on ``ChatStorage`` for ids that are
    not a single canonical path segment (empty, ``.``, ``..``, containing a
    separator, absolute) or whose resolved directory would not live under
    ``CHATS_DIR`` / ``PROJECTS_DIR`` / ``DOCS_DIR`` (e.g. a planted symlink). Subclasses
    ``ValueError`` so callers that already treat a bad id as a cache miss or
    a 4xx keep working.
    """


def _validate_id_segment(value, label: str) -> str:
    """Return ``value`` if it is a single canonical path segment, else raise.

    ``Path(value).name != value`` rejects everything that could steer a join
    away from ``root / value``: empty strings, ``.``/``..``, embedded
    separators (``<id>/workspace/cache`` -- security finding #279217) and
    absolute paths. Ids are otherwise opaque (UUIDs in production, short
    fixture names in tests), so no format beyond that is enforced.
    """
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise InvalidStorageIdError(f"Invalid {label} ID")
    return value


def _resolve_contained_dir(root: Path, segment: str) -> Path:
    """Return ``root / segment`` after confirming it resolves under ``root``.

    The canonical-segment check makes a lexical escape impossible; this
    guards the remaining physical one, a ``root/<segment>`` entry that is a
    symlink pointing outside the tree. The returned path is the plain join
    (not the resolved one) so callers and tests can keep reasoning about
    ``CHATS_DIR / id``.
    """
    candidate = root / segment
    root_resolved = root.resolve()
    try:
        candidate.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise InvalidStorageIdError(f"Invalid ID: {segment!r} escapes {root}") from exc
    return candidate


def _check_contained(root: Path, candidate: Path) -> Path:
    """Return ``candidate`` unchanged if it resolves under ``root``, else raise.

    Used by the workspace-root resolvers for the fixed segments they append
    below an already-validated id dir. ``root`` is that id dir itself (e.g.
    ``CHATS_DIR / cid``), not the data root, so a symlinked ``workspace``
    entry can point neither outside the data tree nor at another
    conversation's or project's workspace (``chats/A/workspace ->
    chats/B/workspace``). The id dir was already containment-checked against
    its data root by ``_resolve_contained_dir``, so "under the resolved id
    dir" implies "under the data root".
    """
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidStorageIdError(f"Invalid path: {candidate} escapes {root}") from exc
    return candidate


class ChatStorage:
    """Manages chat conversation storage and retrieval.

    Path helpers no longer accept user_id — the flat layout
    (data/chats/{conversation_id}/) makes the user dimension unnecessary.
    Ownership is authoritative in the conversations SQLite table.

    ``get_conversation_dir`` / ``get_project_dir`` / ``get_doc_dir`` (and the
    helpers built on them) are the ONLY places that turn an id into a
    filesystem path. They validate the id and containment-check the result,
    so every caller is safe by construction; production code must not join
    ``CHATS_DIR`` / ``PROJECTS_DIR`` / ``DOCS_DIR`` with an id itself.
    Ownership is deliberately NOT
    checked here -- schedulers, migrations and cross-user subagents resolve
    paths without a "current user"; HTTP routes go through
    ``chat.conversation_access`` which adds the DB ownership lookup.
    """

    # ------------------------------------------------------------------
    # Path resolvers (validated, containment-checked)
    # ------------------------------------------------------------------

    @staticmethod
    def get_conversation_dir(conversation_id: str) -> Path:
        """Return ``CHATS_DIR / conversation_id`` for a canonical, contained id.

        The directory is NOT created here; callers that need it to exist
        should call mkdir() themselves (only create_conversation does that).

        Raises:
            InvalidStorageIdError: non-canonical id or path outside CHATS_DIR.
        """
        _validate_id_segment(conversation_id, "conversation")
        return _resolve_contained_dir(CHATS_DIR, conversation_id)

    # Historical private name; same validated resolver.
    _get_conversation_dir = get_conversation_dir

    @staticmethod
    def get_project_dir(project_id: str) -> Path:
        """Return ``PROJECTS_DIR / project_id`` for a canonical, contained id.

        Raises:
            InvalidStorageIdError: non-canonical id or path outside PROJECTS_DIR.
        """
        _validate_id_segment(project_id, "project")
        return _resolve_contained_dir(PROJECTS_DIR, project_id)

    @staticmethod
    def get_project_db_path(project_id: str) -> Path:
        """Return the per-project SQLite database file path."""
        return ChatStorage.get_project_dir(project_id) / "project.db"

    @staticmethod
    def get_conversation_workspace_root(conversation_id: str) -> Path:
        """Return the conversation workspace root ``CHATS_DIR / id / "workspace"``.

        This is the browsable root itself (what the user sees as ``/``) for
        every conversation, standalone or in a project; callers must not
        append ``"workspace"`` to it. Inherits the canonical-id and
        containment checks of ``get_conversation_dir`` and additionally
        refuses a ``workspace`` entry that resolves outside this
        conversation's own dir (a planted symlink, incl. one into another
        conversation's workspace). The directory is NOT created here.

        Raises:
            InvalidStorageIdError: non-canonical id or path outside the
            conversation dir.
        """
        conversation_dir = ChatStorage.get_conversation_dir(conversation_id)
        return _check_contained(conversation_dir, conversation_dir / "workspace")

    @staticmethod
    def get_project_workspace_root(project_id: str) -> Path:
        """Return the project workspace root ``PROJECTS_DIR / id / "workspace" / "workspace"``.

        The browsable root shared by all conversations of the project. The
        doubled ``workspace/workspace`` is the historical on-disk layout;
        only this resolver knows about it. Inherits the canonical-id and
        containment checks of ``get_project_dir`` and additionally refuses
        a root that resolves outside this project's own dir (a planted
        symlink at either ``workspace`` level, incl. one into another
        project). The directory is NOT created here.

        Raises:
            InvalidStorageIdError: non-canonical id or path outside the
            project dir.
        """
        project_dir = ChatStorage.get_project_dir(project_id)
        return _check_contained(project_dir, project_dir / "workspace" / "workspace")

    @staticmethod
    def get_doc_dir(doc_id: str) -> Path:
        """Return ``DOCS_DIR / doc_id`` for a canonical, contained id.

        The sole id-to-path resolver for Quest Docs (``doc.md``, ``assets/``
        and ``revisions/`` live under it; see chat/docs/files.py). The
        directory is NOT created here.

        Raises:
            InvalidStorageIdError: non-canonical id or path outside DOCS_DIR.
        """
        _validate_id_segment(doc_id, "doc")
        return _resolve_contained_dir(DOCS_DIR, doc_id)

    @staticmethod
    def _get_chat_history_file(conversation_id: str) -> Path:
        """Return the path to chat_history.json for a conversation."""
        return ChatStorage.get_conversation_dir(conversation_id) / "chat_history.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    async def create_conversation(user_id: int) -> tuple[str, str]:
        """Create a new conversation for a user.

        Creates the conversation directory, writes chat_history.json, and
        inserts a row into the conversations table via conversation_store.

        Args:
            user_id: User's integer ID.

        Returns:
            ``(conversation_id, created_at_iso)`` -- the route uses both to
            answer the create response without re-reading the JSON file we
            just wrote.
        """
        from db.conversation_store import create_conversation as db_create_conversation

        conversation_id = str(uuid.uuid4())
        conversation_dir = ChatStorage._get_conversation_dir(conversation_id)
        conversation_dir.mkdir(parents=True, exist_ok=True)

        created_at_str = utc_timestamp()

        # Write chat_history.json using the newer user_id (integer) format
        chat_data = {
            "id": conversation_id,
            "user_id": user_id,
            "created_at": created_at_str,
            "messages": []
        }

        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        with open(chat_file, "w") as f:
            json.dump(chat_data, f, indent=2)

        # Insert metadata row into SQLite
        created_at_dt = _parse_datetime(created_at_str)
        await db_create_conversation(user_id, conversation_id, created_at_dt)

        return conversation_id, created_at_str

    @staticmethod
    async def create_slack_conversation(
        user_id: int,
        slack_channel_id: str,
        slack_thread_ts: str,
        slack_user_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Create a new Slack-originated conversation.

        Mirrors create_conversation() but marks origin="slack" and links
        the conversation to a (slack_channel_id, slack_thread_ts) pair via
        the slack_conversations table.

        Args:
            user_id: User's integer ID.
            slack_channel_id: Slack DM channel id (``D...``).
            slack_thread_ts: Slack ``ts`` of the top-level user DM.
            slack_user_id: Optional Slack user id of the DM partner.
            model: Optional LLM model ID to set on the conversation.

        Returns:
            conversation_id: UUID string for the new conversation.
        """
        from db.conversation_store import create_conversation as db_create_conversation
        from chat.slack_conversation_store import create_slack_conversation as db_create_slack

        conversation_id = str(uuid.uuid4())
        conversation_dir = ChatStorage._get_conversation_dir(conversation_id)
        conversation_dir.mkdir(parents=True, exist_ok=True)

        created_at_str = utc_timestamp()

        chat_data = {
            "id": conversation_id,
            "user_id": user_id,
            "created_at": created_at_str,
            "origin": "slack",
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
            "messages": [],
        }

        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        with open(chat_file, "w") as f:
            json.dump(chat_data, f, indent=2)

        created_at_dt = _parse_datetime(created_at_str)
        await db_create_conversation(
            user_id, conversation_id, created_at_dt,
            model=model, origin="slack",
        )
        await db_create_slack(
            conversation_id=conversation_id,
            user_id=user_id,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            slack_user_id=slack_user_id,
        )

        return conversation_id

    @staticmethod
    async def create_user_subagent_conversation(
        user_id: int,
        model: Optional[str] = None,
        custom_name: Optional[str] = None,
    ) -> str:
        """Create a cross-user subagent conversation (origin="user_subagent").

        Mirrors create_conversation() but marks the origin so the web UI
        renders it read-only and run_conversation_turn picks the restricted
        subagent toolset. ``user_id`` is the TARGET user (the account the
        subagent runs in); the caller linkage lives on the
        user_subagent_runs row.

        Returns:
            conversation_id: UUID string for the new conversation.
        """
        from db.conversation_store import create_conversation as db_create_conversation
        from db.conversation_store import rename_conversation

        conversation_id = str(uuid.uuid4())
        conversation_dir = ChatStorage._get_conversation_dir(conversation_id)
        conversation_dir.mkdir(parents=True, exist_ok=True)

        created_at_str = utc_timestamp()

        chat_data = {
            "id": conversation_id,
            "user_id": user_id,
            "created_at": created_at_str,
            "origin": "user_subagent",
            "messages": [],
        }

        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        with open(chat_file, "w") as f:
            json.dump(chat_data, f, indent=2)

        created_at_dt = _parse_datetime(created_at_str)
        await db_create_conversation(
            user_id, conversation_id, created_at_dt,
            model=model, origin="user_subagent",
        )
        if custom_name:
            try:
                await rename_conversation(
                    user_id, conversation_id, custom_name[:100],
                )
            except Exception:
                pass  # Non-fatal; the auto-title fallback still works

        return conversation_id

    @staticmethod
    async def create_inference_api_conversation(
        user_id: int,
        model: Optional[str] = None,
    ) -> str:
        """Create a one-shot inference API conversation (origin="inference_api").

        Mirrors create_user_subagent_conversation(): the origin marks the
        conversation read-only in the web UI and makes run_conversation_turn pick
        the restricted inference toolset + headless system prompt. The run
        is driven by chat/inference_api.py on behalf of ``user_id`` (the
        owner of the bearer token that authenticated the API call).

        Returns:
            conversation_id: UUID string for the new conversation.
        """
        from db.conversation_store import create_conversation as db_create_conversation

        conversation_id = str(uuid.uuid4())
        conversation_dir = ChatStorage._get_conversation_dir(conversation_id)
        conversation_dir.mkdir(parents=True, exist_ok=True)

        created_at_str = utc_timestamp()

        chat_data = {
            "id": conversation_id,
            "user_id": user_id,
            "created_at": created_at_str,
            "origin": "inference_api",
            "messages": [],
        }

        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        with open(chat_file, "w") as f:
            json.dump(chat_data, f, indent=2)

        created_at_dt = _parse_datetime(created_at_str)
        await db_create_conversation(
            user_id, conversation_id, created_at_dt,
            model=model, origin="inference_api",
        )

        return conversation_id

    @staticmethod
    def get_conversation(conversation_id: str) -> Optional[Dict]:
        """Get conversation history from disk.

        This reads the chat_history.json file for the given conversation_id.
        It does NOT perform an ownership check — callers that need to verify
        ownership should call db.conversation_store.get_conversation_meta()
        first.

        Args:
            conversation_id: Conversation UUID.

        Returns:
            Dictionary with conversation data, or None if the file does not exist.
        """
        chat_file = ChatStorage._get_chat_history_file(conversation_id)

        if not chat_file.exists():
            return None

        with open(chat_file, "r") as f:
            return json.load(f)

    @staticmethod
    async def list_conversations(
        user_id: int,
        include_archived: bool = False,
        *,
        exclude_projects: bool = False,
        include_slack: bool = True,
        include_inference: bool = True,
        limit: Optional[int] = None,
        before: Optional[tuple] = None,
    ) -> List[Dict]:
        """List conversations for a user, ordered by most recently active.

        Defaults return the full list; the keyword-only filter/paging params
        are passed straight through to ``list_conversations_meta`` (see its
        docstring for cursor semantics).

        Resolves each row's display title without opening chat_history.json:
        ``custom_name`` wins, otherwise the cached ``auto_title`` (filled in
        on the first user-message append). Falls back to a one-shot file
        read only for legacy rows that were created before the auto_title
        cache existed and have not been rewritten since (the lifespan
        backfill closes this gap on startup; this fallback covers boots
        between deploy and backfill).

        Args:
            user_id: User's integer ID.
            include_archived: If True, include archived conversations in results.

        Returns:
            List of conversation metadata dicts, most recent first.
        """
        from db.conversation_store import list_conversations_meta

        meta_rows = await list_conversations_meta(
            user_id,
            include_archived=include_archived,
            exclude_projects=exclude_projects,
            include_slack=include_slack,
            include_inference=include_inference,
            limit=limit,
            before=before,
        )

        conversations = []
        for meta in meta_rows:
            conv_id = meta["id"]
            title = ChatStorage._resolve_list_title(conv_id, meta)

            conversations.append({
                "id": conv_id,
                "title": title,
                "created_at": meta["created_at"],
                "last_message_at": meta["last_message_at"],
                "project_id": meta.get("project_id"),
                "routine_id": meta.get("routine_id"),
                "model": meta.get("model"),
                "archived": meta.get("archived", False),
                "custom_name": meta.get("custom_name"),
                "origin": meta.get("origin"),
            })

        return conversations

    @staticmethod
    def _resolve_list_title(conversation_id: str, meta: Dict) -> str:
        """Pick the sidebar title for a conversation row.

        ``custom_name`` wins when set; otherwise the cached ``auto_title``
        (populated by ``append_message`` on first user message). Legacy rows
        where ``auto_title`` is NULL but ``last_message_seq > 0`` fall back
        to a file read so titles remain correct across the rollout window.
        Rows with no on-disk file (corrupted) and no cached title display
        the placeholder ``"New Chat"``.
        """
        custom = meta.get("custom_name")
        if custom:
            return custom
        auto = meta.get("auto_title")
        if auto:
            return auto
        # Legacy fallback: pre-cache rows that already have user messages.
        # The lifespan backfill repairs this lazily; this branch only fires
        # when the backfill hasn't run yet (boot window) or for a brand-new
        # row that hasn't received its first user message.
        if not meta.get("last_message_seq"):
            return "New Chat"
        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        try:
            with open(chat_file, "r") as f:
                chat_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return "New Chat"
        messages = chat_data.get("messages") or []
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content") or ""
                if len(content) > 50:
                    return content[:50] + "..."
                return content[:50] or "New Chat"
        return "New Chat"

    @staticmethod
    def count_user_message_active_days(conversation_id: str) -> int:
        """Number of distinct UTC days on which the user sent a message.

        Whole-lifetime variant of :meth:`user_message_active_days` (see there
        for the read semantics). Missing or unreadable history counts as 0 --
        deleted conversations still appear in the admin cost analytics, just
        without an activity profile.
        """
        return len(ChatStorage.user_message_active_days(conversation_id))

    @staticmethod
    def user_message_active_days(
        conversation_id: str,
        start_day: Optional[str] = None,
        end_day: Optional[str] = None,
    ) -> set:
        """Distinct UTC days ("YYYY-MM-DD") on which the user sent a message.

        Reads chat_history.json and collects unique dates among user-role
        message timestamps (ISO-8601 UTC strings, so the date is the first
        10 characters), optionally clipped to the inclusive
        ``[start_day, end_day]`` ISO-date window (the admin user report
        aggregates per-range). Returns the day set (not a count) so callers
        can union days across conversations without double-counting. Missing
        or unreadable history yields an empty set.
        """
        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        try:
            with open(chat_file, "r") as f:
                chat_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()
        return {
            day
            for msg in chat_data.get("messages") or []
            if isinstance(msg, dict)
            and msg.get("role") == "user"
            and isinstance(msg.get("timestamp"), str)
            and (day := msg["timestamp"][:10])
            and (start_day is None or day >= start_day)
            and (end_day is None or day <= end_day)
        }

    @staticmethod
    def _read_seq_high_water(conversation_id: str) -> int:
        """Compute the highest ``seq`` already on disk for a conversation.

        Pre-migration files have no ``seq`` field on any message; in that
        case we fall back to ``len(messages)`` so the next append starts
        numbering from one past the existing message count. This is
        idempotent across restarts: the JSON file remains the source of
        truth, and the migration backfill plus this read keep the DB
        ``last_message_seq`` aligned.
        """
        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        if not chat_file.exists():
            return 0
        try:
            with open(chat_file, "r") as f:
                chat_data = json.load(f)
        except Exception:
            return 0
        messages = chat_data.get("messages") or []
        if not messages:
            return 0
        max_seq = 0
        any_seq = False
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("seq"), int):
                any_seq = True
                if msg["seq"] > max_seq:
                    max_seq = msg["seq"]
        if any_seq:
            return max_seq
        # Pre-migration file: number from the existing length so the first
        # newly-stamped message gets seq = len(messages) + 1.
        return len(messages)

    @staticmethod
    async def append_message(
        conversation_id: str,
        role: str,
        content: str,
        timestamp: Optional[str] = None,
        attachments: Optional[List[Dict]] = None,
    ) -> tuple[int, dict]:
        """Append a message to conversation history.

        After writing the file, updates last_message_at and last_message_seq
        in the DB so the sidebar sort order and the WS subscribe handler
        both stay current.

        Args:
            conversation_id: Conversation UUID.
            role: Message role ('user' or 'assistant').
            content: Message content.
            timestamp: Optional ISO timestamp (defaults to current UTC time).
            attachments: Optional list of attachment ref dicts (e.g., pasted
                image refs from the composer). When non-empty, the list is
                persisted on the message row as ``attachments`` so the
                transcript can render thumbnails after a reload.

        Returns:
            ``(seq, message)`` tuple for the freshly-appended message. The
            persistent-WS flush callback uses the seq to publish a
            ``message_appended`` event after the disk write.
        """
        from db.conversation_store import (
            set_conversation_auto_title,
            update_last_message_at,
            update_last_message_seq,
        )

        chat_file = ChatStorage._get_chat_history_file(conversation_id)

        if not chat_file.exists():
            raise FileNotFoundError(f"Conversation {conversation_id} not found")

        with open(chat_file, "r") as f:
            chat_data = json.load(f)

        ts = timestamp or utc_timestamp()
        previous_high_water = ChatStorage._read_seq_high_water(conversation_id)
        new_seq = previous_high_water + 1
        message = {
            "role": role,
            "content": content,
            "timestamp": ts,
            "seq": new_seq,
        }
        if attachments:
            message["attachments"] = attachments

        chat_data["messages"].append(message)

        with open(chat_file, "w") as f:
            json.dump(chat_data, f, indent=2)

        # Update DB timestamp so sidebar ordering is always current.
        try:
            await update_last_message_at(conversation_id, _parse_datetime(ts))
        except Exception:
            pass  # Non-fatal; the message was written to disk successfully

        # Mirror the high-water mark into the DB cache.
        try:
            await update_last_message_seq(conversation_id, new_seq)
        except Exception:
            pass

        # Cache the auto-derived sidebar title on the first user message so
        # the conversation-list endpoint doesn't have to re-open the JSON.
        # ``set_conversation_auto_title`` is idempotent (no-op when already set).
        if role == "user":
            # Derive the sidebar title from a flags-stripped copy so a first
            # message that begins with the ``%%flags[...]`` magic line does not
            # show the magic syntax as its leading title text. The visible chat
            # bubble still keeps the magic line (persisted ``content`` above);
            # only the title is cleaned. ``parse_flags_line`` is a no-op for any
            # message without a leading magic line, so this is safe to call
            # unconditionally (including on non-first messages).
            from chat.conversation_flags import parse_flags_line
            _, title_source = parse_flags_line(content)
            title = _compute_auto_title(title_source)
            if title:
                try:
                    await set_conversation_auto_title(conversation_id, title)
                except Exception:
                    pass

        # Publish ``message_appended`` for this message so persistent-WS
        # subscribers (other tabs / the web UI watching a Slack-driven
        # conversation) see the user bubble immediately. ``append_message``
        # is the single-message path used at turn-start; the flush callback
        # in ``_flush_helper.py`` covers ``append_structured_messages`` for
        # the assistant side.
        _publish_appended_to_bus(conversation_id, [(new_seq, message)])

        return new_seq, message

    @staticmethod
    async def append_structured_messages(
        conversation_id: str,
        messages: List[Dict]
    ) -> list[tuple[int, Dict]]:
        """Append structured messages to conversation history.

        Messages can have different types:
        - type: "text"        — regular text with role and content
        - type: "tool_use"    — tool invocation with tool_name, tool_input, tool_id
        - type: "tool_result" — tool output with tool_id, tool_output

        Each appended message is stamped with a per-conversation monotonic
        ``seq``. The new high-water seq is mirrored into the DB cache so the
        persistent-WS subscribe handler can answer up_to_date / catchup /
        resync without re-reading the file.

        After writing the file, updates last_message_at in the DB to the
        timestamp of the last message in the batch.

        Args:
            conversation_id: Conversation UUID.
            messages: List of message dicts, each with a 'type' field.

        Returns:
            ``[(seq, message), ...]`` for each newly appended message, in
            append order. Callers (the shared flush callback) use the seq
            values to publish ``message_appended`` events on the persistent
            WS bus after the disk write succeeds.
        """
        from db.conversation_store import (
            set_conversation_auto_title,
            update_last_message_at,
            update_last_message_seq,
        )

        chat_file = ChatStorage._get_chat_history_file(conversation_id)

        if not chat_file.exists():
            raise FileNotFoundError(f"Conversation {conversation_id} not found")

        with open(chat_file, "r") as f:
            chat_data = json.load(f)

        previous_high_water = ChatStorage._read_seq_high_water(conversation_id)
        latest_ts: Optional[str] = None
        appended: list[tuple[int, Dict]] = []
        next_seq = previous_high_water
        first_user_content: Optional[str] = None
        for message in messages:
            if "timestamp" not in message:
                message["timestamp"] = utc_timestamp()
            latest_ts = message["timestamp"]
            next_seq += 1
            message["seq"] = next_seq
            chat_data["messages"].append(message)
            appended.append((next_seq, message))
            if (
                first_user_content is None
                and message.get("role") == "user"
                and isinstance(message.get("content"), str)
            ):
                first_user_content = message["content"]

        with open(chat_file, "w") as f:
            json.dump(chat_data, f, indent=2)

        # Update DB timestamp to the last message's timestamp
        if latest_ts:
            try:
                await update_last_message_at(conversation_id, _parse_datetime(latest_ts))
            except Exception:
                pass  # Non-fatal; the messages were written to disk successfully

        if appended:
            try:
                await update_last_message_seq(conversation_id, appended[-1][0])
            except Exception:
                pass

        # Cache the auto-derived sidebar title from the first user message in
        # this batch (idempotent across batches; no-op when already set). Strip
        # any leading ``%%flags[...]`` magic line for title purposes only so the
        # sidebar never shows the magic syntax (no-op for normal messages).
        if first_user_content is not None:
            from chat.conversation_flags import parse_flags_line
            _, title_source = parse_flags_line(first_user_content)
            title = _compute_auto_title(title_source)
            if title:
                try:
                    await set_conversation_auto_title(conversation_id, title)
                except Exception:
                    pass

        return appended

    @staticmethod
    def update_action_request_message(
        conversation_id: str,
        request_id: int,
        status: str,
        result: Optional[Dict] = None,
        feedback: Optional[str] = None,
    ) -> None:
        """Update the persisted ``action_request`` chat-history entry in place.

        Locates the matching ``{"type": "action_request", "request_id": N}``
        entry in chat_history.json and rewrites its ``status`` (and adds
        ``result`` / ``feedback`` fields when provided) so a page reload
        or replay shows the same outcome the live UI shows. Best-effort:
        a missing file or missing entry is a silent no-op.
        """
        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        if not chat_file.exists():
            return

        try:
            with open(chat_file, "r") as f:
                chat_data = json.load(f)
        except Exception:
            return

        messages = chat_data.get("messages") or []
        changed = False
        for msg in messages:
            if (
                isinstance(msg, dict)
                and msg.get("type") == "action_request"
                and msg.get("request_id") == request_id
            ):
                msg["status"] = status
                if result is not None:
                    msg["result"] = result
                if feedback is not None:
                    msg["feedback"] = feedback
                changed = True
                break

        if not changed:
            return

        try:
            with open(chat_file, "w") as f:
                json.dump(chat_data, f, indent=2)
        except Exception:
            # Best-effort: a failed disk write must not break the resolve.
            pass

    @staticmethod
    def set_guide_snapshot(
        conversation_id: str,
        guide_id: str,
        guide_name: str,
        guide_content: str,
    ) -> None:
        """Set the guide snapshot on a conversation (called once on first message).

        Only sets the snapshot if it hasn't been set already (idempotent).

        Args:
            conversation_id: Conversation UUID.
            guide_id: Guide UUID.
            guide_name: Guide display name.
            guide_content: Guide system prompt content at time of snapshot.
        """
        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        if not chat_file.exists():
            return

        with open(chat_file, "r") as f:
            chat_data = json.load(f)

        # Only set if not already snapshotted
        if "guide_id" not in chat_data or chat_data["guide_id"] is None:
            chat_data["guide_id"] = guide_id
            chat_data["guide_snapshot"] = {
                "name": guide_name,
                "content": guide_content,
            }

            with open(chat_file, "w") as f:
                json.dump(chat_data, f, indent=2)

    @staticmethod
    def get_guide_snapshot(
        conversation_id: str,
    ) -> Optional[Dict]:
        """Get the guide snapshot from a conversation.

        Args:
            conversation_id: Conversation UUID.

        Returns:
            Dict with 'guide_id' and 'guide_snapshot' keys, or None if not set.
        """
        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        if not chat_file.exists():
            return None

        with open(chat_file, "r") as f:
            chat_data = json.load(f)

        guide_id = chat_data.get("guide_id")
        if guide_id:
            return {
                "guide_id": guide_id,
                "guide_snapshot": chat_data.get("guide_snapshot"),
            }
        return None

    # ------------------------------------------------------------------
    # Per-conversation notice flags (chat_history.json top level)
    # ------------------------------------------------------------------

    @staticmethod
    def get_conversation_flags(conversation_id: str) -> Dict[str, bool]:
        """Return the notice flags set on a conversation.

        Reads the top-level ``CONVERSATION_NOTICE_FLAGS`` keys of
        chat_history.json and returns those that are present, as booleans.
        A missing or unreadable chat_history.json yields ``{}``.

        These are NOT the user-chosen ``conversations.flags`` opt-in
        behaviors (chat/conversation_flags.py); they are server-set,
        persisted-once markers read by the system prompt builder. See the
        ``CONVERSATION_NOTICE_FLAGS`` comment for the legacy-conversation
        rule built on ``own_workspace``.
        """
        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        if not chat_file.exists():
            return {}
        try:
            with open(chat_file, "r") as f:
                chat_data = json.load(f)
        except Exception:
            return {}
        if not isinstance(chat_data, dict):
            return {}
        return {
            name: bool(chat_data[name])
            for name in CONVERSATION_NOTICE_FLAGS
            if name in chat_data
        }

    @staticmethod
    def set_conversation_flag(conversation_id: str, name: str, value: bool) -> None:
        """Set one notice flag at the top level of chat_history.json.

        Rewrites the file the same way ``append_message`` does, leaving
        ``messages`` and every other key untouched. Skips the write when the
        stored value already matches. A missing chat_history.json is a
        logged no-op: the file is only ever created by the ``create_*``
        methods, and a flag on a conversation without history has nothing
        to annotate. An unreadable or non-object chat_history.json is also a
        logged no-op (nothing is written), mirroring
        ``get_conversation_flags``.

        Concurrency note: this is the same synchronous, non-atomic
        read-modify-write as the other chat_history.json writers. It is safe
        only because there is no ``await`` between the read and the write
        and everything runs on the single event loop, so it cannot
        interleave with a message append; keep it fully synchronous.

        Raises:
            ValueError: ``name`` is not one of ``CONVERSATION_NOTICE_FLAGS``.
        """
        if name not in CONVERSATION_NOTICE_FLAGS:
            raise ValueError(f"Unknown conversation flag: {name!r}")
        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        if not chat_file.exists():
            logger.warning(
                "set_conversation_flag(%s, %s): no chat_history.json; skipped",
                conversation_id, name,
            )
            return
        try:
            with open(chat_file, "r") as f:
                chat_data = json.load(f)
        except Exception:
            logger.warning(
                "set_conversation_flag(%s, %s): unreadable chat_history.json; skipped",
                conversation_id, name, exc_info=True,
            )
            return
        if not isinstance(chat_data, dict):
            logger.warning(
                "set_conversation_flag(%s, %s): chat_history.json is not an object; skipped",
                conversation_id, name,
            )
            return
        value = bool(value)
        if chat_data.get(name) is value:
            return
        chat_data[name] = value
        with open(chat_file, "w") as f:
            json.dump(chat_data, f, indent=2)

    # ------------------------------------------------------------------
    # Loaded skills (per-conversation, manually loaded by user)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_loaded_skills_file(conversation_id: str) -> Path:
        """Return the path to loaded_skills.json for a conversation."""
        return ChatStorage._get_conversation_dir(conversation_id) / "loaded_skills.json"

    @staticmethod
    def get_loaded_skill_ids(conversation_id: str) -> list[str]:
        """Read the list of manually loaded skill IDs for a conversation.

        Returns an empty list if the file does not exist (no skills loaded yet).

        Args:
            conversation_id: Conversation UUID.

        Returns:
            List of skill ID strings.
        """
        skills_file = ChatStorage._get_loaded_skills_file(conversation_id)
        if not skills_file.exists():
            return []

        try:
            with open(skills_file, "r") as f:
                data = json.load(f)
            return data.get("skill_ids", [])
        except (json.JSONDecodeError, KeyError):
            return []

    @staticmethod
    def add_loaded_skill_ids(conversation_id: str, skill_ids: list[str]) -> None:
        """Append skill IDs to the loaded skills file, deduplicating.

        Creates the file if it does not exist. Existing IDs are preserved;
        new IDs are appended (order-preserving dedup).

        Args:
            conversation_id: Conversation UUID.
            skill_ids: List of skill ID strings to add.
        """
        if not skill_ids:
            return

        existing = ChatStorage.get_loaded_skill_ids(conversation_id)
        existing_set = set(existing)
        merged = existing + [sid for sid in skill_ids if sid not in existing_set]

        skills_file = ChatStorage._get_loaded_skills_file(conversation_id)
        with open(skills_file, "w") as f:
            json.dump({"skill_ids": merged}, f, indent=2)

    # ------------------------------------------------------------------
    # Skill reads (per-conversation skills whose full content the model
    # has seen; gates edit_skill content edits)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_skill_reads_file(conversation_id: str) -> Path:
        """Return the path to skill_reads.json for a conversation."""
        return ChatStorage._get_conversation_dir(conversation_id) / "skill_reads.json"

    @staticmethod
    def get_skill_read_ids(conversation_id: str) -> list[str]:
        """Read the list of skill IDs whose content the model has seen.

        Populated by every path that puts a skill's full body in front of
        the model: system-prompt auto-loads, composer-loaded skills, the
        load_skills / get_skill tools, and create_skill (the model
        authored the content). Returns an empty list if the file does not
        exist (nothing read yet).

        Args:
            conversation_id: Conversation UUID.

        Returns:
            List of skill ID strings.
        """
        reads_file = ChatStorage._get_skill_reads_file(conversation_id)
        if not reads_file.exists():
            return []

        try:
            with open(reads_file, "r") as f:
                data = json.load(f)
            return data.get("skill_ids", [])
        except (json.JSONDecodeError, KeyError):
            return []

    @staticmethod
    def add_skill_read_ids(conversation_id: str, skill_ids: list[str]) -> None:
        """Append skill IDs to the skill reads file, deduplicating.

        Creates the file if it does not exist. Existing IDs are preserved;
        new IDs are appended (order-preserving dedup). Skips the write
        entirely when every ID is already recorded (this runs on every
        system-prompt build).

        Concurrency note: same single-event-loop guarantee as
        ``add_workspace_read_paths`` below -- this method must stay fully
        synchronous.

        Args:
            conversation_id: Conversation UUID.
            skill_ids: List of skill ID strings to add.
        """
        if not skill_ids:
            return

        existing = ChatStorage.get_skill_read_ids(conversation_id)
        existing_set = set(existing)
        new_ids: list[str] = []
        for sid in skill_ids:
            if sid not in existing_set:
                existing_set.add(sid)
                new_ids.append(sid)
        if not new_ids:
            return

        reads_file = ChatStorage._get_skill_reads_file(conversation_id)
        with open(reads_file, "w") as f:
            json.dump({"skill_ids": existing + new_ids}, f, indent=2)

    # ------------------------------------------------------------------
    # Workspace reads (per-conversation files the model has read/written;
    # gates edit_workspace_file)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_workspace_reads_file(conversation_id: str) -> Path:
        """Return the path to workspace_reads.json for a conversation."""
        return ChatStorage._get_conversation_dir(conversation_id) / "workspace_reads.json"

    @staticmethod
    def get_workspace_read_paths(conversation_id: str) -> list[str]:
        """Read the list of workspace file paths the model has read or written.

        Paths are canonical workspace-relative strings. Returns an empty
        list if the file does not exist (nothing read yet).

        Args:
            conversation_id: Conversation UUID.

        Returns:
            List of workspace-relative path strings.
        """
        reads_file = ChatStorage._get_workspace_reads_file(conversation_id)
        if not reads_file.exists():
            return []

        try:
            with open(reads_file, "r") as f:
                data = json.load(f)
            return data.get("paths", [])
        except (json.JSONDecodeError, KeyError):
            return []

    @staticmethod
    def add_workspace_read_paths(conversation_id: str, paths: list[str]) -> None:
        """Append workspace-relative paths to the workspace reads file, deduplicating.

        Creates the file if it does not exist. Existing paths are preserved;
        new paths are appended (order-preserving dedup).

        Concurrency note: parallel sub-agents are asyncio coroutines on the
        single server event loop (no threads, no extra processes), so this
        synchronous read-modify-write cannot be interleaved by another tool
        call and needs no lock. That only holds while this method stays
        fully synchronous -- do NOT make it async or add awaits/thread
        offloads between the read and the write below.

        Args:
            conversation_id: Conversation UUID.
            paths: List of canonical workspace-relative path strings to add.
        """
        if not paths:
            return

        existing = ChatStorage.get_workspace_read_paths(conversation_id)
        existing_set = set(existing)
        merged = existing + [p for p in paths if p not in existing_set]

        reads_file = ChatStorage._get_workspace_reads_file(conversation_id)
        with open(reads_file, "w") as f:
            json.dump({"paths": merged}, f, indent=2)

    # ------------------------------------------------------------------
    # Doc reads (per-conversation Quest Docs whose body the model has
    # read or authored; gates edit_doc, tool and action request alike)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_doc_reads_file(conversation_id: str) -> Path:
        """Return the path to doc_reads.json for a conversation."""
        return ChatStorage._get_conversation_dir(conversation_id) / "doc_reads.json"

    @staticmethod
    def get_doc_read_ids(conversation_id: str) -> list[str]:
        """Read the list of doc IDs whose body the model has seen.

        Populated by ``read_doc`` and ``create_doc`` (the model authored the
        content); ``search_docs`` snippets do not count. Returns an empty
        list if the file does not exist (nothing read yet).

        Args:
            conversation_id: Conversation UUID.

        Returns:
            List of doc ID strings.
        """
        reads_file = ChatStorage._get_doc_reads_file(conversation_id)
        if not reads_file.exists():
            return []

        try:
            with open(reads_file, "r") as f:
                data = json.load(f)
            return data.get("doc_ids", [])
        except (json.JSONDecodeError, KeyError, AttributeError):
            return []

    @staticmethod
    def add_doc_read_ids(conversation_id: str, doc_ids: list[str]) -> None:
        """Append doc IDs to the doc reads file, deduplicating.

        Creates the file if it does not exist. Existing IDs are preserved;
        new IDs are appended (order-preserving dedup). Skips the write
        entirely when every ID is already recorded.

        Concurrency note: same single-event-loop guarantee as
        ``add_workspace_read_paths`` above -- this method must stay fully
        synchronous.

        Args:
            conversation_id: Conversation UUID.
            doc_ids: List of doc ID strings to add.
        """
        if not doc_ids:
            return

        existing = ChatStorage.get_doc_read_ids(conversation_id)
        existing_set = set(existing)
        new_ids: list[str] = []
        for did in doc_ids:
            if did not in existing_set:
                existing_set.add(did)
                new_ids.append(did)
        if not new_ids:
            return

        reads_file = ChatStorage._get_doc_reads_file(conversation_id)
        with open(reads_file, "w") as f:
            json.dump({"doc_ids": existing + new_ids}, f, indent=2)

    # ------------------------------------------------------------------
    # System prompt (persisted per-conversation)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_system_prompt_file(conversation_id: str) -> Path:
        """Return the path to system_prompt.txt for a conversation."""
        return ChatStorage._get_conversation_dir(conversation_id) / "system_prompt.txt"

    @staticmethod
    def set_system_prompt(conversation_id: str, system_prompt: str) -> None:
        """Write the system prompt text to disk.

        Overwrites any existing file so the saved copy always reflects
        the latest prompt that would be used for a new session.

        Args:
            conversation_id: Conversation UUID.
            system_prompt: Full system prompt text.
        """
        prompt_file = ChatStorage._get_system_prompt_file(conversation_id)
        with open(prompt_file, "w") as f:
            f.write(system_prompt)

    @staticmethod
    def get_system_prompt_text(conversation_id: str) -> str | None:
        """Read and return the saved system prompt text.

        Returns None if the file does not exist (no messages sent yet).

        Args:
            conversation_id: Conversation UUID.

        Returns:
            The system prompt text, or None.
        """
        prompt_file = ChatStorage._get_system_prompt_file(conversation_id)
        if not prompt_file.exists():
            return None
        with open(prompt_file, "r") as f:
            return f.read()

    # ------------------------------------------------------------------
    # Project support
    # ------------------------------------------------------------------

    @staticmethod
    async def search_conversations(
        user_id: int,
        query: str,
        max_results: int = 50,
        max_per_conversation: int = 3,
    ) -> Dict:
        """Search across all conversations for a user by substring match.

        Scans chat_history.json files for messages containing the query
        (case-insensitive). Returns matching snippets with metadata.

        Args:
            user_id: User's integer ID.
            query: Search query string (minimum 2 characters).
            max_results: Maximum total results to return.
            max_per_conversation: Maximum matches per conversation.

        Returns:
            Dict with 'results', 'total_matches', and 'query' keys.
        """
        import html as html_module
        from db.conversation_store import list_conversations_meta

        query_lower = query.lower()
        meta_rows = await list_conversations_meta(user_id, include_archived=True)

        all_results = []
        total_matches = 0

        # Roles whose content is searchable plain text
        searchable_roles = {"user", "assistant"}
        # Message types to exclude (structured data, not user-facing text)
        excluded_types = {
            "tool_use", "tool_result",
            "action_request", "stats", "error", "interrupted",
        }

        for meta in meta_rows:
            conv_id = meta["id"]
            chat_file = ChatStorage._get_chat_history_file(conv_id)

            try:
                with open(chat_file, "r") as f:
                    chat_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                continue

            messages = chat_data.get("messages", [])

            # Derive conversation title: custom_name or first user message
            title = meta.get("custom_name") or "New Chat"
            if meta.get("custom_name") is None:
                for msg in messages:
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        title = content[:50]
                        if len(content) > 50:
                            title += "..."
                        break

            conv_matches = 0
            for msg_index, msg in enumerate(messages):
                msg_type = msg.get("type")
                msg_role = msg.get("role", "")

                # Skip non-searchable message types
                if msg_type in excluded_types:
                    continue
                if msg_role not in searchable_roles:
                    continue

                content = msg.get("content", "")
                if not isinstance(content, str) or not content:
                    continue

                content_lower = content.lower()
                match_pos = content_lower.find(query_lower)
                if match_pos == -1:
                    continue

                total_matches += 1
                conv_matches += 1

                if conv_matches <= max_per_conversation and len(all_results) < max_results:
                    # Build snippet: ~200 chars centered around match
                    snippet_half = 100
                    start = max(0, match_pos - snippet_half)
                    end = min(len(content), match_pos + len(query) + snippet_half)
                    raw_snippet = content[start:end]

                    # Add ellipsis if truncated
                    if start > 0:
                        raw_snippet = "..." + raw_snippet
                    if end < len(content):
                        raw_snippet = raw_snippet + "..."

                    # HTML-escape the snippet, then wrap matches in <mark>
                    escaped = html_module.escape(raw_snippet)
                    query_escaped = html_module.escape(query)
                    # Case-insensitive replacement for highlighting
                    highlighted = escaped
                    idx = highlighted.lower().find(query_escaped.lower())
                    if idx != -1:
                        original_match = highlighted[idx:idx + len(query_escaped)]
                        highlighted = (
                            highlighted[:idx]
                            + "<mark>" + original_match + "</mark>"
                            + highlighted[idx + len(query_escaped):]
                        )

                    all_results.append({
                        "conversation_id": conv_id,
                        "conversation_title": title,
                        "project_id": meta.get("project_id"),
                        "message_index": msg_index,
                        "message_role": msg_role,
                        "snippet": highlighted,
                        "timestamp": msg.get("timestamp", meta.get("last_message_at", "")),
                        "archived": meta.get("archived", False),
                    })

            # Early exit if we have enough results
            if len(all_results) >= max_results:
                break

        return {
            "results": all_results,
            "total_matches": total_matches,
            "query": query,
        }

    @staticmethod
    async def create_project_conversation(
        user_id: int,
        project_id: str,
        routine_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> tuple[str, str]:
        """Create a new conversation within a project.

        Creates the conversation directory, its (empty) conversation
        workspace and chat_history.json, and inserts a row into the
        conversations table linked to the project.

        Args:
            user_id: User's integer ID.
            project_id: Project UUID string.
            routine_id: Optional routine UUID that created this conversation.
            model: Optional LLM model ID to set on the conversation.

        Returns:
            ``(conversation_id, created_at_iso)``.
        """
        from db.conversation_store import create_conversation as db_create_conversation

        conversation_id = str(uuid.uuid4())
        conversation_dir = ChatStorage._get_conversation_dir(conversation_id)
        conversation_dir.mkdir(parents=True, exist_ok=True)
        # The conversation workspace exists from the start. Its presence is
        # NOT the post-cutover marker (opening any conversation mkdirs it);
        # the explicit ``own_workspace`` flag below is -- see
        # CONVERSATION_NOTICE_FLAGS.
        ChatStorage.get_conversation_workspace_root(conversation_id).mkdir(exist_ok=True)

        created_at_str = utc_timestamp()

        chat_data = {
            "id": conversation_id,
            "user_id": user_id,
            "project_id": project_id,
            "routine_id": routine_id,
            "created_at": created_at_str,
            "own_workspace": True,
            "messages": []
        }

        chat_file = ChatStorage._get_chat_history_file(conversation_id)
        with open(chat_file, "w") as f:
            json.dump(chat_data, f, indent=2)

        # Routine runs are named after their routine (the prompt is the same
        # for every run, and the sidebar groups them under the routine
        # anyway), so the model is not asked to set a conversation name on
        # routine runs -- see ``is_routine`` in get_system_prompt().
        custom_name: Optional[str] = None
        if routine_id:
            from db.routine_store import get_routine
            routine = await get_routine(user_id, routine_id)
            if routine and routine.get("name"):
                custom_name = routine["name"]

        created_at_dt = _parse_datetime(created_at_str)
        await db_create_conversation(
            user_id, conversation_id, created_at_dt,
            project_id=project_id, routine_id=routine_id, model=model,
            custom_name=custom_name,
        )

        return conversation_id, created_at_str

    @staticmethod
    def create_project_workspace(project_id: str) -> Path:
        """Create the project workspace root (and its parents) for a project.

        Args:
            project_id: Project UUID.

        Returns:
            The project workspace root, data/projects/{project_id}/workspace/workspace/
            (``get_project_workspace_root``).
        """
        root = ChatStorage.get_project_workspace_root(project_id)
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    async def copy_workspace_files(src_root: Path, dst_root: Path) -> None:
        """Copy the contents of one workspace root into another.

        Both arguments are browsable roots as returned by
        ``get_conversation_workspace_root`` / ``get_project_workspace_root``;
        nothing is appended to them. ``dst_root`` is created if missing and
        existing entries there are merged into (``dirs_exist_ok``). A missing
        ``src_root`` is a no-op.

        Skipped entries:

        * the hidden ``.responses/`` dir at the source root: it holds
          authed_get/authed_post response bodies tied to the source
          conversation's tool calls, which the destination can't interpret;
        * symlinks at any depth: they are banned from workspaces (see
          chat/workspace_symlinks.py) and copytree would otherwise
          dereference them and copy their host target;
        * special files (FIFOs, sockets, devices) at any depth: copying a
          writer-less FIFO would block forever.

        Args:
            src_root: Workspace root to copy from.
            dst_root: Workspace root to copy into.
        """
        import asyncio
        import shutil

        if not src_root.is_dir():
            return

        def _ignore(dir_path, names):
            ignored = set()
            for name in names:
                entry = Path(dir_path) / name
                if entry.is_symlink():
                    ignored.add(name)
                elif not (entry.is_dir() or entry.is_file()):
                    ignored.add(name)
            if Path(dir_path) == src_root:
                ignored.update({".responses"} & set(names))
            return ignored

        # Off-thread: workspaces can hold hundreds of MB of files.
        await asyncio.to_thread(
            shutil.copytree, src_root, dst_root, ignore=_ignore, dirs_exist_ok=True
        )

    @staticmethod
    def delete_project_workspace(project_id: str) -> None:
        """Delete the workspace directory for a project.

        Args:
            project_id: Project UUID.
        """
        import shutil
        project_dir = ChatStorage.get_project_dir(project_id)
        if project_dir.exists():
            shutil.rmtree(project_dir)

    @staticmethod
    async def list_project_conversations(project_id: str, include_archived: bool = False) -> List[Dict]:
        """List all conversations for a project, ordered by most recently active.

        Queries the conversations table for the list and sort order, then
        reads each chat_history.json to extract the conversation title.

        Args:
            project_id: Project UUID string.
            include_archived: If True, include archived conversations in results.

        Returns:
            List of conversation metadata dicts, most recent first.
        """
        from db.conversation_store import list_project_conversations_meta

        meta_rows = await list_project_conversations_meta(project_id, include_archived=include_archived)

        conversations = []
        for meta in meta_rows:
            conv_id = meta["id"]
            title = ChatStorage._resolve_list_title(conv_id, meta)

            conversations.append({
                "id": conv_id,
                "title": title,
                "created_at": meta["created_at"],
                "last_message_at": meta["last_message_at"],
                "project_id": meta.get("project_id"),
                "routine_id": meta.get("routine_id"),
                "model": meta.get("model"),
                "archived": meta.get("archived", False),
                "custom_name": meta.get("custom_name"),
                "origin": meta.get("origin"),
            })

        return conversations
