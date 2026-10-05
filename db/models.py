"""SQLAlchemy ORM models for user data."""

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import String, Text, DateTime, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from config.encryption import hash_api_key
from config.password_hashing import password_fingerprint
from db.encrypted_types import EncryptedJSON, EncryptedText


class ModelId(StrEnum):
    """Vertex model identifiers (Gemini, Anthropic) -- a historical enum kept
    for readability of call rows; NOT the source of truth for what is
    selectable (see chat/llm/config.py resolve_model). Instance-served
    models (OpenRouter) carry ``<instance_id>:<wire_id>`` ids and are not
    listed here."""
    GEMINI_3_1_PRO = "gemini-3.1-pro-preview"  # Deprecated: hidden from the model selector; existing rows still run it (now on Vertex)
    GEMINI_3_PRO = "gemini-3-pro-preview"  # Deprecated: replaced by GEMINI_3_1_PRO for new conversations
    GEMINI_3_FLASH = "gemini-3-flash-preview"  # Deprecated: hidden from the model selector; existing rows still run it
    GEMINI_3_1_FLASH_LITE = "gemini-3.1-flash-lite-preview"  # Deprecated: hidden from the model selector; existing rows still run it
    GEMINI_3_5_FLASH = "gemini-3.5-flash"  # Deprecated: hidden from the model selector; existing rows still run it
    GEMINI_3_5_FLASH_LITE = "gemini-3.5-flash-lite"
    GEMINI_3_6_FLASH = "gemini-3.6-flash"
    GEMINI_3_7_FLASH = "gemini-3.7-flash"
    GEMINI_3_8_FLASH = "gemini-3.8-flash"
    CLAUDE_HAIKU_4_5 = "claude-haiku-4.5"
    CLAUDE_SONNET_4_6 = "claude-sonnet-4-6"
    CLAUDE_OPUS_4_6 = "claude-opus-4-6"
    CLAUDE_OPUS_4_7 = "claude-opus-4-7"
    CLAUDE_OPUS_4_8 = "claude-opus-4-8"
    CLAUDE_SONNET_5 = "claude-sonnet-5"
    CLAUDE_SONNET_5_5 = "claude-sonnet-5-5"
    CLAUDE_OPUS_5 = "claude-opus-5"
    CLAUDE_OPUS_5_5 = "claude-opus-5-5"


class ApiCallType(StrEnum):
    """Discriminator for the type of Gemini API call."""
    TOP_LEVEL = "top_level"
    SUB_AGENT = "sub_agent"


class ActionRequestType(StrEnum):
    """Known CORE action request types.

    This enum is deliberately NOT the full universe of valid request types:
    the ``action_requests.request_type`` column is a plain string, and
    plugin-registered types (``<plugin id>_``-prefixed strings admitted via
    chat.action_request_types.registry / chat.llm.tool_schemas.
    register_action_request_type) are equally valid at runtime -- e.g. the
    grandfathered ``send_twitter_dm`` (Twitter/X),
    ``send_slack_message`` / ``send_slack_dm`` (Slack), and
    ``send_telegram_message`` (Telegram) names.
    Keep core types here; never validate a
    request type against this enum alone -- the handler registry is the
    source of truth.
    """
    CREATE_CALENDAR_INVITE = "create_calendar_invite"
    EDIT_CALENDAR_EVENT = "edit_calendar_event"
    CREATE_MEMORY = "create_memory"
    UPLOAD_TO_DRIVE = "upload_to_drive"
    CREATE_DRIVE_FOLDER = "create_drive_folder"
    CREATE_SKILL = "create_skill"
    EDIT_SKILL = "edit_skill"
    CREATE_ROUTINE = "create_routine"
    EDIT_ROUTINE = "edit_routine"
    EDIT_GOOGLE_SPREADSHEET = "edit_google_spreadsheet"
    RESET_GCP_INSTANCE = "reset_gcp_instance"
    RUN_USER_SUBAGENT = "run_user_subagent"
    SUBAGENT_RETURN = "subagent_return"
    WRITE_DOC = "write_doc"


class ActionRequestStatus(StrEnum):
    """Status of an action request."""
    OPEN = "open"
    # Revise (deny + feedback) -- and the legacy bare deny -- resume the
    # model right away with the verdict.
    DENIED = "denied"
    EXECUTED = "executed"
    # Stop: the user halted the conversation loop instead of deciding. The
    # request is discarded, the dangling create_action_request tool_use
    # stays open, and it is closed with a ``stopped`` verdict only when the
    # user's next message arrives (see chat/action_request_routes.py).
    STOPPED = "stopped"


class ToolWaitHandleKind(StrEnum):
    """Discriminator values for tool_wait_handles.kind."""
    # Slack threaded-reply suspends; payload carries (channel, thread_ts,
    # posted_text, posted_ts), response carries {"user_reply": "..."}
    # filled in by the Socket Mode handler when the user replies.
    SLACK_REPLY = "slack_reply"
    # Action-request approve/revise/stop suspends; payload carries
    # (request_id, request_type, params); response carries
    # {"verdict": "executed"|"denied"|"stopped", "request_id", "result": {...}}.
    # correlation_kind / correlation_id are populated at create time
    # (request_id is known up front).
    ACTION_REQUEST = "action_request"
    # Cross-user subagent run: created on the caller's side when an approved
    # run_user_subagent action request launches a subagent conversation in the
    # target user's account. The caller's model blocks on it via
    # wait_for_handles; it resolves when the target user approves or denies
    # the subagent's return call. payload carries (run_id,
    # subagent_conversation_id, target_user_email); response carries
    # {"status": "returned"|"denied"|"failed", "response", "files": [...]}.
    USER_SUBAGENT = "user_subagent"


class ToolWaitHandleStatus(StrEnum):
    """Lifecycle status of a tool wait handle."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    # Action-request Stop: the row is terminal (the composer unlocks, the
    # Requests inbox no longer lists it) but the resume machinery treats
    # it as "wait for the user's next message" rather than a wake-up.
    STOPPED = "stopped"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # Primary key: auto-incrementing integer
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Email address (unique, indexed for fast lookup)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)

    # Profile
    name: Mapped[str] = mapped_column(String(255), default="")

    # Google account subject identifier (the OAuth `sub` claim; the v2
    # userinfo endpoint's `id` field). Captured at login and used by
    # integrations that need a stable Google identity for user
    # resolution. NULL until the user's next Google login.
    google_sub: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, index=True, nullable=True, default=None
    )

    # API key for Bearer token auth. Encrypted at rest (it is injected in
    # plaintext as QUEST_API_KEY into sandbox containers, so it cannot be
    # hash-only); lookups go through ``api_key_hash``. The unique index
    # predates encryption -- random-nonce ciphertexts are unique anyway.
    api_key: Mapped[str] = mapped_column(
        EncryptedText("users.api_key", length=64), unique=True, index=True
    )

    # SHA-256 hex digest of api_key -- the lookup key for Bearer auth
    # (config.encryption.hash_api_key). Maintained by the validator below
    # whenever api_key is set. Nullable only for the migration window.
    api_key_hash: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )

    @validates("api_key")
    def _sync_api_key_hash(self, _key: str, value: Optional[str]) -> Optional[str]:
        self.api_key_hash = hash_api_key(value) if value else None
        return value

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # User settings (JSON blob: {"custom_system_prompt": "..."})
    settings: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)

    # Google OAuth (login) - JSON blob, encrypted at rest
    google_oauth: Mapped[Optional[dict]] = mapped_column(
        EncryptedJSON("users.google_oauth"), nullable=True
    )

    # Google Services OAuth (Gmail, Calendar, etc.) - JSON blob, encrypted at rest
    google_services_oauth: Mapped[Optional[dict]] = mapped_column(
        EncryptedJSON("users.google_services_oauth"), nullable=True
    )

    # Slack OAuth moved to a user_service_credentials row (service="slack",
    # oauth_blob) when Slack became the in-tree plugin -- see migration
    # f3a9c5d81b42. The Telegram Telethon session followed the same path
    # (service="telegram", oauth_blob {"session": ...}) -- migration
    # d7a1f3c9e2b4.

    # Airtable Personal Access Token, encrypted at rest
    airtable_token: Mapped[Optional[str]] = mapped_column(
        EncryptedText("users.airtable_token", length=255), nullable=True
    )

    # Ramp OAuth 2.0 - JSON blob (access_token, refresh_token, expires_at,
    # etc.), encrypted at rest
    ramp_oauth: Mapped[Optional[dict]] = mapped_column(
        EncryptedJSON("users.ramp_oauth"), nullable=True
    )

    # Email/password sign-in (login_method "password"): scrypt hash string
    # from config/password_hashing.py. NULL for accounts that never set a
    # password (Google sign-in, dev logins, pending invites). Kept when the
    # deployment switches to Google sign-in -- password logins are refused
    # by the login_method check, not by clearing hashes.
    password_hash: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, default=None
    )

    def to_dict(self) -> dict:
        """Convert to dictionary matching the current user dict format.

        This ensures backward compatibility with all code that currently
        receives user dicts from load_users()/get_user_by_*().
        """
        d = {
            "id": self.id,
            "email": self.email,
            "name": self.name or "",
            "api_key": self.api_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "settings": self.settings or {},
        }
        if self.google_sub is not None:
            d["google_sub"] = self.google_sub
        if self.google_oauth is not None:
            d["google_oauth"] = self.google_oauth
        if self.google_services_oauth is not None:
            d["google_services_oauth"] = self.google_services_oauth
        if self.airtable_token is not None:
            d["airtable_token"] = self.airtable_token
        if self.ramp_oauth is not None:
            d["ramp_oauth"] = self.ramp_oauth
        if self.password_hash is not None:
            # Only the fingerprint rides on the user dict (session-cookie
            # binding); the hash itself never leaves db/password_store.py.
            d["password_fp"] = password_fingerprint(self.password_hash)
        return d


class UserServiceCredential(Base):
    """A per-user credential for a plugin-contributed service.

    Core integrations store per-user credentials in dedicated ``users``
    columns (``ramp_oauth``, ``airtable_token``, ...); plugins cannot
    add columns, so their per-user connections live here -- one row per
    (user, service). ``secret`` holds an API key (the ``api_key``
    connection kind); ``oauth_blob`` holds token JSON for the ``oauth``
    kind (e.g. the GitHub plugin's access token). Both are encrypted at
    rest (db/encrypted_types.py), like the per-user credential columns.
    """

    __tablename__ = "user_service_credentials"
    __table_args__ = (
        sa.Index("ix_user_service_credentials_user_id", "user_id"),
        sa.Index(
            "ix_user_service_credentials_user_id_service",
            "user_id", "service", unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The owning user (FK to users.id with CASCADE)
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The service key -- a plugin id (e.g. "acme_tracker")
    service: Mapped[str] = mapped_column(String(64), nullable=False)

    # API key / token for the "api_key" connection kind, encrypted at rest
    secret: Mapped[Optional[str]] = mapped_column(
        EncryptedText("user_service_credentials.secret"), nullable=True
    )

    # OAuth token JSON for the "oauth" connection kind, encrypted at rest.
    # Opaque ciphertext in SQL: lookups into the blob (e.g. the Slack
    # user_id reverse lookup) must decrypt in Python, not json_extract.
    oauth_blob: Mapped[Optional[dict]] = mapped_column(
        EncryptedJSON("user_service_credentials.oauth_blob"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "secret": self.secret,
            "oauth_blob": self.oauth_blob,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class InferenceApiKey(Base):
    """A named bearer token for the one-shot Inference API endpoint.

    Users mint these in Settings > Inference API. The raw token is shown
    exactly once at creation and only its SHA-256 hex digest is stored;
    ``token_hint`` (the last few characters) lets the UI identify a key
    without revealing it. POST /api/inference authenticates callers by
    hashing the presented bearer token and looking it up here.
    """

    __tablename__ = "inference_api_keys"
    __table_args__ = (
        sa.Index("ix_inference_api_keys_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # User-chosen display name (e.g. "dashboard-service").
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # SHA-256 hex digest of the raw token. The raw token is never stored.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )

    # Last few characters of the raw token, for display ("...abcd").
    token_hint: Mapped[str] = mapped_column(String(12), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class PasswordToken(Base):
    """A one-time link for setting a password (invite, sign-up or reset).

    Keyed by email rather than user id: invites and self-service sign-ups
    are issued before the account exists, and the account is created when
    the link is used. Only the SHA-256 of the raw token is stored; the raw
    token travels in the link's URL fragment.
    """

    __tablename__ = "password_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Lowercased email the link sets the password for.
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    # SHA-256 hex digest of the raw token.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )

    # "invite" (admin-issued, also used for resets an admin hands out) or
    # "reset" (self-service email link, incl. first-time sign-up).
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class Memory(Base):
    """A user-saved memory: a markdown text blob (up to 4KB).

    Memories are immutable -- to edit, delete and re-create.
    Full-text search is provided via an FTS5 virtual table kept
    in sync by database triggers (see the Alembic migration).
    """

    __tablename__ = "memories"
    __table_args__ = (
        sa.Index("ix_memories_user_id_archived", "user_id", "archived"),
    )

    # Primary key: UUID string (matches conversation ID pattern in chat/storage.py)
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Owner (integer FK to users.id)
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Memory content (markdown text, max 4KB enforced at application layer)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )
    # Soft-delete / archive flag
    archived: Mapped[bool] = mapped_column(
        default=False, server_default="0"
    )


class Guide(Base):
    """A named system prompt preset for a user.

    Each user has a default guide (is_default=True) that maps to the
    original custom_system_prompt.  Users can create additional named
    guides and select one per conversation.
    """

    __tablename__ = "guides"
    __table_args__ = (
        sa.Index("ix_guides_user_id_name", "user_id", "name", unique=True),
    )

    # Primary key: UUID string
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Owner (integer FK to users.id)
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Guide name (unique per user)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Guide content (system prompt text)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Whether this is the user's default guide
    is_default: Mapped[bool] = mapped_column(
        default=False, server_default="0"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class Project(Base):
    """A user project that groups related conversations with a shared workspace."""

    __tablename__ = "projects"
    __table_args__ = (
        sa.Index("ix_projects_user_id_name", "user_id", "name", unique=True),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Owner (integer FK to users.id)
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Project display name (max 100 chars enforced at application layer)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Optional project guide (custom instructions for all conversations in this project)
    guide: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Public mode: chosen at creation time and immutable afterwards.
    # Conversations in a public project run with internet-enabled sandboxing
    # and are cut off from internal resources (skills, memories, connectors,
    # action requests) so a public project can never become a
    # data-exfiltration path. Public projects cannot have project skills,
    # and have routines only behind the ``public_project_routines`` feature
    # gate.
    public: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    # Soft-hide flag (the project-level twin of ``conversations.archived``):
    # an archived project is dropped from the default project list and its
    # scheduled routines are passed over, but rows, workspace and
    # conversations are all kept. Unarchiving restores it exactly.
    archived: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class Routine(Base):
    """A canned prompt attached to a project that can be run in one click.

    Routines combine a prompt, an optional guide, and are scoped to a project.
    Running a routine creates a new conversation in the project with the
    prompt automatically sent as the first message.
    """

    __tablename__ = "routines"
    __table_args__ = (
        sa.Index("ix_routines_project_id", "project_id"),
        sa.Index("ix_routines_project_id_name", "project_id", "name", unique=True),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Project this routine belongs to (FK to projects.id with CASCADE)
    project_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Owner (integer FK to users.id -- denormalized for fast per-user queries)
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Routine display name (max 100 chars enforced at application layer)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # The prompt text to send as the first message
    prompt: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional guide to use for conversations started by this routine
    # NULL means use the user's default guide
    guide_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        sa.ForeignKey("guides.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Optional model to use for conversations started by this routine
    # NULL means use the user's current model selection (manual) or config default (scheduled)
    model: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        default=None,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class RoutineSchedule(Base):
    """A schedule attached to a routine for automatic execution.

    Each routine can have at most one schedule. The schedule_type determines
    which configuration columns are relevant:
    - 'daily': uses daily_time_utc, daily_time_local, timezone
    - 'weekly': uses weekly_days plus daily_time_utc, daily_time_local, timezone
    - 'hourly': uses hourly_minute
    - 'every_n_minutes': uses interval_minutes

    Anchored types (daily / weekly / hourly) keep their next occurrence in
    next_due_at; see db/schedule_timing.py.
    """

    __tablename__ = "routine_schedules"
    __table_args__ = (
        sa.Index("ix_routine_schedules_routine_id", "routine_id", unique=True),
        sa.Index("ix_routine_schedules_enabled_type", "is_enabled", "schedule_type"),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # One-to-one relationship: each routine has at most one schedule
    # Uniqueness enforced by ix_routine_schedules_routine_id unique index in __table_args__
    routine_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("routines.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Denormalized for fast per-user queries and scheduler queries
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Schedule type discriminator: 'daily', 'weekly', 'hourly', 'every_n_minutes'
    schedule_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )

    # --- Daily schedule fields ---
    # Time in UTC for efficient querying (derived from daily_time_local + timezone)
    daily_time_utc: Mapped[Optional[str]] = mapped_column(
        String(5), nullable=True
    )  # "HH:MM" in UTC

    # Time as entered by user in their local timezone
    daily_time_local: Mapped[Optional[str]] = mapped_column(
        String(5), nullable=True
    )  # "HH:MM" in local time

    # IANA timezone string (e.g., "America/New_York")
    timezone: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    # --- Weekly schedule fields ---
    # Comma-separated weekday numbers, Mon=0 .. Sun=6 (e.g. "0,2,4"). The
    # time of day reuses daily_time_local / timezone / daily_time_utc.
    weekly_days: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )

    # --- Hourly schedule fields ---
    # Minute offset from the top of each hour (0-59)
    hourly_minute: Mapped[Optional[int]] = mapped_column(
        sa.Integer, nullable=True
    )

    # --- Interval schedule fields ---
    # Run every N minutes
    interval_minutes: Mapped[Optional[int]] = mapped_column(
        sa.Integer, nullable=True
    )

    # --- Scheduling state ---
    # Whether this schedule is active (can be paused by user)
    is_enabled: Mapped[bool] = mapped_column(
        default=True, server_default="1"
    )

    # Last time the scheduler started a run for this schedule
    last_run_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )

    # Last time a run completed successfully
    last_run_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )

    # Next occurrence (UTC) of an anchored schedule. The scheduler fires once
    # the clock passes it and advances it on claim, so a late poll (e.g. after
    # a restart) still catches the run. NULL for every_n_minutes, and NULL
    # means "recompute from now" for anchored types.
    next_due_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )

    # Whether a run is currently in progress (for skip-if-running logic)
    is_running: Mapped[bool] = mapped_column(
        default=False, server_default="0"
    )

    # Last run's conversation ID (for linking to the most recent scheduled run)
    last_conversation_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, default=None
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class RoutineScheduleRun(Base):
    """One scheduled occurrence of a routine schedule: the run ledger.

    ``occurrence_at`` is the nominal scheduled instant (the claim time for
    every_n_minutes). The unique (schedule_id, occurrence_at) index makes a
    double fire of the same occurrence impossible. ``status`` is one of
    running / completed / failed / interrupted / missed. An interrupted run
    (server shutdown or crash mid-run) is retried within the schedule type's
    catch-up grace; ``attempt`` counts starts.
    """

    __tablename__ = "routine_schedule_runs"
    __table_args__ = (
        sa.Index(
            "ix_routine_schedule_runs_schedule_occurrence",
            "schedule_id", "occurrence_at", unique=True,
        ),
        sa.Index("ix_routine_schedule_runs_status", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    schedule_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("routine_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, default=None
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Conversation(Base):
    """Per-conversation metadata stored in SQLite.

    The actual conversation content (chat_history.json, sdk_history.json,
    workspace files) lives on disk at data/chats/{id}/.  This table stores
    only the metadata needed for ownership checks and sidebar ordering:

    - id          — matches the UUID directory name under data/chats/
    - user_id     — FK to users.id (determines ownership)
    - project_id  — optional FK to projects.id (NULL for standalone conversations)
    - created_at  — shown in UI; never requires a file read
    - last_message_at — used for sidebar sort; updated on every append
    """

    __tablename__ = "conversations"
    __table_args__ = (
        sa.Index("ix_conversations_user_id", "user_id"),
        sa.Index("ix_conversations_user_id_last_message", "user_id", "last_message_at"),
    )

    # UUID string — same as the conversation directory name
    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Owner (integer FK to users.id); cascade-deleted when the user is removed
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Optional project association
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Optional routine that created this conversation (NULL for non-routine conversations)
    routine_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        sa.ForeignKey("routines.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Soft-delete / archive flag
    archived: Mapped[bool] = mapped_column(
        default=False, server_default="0"
    )

    # LLM model used for this conversation (e.g. "claude-sonnet-4-6", "gemini-3.5-flash-lite")
    # NULL means no model has been set yet (new conversation before first message)
    model: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, default=None
    )

    # Optional custom display name (overrides auto-generated title from first message)
    custom_name: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, default=None
    )

    # Cached auto-derived title (slice of the first user message). Filled in
    # by ChatStorage.append_message / append_structured_messages on the first
    # user message and read by the sidebar list query so it doesn't have to
    # open chat_history.json per row. ``custom_name`` still wins when set.
    auto_title: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, default=None
    )

    # Discriminator for where the conversation was created. Known values:
    # "web" (default), "slack", "user_subagent" (a cross-user subagent run
    # created by another user's approved run_user_subagent action request; the
    # owner can view but not send), and "inference_api" (a one-shot headless
    # run driven by POST /api/inference; also read-only for the owner). NULL
    # is treated as "web" by application code.
    origin: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, default=None
    )

    # Cache of the highest per-conversation message ``seq`` written to
    # chat_history.json. Read by the persistent WS subscribe handler to
    # answer up_to_date / catchup / resync without scanning the JSON file
    # on every subscribe. Source of truth remains the JSON file; this column
    # is updated atomically alongside append_structured_messages.
    last_message_seq: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0", default=0,
    )

    # Per-conversation flags set at the start of the conversation (parsed from a
    # magic ``%%flags[...]`` first line). Stored as a JSON array of enabled flag
    # name strings, e.g. ``["nested_subagents"]``. NULL is treated as "no flags"
    # (empty set) by application code. See chat/conversation_flags.py.
    flags: Mapped[Optional[list]] = mapped_column(
        sa.JSON, nullable=True, default=None
    )


class SlackConversation(Base):
    """Maps a (slack_channel_id, slack_thread_ts) pair to a Quest conversation.

    Populated when a user sends a top-level DM to the bot; subsequent
    threaded replies to the same ts are routed into the existing
    conversation.
    """

    __tablename__ = "slack_conversations"
    __table_args__ = (
        sa.Index(
            "ix_slack_conversations_channel_thread",
            "slack_channel_id", "slack_thread_ts",
            unique=True,
        ),
        sa.Index("ix_slack_conversations_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    conversation_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    slack_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_thread_ts: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class LlmCallGemini(Base):
    """Records a single Gemini API call with the provider's NATIVE usage fields.

    One row per ``send_message_stream()`` call (one streaming turn) from
    either the top-level agent or a sub-agent. Multiple rows per conversation
    message are expected when the model makes tool calls (each tool-call turn
    is a separate API call) or spawns sub-agents.

    Raw provider-native numbers only -- no normalization, no coalescing. All
    interpretation (billing buckets, dashboard display, $ math) happens in
    read-side queries (db/llm_call_store.py). Native usage columns are
    nullable: NULL means "the SDK did not populate the field" (faithful to
    ``raw_usage``, which omits unset keys); read queries COALESCE to 0.

    Gemini field semantics (Google GenAI):
    - ``prompt_token_count`` INCLUDES cached tokens
      (``cached_content_token_count`` is the cache-hit subset of it).
    - ``candidates_token_count`` EXCLUDES ``thoughts_token_count``
      (reasoning tokens, billed at the output rate).
    - ``total_token_count = prompt + candidates + thoughts + tool_use_prompt``.
    - Context-tier pricing (Pro-tier 200k threshold) keys on
      ``prompt_token_count`` per row -- derive tiers per row at
      cost-computation time; never from aggregated sums.

    ``raw_usage`` (JSON) remains the lossless catch-all carrying the provider
    usage fields verbatim -- insurance for future provider fields that predate
    their own column.

    Rows are never cascade-deleted when the parent conversation or user is
    removed -- this table is an append-only analytics log.
    """

    __tablename__ = "llm_calls_gemini"
    __table_args__ = (
        sa.Index("ix_llm_calls_gemini_conversation_id", "conversation_id"),
        sa.Index("ix_llm_calls_gemini_user_id", "user_id"),
        sa.Index("ix_llm_calls_gemini_created_at", "created_at"),
    )

    # Auto-incrementing integer primary key
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The conversation this call belongs to (no FK cascade -- kept for analytics)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # The user who owns this conversation (no FK cascade -- kept for analytics)
    user_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # The model used for this call (e.g., ModelId.GEMINI_3_PRO)
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    # Whether this is a top-level agent call or a sub-agent call
    # Values: ApiCallType.TOP_LEVEL or ApiCallType.SUB_AGENT
    call_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Optional sub-agent name (NULL for top-level calls)
    agent_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # SDK transport backend: "genapi" / "vertex". Nullable for legacy rows /
    # unknown models. The same logical model can bill differently per backend.
    backend: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Sub-agent nesting depth: 1 = top-level agent or 1st-level sub-agent,
    # 2 = nested (2nd-level) sub-agent. Combined with call_type this attributes
    # cost across the three execution tiers.
    level: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1, server_default="1")

    # --- Native Gemini usage fields, verbatim (see class docstring) ---
    # Total prompt tokens for this call; INCLUDES cached_content_token_count.
    prompt_token_count: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Response tokens; EXCLUDES thoughts_token_count.
    candidates_token_count: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Cache-hit subset of prompt_token_count (billed at the reduced rate).
    cached_content_token_count: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Reasoning tokens, billed at the output rate.
    thoughts_token_count: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Tokens consumed by tool-use prompting.
    tool_use_prompt_token_count: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # prompt + candidates + thoughts + tool_use_prompt (provider-reported).
    total_token_count: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)

    # Lossless provider usage fields verbatim (catch-all; the native columns
    # above are its queryable projection). NULL when the provider returned no
    # usage object; also NULL-total-fields for backfilled pre-raw_usage rows.
    raw_usage: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)

    # Duration of this specific API call in milliseconds
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    # Timestamp of when this call was made
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class LlmCallAnthropic(Base):
    """Records a single Anthropic API call with the provider's NATIVE usage fields.

    One row per ``send_message_stream()`` call (one streaming turn) from
    either the top-level agent or a sub-agent -- same granularity and
    append-only semantics as LlmCallGemini (see its docstring); raw numbers
    only, all interpretation in read-side queries (db/llm_call_store.py).

    Anthropic field semantics:
    - ``input_tokens`` EXCLUDES cache reads and cache creation (uncached only).
    - ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` are the
      differently-priced cache buckets (read ~0.1x, creation 1.25x/2x).
    - ``cache_creation_5m_input_tokens`` / ``cache_creation_1h_input_tokens``
      split cache creation by TTL (the API's ``usage.cache_creation``
      ephemeral_5m/1h fields; 5m writes bill 1.25x, 1h writes 2x). NULL for
      backfilled history (never captured before) and when the SDK omits them.
    - No provider-reported total field exists.
    - Context-tier pricing (if a long-context premium returns) keys on
      ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``
      per row -- derive tiers per row at cost-computation time; never from
      aggregated sums.
    """

    __tablename__ = "llm_calls_anthropic"
    __table_args__ = (
        sa.Index("ix_llm_calls_anthropic_conversation_id", "conversation_id"),
        sa.Index("ix_llm_calls_anthropic_user_id", "user_id"),
        sa.Index("ix_llm_calls_anthropic_created_at", "created_at"),
    )

    # Auto-incrementing integer primary key
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The conversation this call belongs to (no FK cascade -- kept for analytics)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # The user who owns this conversation (no FK cascade -- kept for analytics)
    user_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # The Anthropic model used for this call (e.g., "claude-opus-4-8")
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    # Whether this is a top-level agent call or a sub-agent call
    # Values: ApiCallType.TOP_LEVEL or ApiCallType.SUB_AGENT
    call_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Optional sub-agent name (NULL for top-level calls)
    agent_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # SDK transport backend ("vertex" today). Nullable for legacy rows.
    backend: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Sub-agent nesting depth: 1 = top-level agent or 1st-level sub-agent,
    # 2 = nested (2nd-level) sub-agent. Combined with call_type this attributes
    # cost across the three execution tiers.
    level: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1, server_default="1")

    # --- Native Anthropic usage fields, verbatim (see class docstring) ---
    # Uncached input tokens only (EXCLUDES cache read/creation).
    input_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Input tokens served from cache (billed ~0.1x input rate).
    cache_read_input_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Input tokens written to cache (billed above the input rate).
    cache_creation_input_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # TTL split of cache_creation_input_tokens: 5m writes bill 1.25x, 1h 2x.
    cache_creation_5m_input_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    cache_creation_1h_input_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)

    # Lossless provider usage fields verbatim (catch-all; the native columns
    # above are its queryable projection). NULL when the provider returned no
    # usage object; also NULL-split-fields for backfilled pre-raw_usage rows.
    raw_usage: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)

    # Duration of this specific API call in milliseconds
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    # Timestamp of when this call was made
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class LlmCallOpenRouter(Base):
    """Records a single OpenRouter API call with the provider's NATIVE usage fields.

    One row per ``send_message_stream()`` call (one streaming turn) from
    either the top-level agent or a sub-agent -- same granularity and
    append-only semantics as LlmCallGemini (see its docstring); raw numbers
    only, all interpretation in read-side queries (db/llm_call_store.py).

    OpenRouter field semantics (OpenAI-compatible usage object, with the two
    nested detail objects flattened at capture time by the provider):
    - ``prompt_tokens`` INCLUDES cached tokens (``cached_prompt_tokens`` --
      the API's ``prompt_tokens_details.cached_tokens`` -- is the cache-hit
      subset of it), the Gemini-style convention.
    - ``completion_tokens`` INCLUDES ``reasoning_tokens`` (the API's
      ``completion_tokens_details.reasoning_tokens``).
    - ``total_tokens = prompt_tokens + completion_tokens``.
    - ``cost`` / ``upstream_inference_cost`` / ``is_byok`` are the accounting
      fields OpenRouter reported for the request (see the column comments);
      read queries prefer ``cost`` (plus ``upstream_inference_cost`` on
      BYOK rows) over the list-price estimate when present.
    - Context-tier pricing (should a tiered OpenRouter model ever be added)
      keys on ``prompt_tokens`` per row.
    """

    __tablename__ = "llm_calls_openrouter"
    __table_args__ = (
        sa.Index("ix_llm_calls_openrouter_conversation_id", "conversation_id"),
        sa.Index("ix_llm_calls_openrouter_user_id", "user_id"),
        sa.Index("ix_llm_calls_openrouter_created_at", "created_at"),
    )

    # Auto-incrementing integer primary key
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The conversation this call belongs to (no FK cascade -- kept for analytics)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # The user who owns this conversation (no FK cascade -- kept for analytics)
    user_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # The OpenRouter model used (e.g., "deepseek/deepseek-v4-flash-0731")
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    # Whether this is a top-level agent call or a sub-agent call
    # Values: ApiCallType.TOP_LEVEL or ApiCallType.SUB_AGENT
    call_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Optional sub-agent name (NULL for top-level calls)
    agent_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # SDK transport backend ("openrouter" today). Nullable to match siblings.
    backend: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Sub-agent nesting depth: 1 = top-level agent or 1st-level sub-agent,
    # 2 = nested (2nd-level) sub-agent. Combined with call_type this attributes
    # cost across the three execution tiers.
    level: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1, server_default="1")

    # --- Native OpenRouter usage fields (see class docstring) ---
    # Total prompt tokens; INCLUDES cached_prompt_tokens.
    prompt_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Response tokens; INCLUDES reasoning_tokens.
    completion_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Cache-hit subset of prompt_tokens (prompt_tokens_details.cached_tokens).
    cached_prompt_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # Reasoning subset of completion_tokens
    # (completion_tokens_details.reasoning_tokens).
    reasoning_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # prompt_tokens + completion_tokens (provider-reported).
    total_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    # USD OpenRouter reported charging the account for this request (the
    # usage object's ``cost``, present because the provider opts in with
    # ``usage: {"include": true}``). NULL for rows recorded before capture
    # existed or when the provider omitted it -- those stay list-price
    # estimated in the read queries. On a bring-your-own-key request this
    # is only OpenRouter's fee; the upstream provider's charge (billed to
    # the user's own key) is the next column.
    cost: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    # ``cost_details.upstream_inference_cost``: the upstream provider's
    # charge. Observed populated (equal to ``cost``) on non-BYOK requests
    # too, so read queries add it to ``cost`` ONLY when ``is_byok`` is true.
    upstream_inference_cost: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    # Whether the request ran on a bring-your-own-key upstream (the usage
    # object's ``is_byok``); NULL when not reported.
    is_byok: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)

    # Lossless provider usage fields verbatim (catch-all; the native columns
    # above are its queryable projection). NULL when the provider returned no
    # usage object.
    raw_usage: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)

    # Duration of this specific API call in milliseconds
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    # Timestamp of when this call was made
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ActionRequest(Base):
    """An action request proposed by the LLM agent.

    Action requests are actions the agent wants to perform on external
    systems (Slack, email, etc.) that require user approval. Each
    request has a type, JSON parameters, and a lifecycle status.
    """

    __tablename__ = "action_requests"
    __table_args__ = (
        sa.Index("ix_action_requests_user_id", "user_id"),
        sa.Index("ix_action_requests_conversation_id", "conversation_id"),
        sa.Index("ix_action_requests_user_id_status", "user_id", "status"),
        sa.Index(
            "ix_action_requests_user_status_created",
            "user_id", "status", "created_at",
        ),
    )

    # Auto-incrementing integer PK (matches the LlmCall* pattern)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Owner (integer FK to users.id with cascade)
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Conversation this request was made in (no FK -- same pattern as gemini_api_calls)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Request type discriminator (ActionRequestType enum value)
    request_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Type-specific parameters as JSON
    params: Mapped[dict] = mapped_column(JSON, nullable=False)

    # LLM-provided reasoning for the request
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)

    # Lifecycle status (ActionRequestStatus enum value)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ActionRequestStatus.OPEN, server_default="open"
    )

    # Execution result (NULL until resolved)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class SkillVisibility(StrEnum):
    """Visibility levels for skills in the skill library."""
    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"
    PROJECT = "project"


class Skill(Base):
    """A reusable skill definition in the skill library.

    Skills are instructions/prompts that can be shared across users.
    Unlike guides (which are per-user with CASCADE delete), skills use
    SET NULL on creator deletion so they persist for other users.
    """

    __tablename__ = "skills"
    __table_args__ = (
        sa.Index("ix_skills_creator_id_name", "creator_id", "name", unique=True),
        sa.Index("ix_skills_visibility", "visibility"),
        sa.Index("ix_skills_project_id", "project_id"),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Creator (integer FK to users.id with SET NULL -- skills persist when creator is deleted)
    creator_id: Mapped[Optional[int]] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Project (nullable -- NULL means user-level skill, non-NULL means project-level skill)
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        sa.ForeignKey("projects.id"),
        nullable=True,
        default=None,
    )

    # Skill display name (max 100 chars enforced at application layer)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Short description of the skill (max 500 chars enforced at application layer)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    # Full skill instructions (max 64KB enforced at application layer)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Visibility level: "private", "shared", "public"
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SkillVisibility.PRIVATE
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class SkillShare(Base):
    """Junction table for sharing skills with specific users.

    When a skill has visibility="shared", this table tracks which users
    have access. Cascade-deletes when the skill or user is removed.
    """

    __tablename__ = "skill_shares"
    __table_args__ = (
        sa.Index("ix_skill_shares_skill_id", "skill_id"),
        sa.Index("ix_skill_shares_user_id", "user_id"),
        sa.Index("ix_skill_shares_skill_id_user_id", "skill_id", "user_id", unique=True),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # The shared skill (FK to skills.id with CASCADE)
    skill_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The user the skill is shared with (FK to users.id with CASCADE)
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Doc(Base):
    """A Quest Doc: metadata row for a markdown document on disk.

    The body, embedded images and revision snapshots live under
    ``<data_dir>/docs/<id>/`` (resolved only via ``ChatStorage.get_doc_dir``;
    see chat/docs/files.py). Rows are read and written through
    db/doc_store.py, which also enforces case-insensitive title uniqueness
    per ``(owner_id, project_id, mode)`` -- a plain unique index cannot,
    because the NULL ``project_id`` (user doc) case never collides in
    SQLite; the per-mode scope keeps a public conversation's create_doc
    from revealing private titles through the collision error.
    """

    __tablename__ = "docs"
    __table_args__ = (
        sa.Index("ix_docs_owner_id", "owner_id"),
        sa.Index("ix_docs_project_id", "project_id"),
        sa.Index("ix_docs_updated_at", "updated_at"),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Owner (project docs are owned by the project owner)
    owner_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # NULL = user doc; non-NULL = project doc (deleted with the project)
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        default=None,
    )

    # 1..200 chars, 0..500 chars (enforced in db/doc_store.py)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    # "private" | "public" (chat/docs/constants.py DOC_MODES). Project docs
    # copy the project's immutable ``public`` flag at creation.
    mode: Mapped[str] = mapped_column(String(16), nullable=False)

    # Cached bytes of doc.md and file count of assets/, for list views.
    content_size: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    asset_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )

    # "conversation:<id>" | "ui" | "action_request:<id>"
    last_write_source: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True, default=None
    )

    # Timestamps; updated_at doubles as the optimistic-concurrency token.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class DocShare(Base):
    """A read or write grant on a doc to one user, or to everyone.

    ``user_id`` NULL means everyone on the install. SQLite treats NULLs as
    distinct in the unique ``(doc_id, user_id)`` index, so db/doc_store.py
    enforces "at most one everyone row per doc" itself. Cascade-deletes
    with the doc or the recipient user.
    """

    __tablename__ = "doc_shares"
    __table_args__ = (
        sa.Index("ix_doc_shares_doc_id_user_id", "doc_id", "user_id", unique=True),
        sa.Index("ix_doc_shares_user_id", "user_id"),
        # At most one "everyone" (user_id IS NULL) row per doc; SQLite treats
        # NULLs as distinct in the composite unique index above.
        sa.Index(
            "ix_doc_shares_everyone", "doc_id", unique=True,
            sqlite_where=sa.text("user_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    doc_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("docs.id", ondelete="CASCADE"),
        nullable=False,
    )

    # NULL = everyone on the install
    user_id: Mapped[Optional[int]] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    # "read" | "write" (chat/docs/constants.py DOC_SHARE_PERMISSIONS)
    permission: Mapped[str] = mapped_column(String(8), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class UserSkillAutoload(Base):
    """Junction table tracking which skills a user has auto-loaded.

    Auto-loaded skills are automatically included in every conversation.
    """

    __tablename__ = "user_skill_autoloads"
    __table_args__ = (
        sa.Index("ix_user_skill_autoloads_user_id", "user_id"),
        sa.Index("ix_user_skill_autoloads_skill_id", "skill_id"),
        sa.Index("ix_user_skill_autoloads_user_id_skill_id", "user_id", "skill_id", unique=True),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # The user who auto-loaded the skill (FK to users.id with CASCADE)
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The auto-loaded skill (FK to skills.id with CASCADE)
    skill_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ProjectSkillAutoload(Base):
    """Junction table tracking which skills a project has auto-loaded.

    Project auto-loaded skills are automatically included in every
    conversation within that project, in addition to user auto-loads.
    """

    __tablename__ = "project_skill_autoloads"
    __table_args__ = (
        sa.Index("ix_project_skill_autoloads_project_id", "project_id"),
        sa.Index("ix_project_skill_autoloads_skill_id", "skill_id"),
        sa.Index("ix_project_skill_autoloads_project_id_skill_id", "project_id", "skill_id", unique=True),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # The project (FK to projects.id with CASCADE)
    project_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The auto-loaded skill (FK to skills.id with CASCADE)
    skill_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class RoutineSkillAutoload(Base):
    """Junction table tracking which skills a routine has auto-loaded.

    Routine auto-loaded skills are automatically included in every
    conversation created by that routine (manual play or scheduled run),
    in addition to user and project auto-loads.
    """

    __tablename__ = "routine_skill_autoloads"
    __table_args__ = (
        sa.Index("ix_routine_skill_autoloads_routine_id", "routine_id"),
        sa.Index("ix_routine_skill_autoloads_skill_id", "skill_id"),
        sa.Index("ix_routine_skill_autoloads_routine_id_skill_id", "routine_id", "skill_id", unique=True),
    )

    # UUID string primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # The routine (FK to routines.id with CASCADE)
    routine_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("routines.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The auto-loaded skill (FK to skills.id with CASCADE)
    skill_id: Mapped[str] = mapped_column(
        String(36),
        sa.ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ToolWaitHandle(Base):
    """A durable handle for a tool that is awaiting human resolution.

    Tools that need user input (e.g. create_action_request's approve/deny
    or send_slack_reply_and_get_response's user-reply wait) insert a row
    here and either suspend directly or return its id to the model so it
    can call ``wait_for_handles``. The row is the source of truth --
    in-process futures are only fast-path wake-up signals, so the same
    conversation can resume across a server restart.
    """

    __tablename__ = "tool_wait_handles"
    __table_args__ = (
        sa.Index("ix_tool_wait_handles_user_id", "user_id"),
        sa.Index("ix_tool_wait_handles_conversation_id", "conversation_id"),
        sa.Index("ix_tool_wait_handles_user_id_status", "user_id", "status"),
        sa.Index("ix_tool_wait_handles_tool_id", "tool_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # No FK on conversation_id so analytics/audit rows survive deletion of
    # the conversation, mirroring action_requests.
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)

    kind: Mapped[str] = mapped_column(String(50), nullable=False)

    # The LLM tool_use id of the tool call that registered this handle. The
    # dispatch resume bucket walks dangling tool_uses by this id and
    # closes them with the row's response.
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ToolWaitHandleStatus.PENDING,
        server_default=ToolWaitHandleStatus.PENDING.value,
    )

    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    response: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)

    # Optional discriminator + id pointing at a related entity (e.g. the
    # memory created when the user accepted, or an action_request wrapped
    # by this handle). Both are nullable; populated on resolve.
    correlation_kind: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, default=None
    )
    correlation_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, default=None
    )

    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )


class UserSubagentRunStatus(StrEnum):
    """Lifecycle status of a cross-user subagent run."""
    # Launched; the subagent conversation loop is (or will be) running.
    RUNNING = "running"
    # The subagent proposed a return call; awaiting the target user's
    # approve / revise / deny on the subagent_return card.
    AWAITING_RETURN = "awaiting_return"
    # Target user approved the return; response + files delivered to the
    # caller conversation.
    RETURNED = "returned"
    # Target user hard-denied the return call; the run ended without
    # delivering a response.
    DENIED = "denied"
    # The run failed (exception, or the model ended without a return call
    # despite nudging).
    FAILED = "failed"


class UserSubagentRun(Base):
    """A cross-user subagent run created by a run_user_subagent action request.

    Links the caller's conversation (where the approved action request
    originated and where the caller-side ``user_subagent`` wait handle
    lives) to the subagent conversation created in the target user's
    account. The row is the durable source of truth for the run's
    lifecycle; chat/user_subagent.py drives the transitions.
    """

    __tablename__ = "user_subagent_runs"
    __table_args__ = (
        sa.Index(
            "ix_user_subagent_runs_subagent_conversation_id",
            "subagent_conversation_id",
        ),
        # Cost-attribution rollups query "all runs launched from
        # conversation C" (join llm_calls_* on subagent_conversation_id),
        # so the caller conversation is indexed too.
        sa.Index(
            "ix_user_subagent_runs_caller_conversation_id",
            "caller_conversation_id",
        ),
        sa.Index("ix_user_subagent_runs_caller_user_id", "caller_user_id"),
        sa.Index("ix_user_subagent_runs_target_user_id", "target_user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Plain integers with no FK, mirroring the llm_calls_* analytics
    # tables: run rows are the cost-attribution linkage between a caller
    # conversation and its subagent conversation's llm_calls_* rows, and
    # must survive deletion of either user just like the call rows do.
    caller_user_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    target_user_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # No FK on conversation ids so run rows survive conversation deletion,
    # mirroring action_requests / tool_wait_handles.
    caller_conversation_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    subagent_conversation_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )

    # The caller-side wait handle (kind "user_subagent") resolved when the
    # target user approves or denies the subagent's return call.
    wait_handle_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # The exact prompt the caller approved (also seeded as the subagent
    # conversation's first user message).
    prompt: Mapped[str] = mapped_column(Text, nullable=False)

    # Skill ids autoloaded into the subagent's system prompt (validated
    # against the target user's visibility at proposal and execute time).
    skill_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=UserSubagentRunStatus.RUNNING,
        server_default=UserSubagentRunStatus.RUNNING.value,
    )

    # Human-readable failure detail when status is "failed".
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )
