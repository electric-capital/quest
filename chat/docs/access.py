"""The Quest Docs access rule -- the ONE place the read/write matrix lives.

Invariant 3 of the Quest Docs spec: every read/write decision about a doc
goes through :func:`resolve_doc_access`. The model-facing tools, the HTTP
routes, the ``write_doc`` action request (pre-card AND approve time),
``list_docs`` / ``search_docs`` and the system prompt consume the returned
:class:`DocAccess`; none of them re-derive any part of the rule below.

The matrix (rows = the doc, columns = who asks from which conversation;
"recipient" = a share recipient; the last column covers sub_agent,
inference_api, user_subagent and script runs)::

    | Doc                   | Private  | Private      | Public | Public      | Read-only |
    |                       | owner    | recipient    | owner  | recipient   | run kinds |
    |-----------------------|----------|--------------|--------|-------------|-----------|
    | Private, unshared     | Free     | n/a (Hidden) | Hidden | n/a(Hidden) | Read      |
    | Private, shared (any) | Approval | read: Read   | Hidden | Hidden      | Read      |
    |                       |          | write: Appr. |        |             |           |
    | Public, unshared      | Read     | n/a (Hidden) | Free   | n/a(Hidden) | Read (*)  |
    | Public, shared        | Read     | Read         | Free   | read: Read  | Read (*)  |
    |                       |          |              |        | write: Free |           |

(*) except ``script``: scripts never see project docs (rule 2), and public
docs are always project docs, so a script sees every public doc as Hidden.

A user doc (``project_id`` None) is always private: the "Public" rows only
ever describe docs of a public project, and every "Public owner / Public
recipient" cell of a user doc is Hidden. The "Private owner / Private
recipient" cells of the Public rows are reached from the conversations of
a private project that lists the doc's project as a doc source (rule 2,
``doc_source_project_ids``): there a public doc is readable, never
writable. A stored ``mode="public"`` on a user doc (a leftover of the
dropped user-doc mode switch; migration ``e1b7c4d9a2f6`` flips them) is
evaluated as ``private`` for every decision below.

(Read-only run kinds read only docs they can see: rules 1-3 below still
apply to them.)

- **Free**: the write tool succeeds without a card.
- **Approval**: the tool refuses with ``approval_required`` and the change
  goes through a ``write_doc`` action request.
- **Read**: readable; writes refuse with ``deny_reason``.
- **Hidden**: behaves exactly like a nonexistent id (:data:`HIDDEN`, no
  ``deny_reason``, reported with ``doc_not_found_message``).

Rules layered on the matrix, applied in this order:

1. No relationship (not the owner, no matching share row) -> Hidden. A
   share row with ``user_id=None`` grants everyone on the install.
2. Project docs are visible only from conversations of that same project
   (and from the UI); standalone conversations, other projects'
   conversations and sandbox scripts (no conversation context) see Hidden.
   Exception: a PUBLIC doc (a doc of a public project) is visible,
   read-only, from the conversations of a private project whose owner
   listed the doc's project among the project's **doc sources**
   (``doc_source_project_ids``, Project Settings > Docs access; stored in
   ``project_doc_sources``), so what a chosen public project gathers can
   be read there. Standalone conversations have no doc sources, public
   conversations stay confined to their own project's docs, and scripts
   still see no project doc at all.
3. A public conversation never learns of a private doc (Hidden, even for
   the owner and even with a write share).
4. Read-only run kinds (sub-agents, inference-API runs, cross-user subagent
   runs, sandbox scripts) can read whatever they can see and never write.
5. ``ui`` (the HTTP routes): Free for the owner or a write share, else
   Read. Ownership-only operations (rename, delete) are a separate route
   check, not part of this matrix; no doc's mode can be switched.
6. Slack-driven runs cannot open action requests, so an Approval verdict
   becomes a denial there.
7. The owner's **require approval** switch (``docs.require_approval``,
   the "Require approval for agent writes" checkbox in the doc header
   menu): every Free verdict a *conversation* would get becomes Approval
   (``required_by_owner=True`` on the verdict, so the notes say why) --
   the owner's unshared private doc included. Where no approval card can
   be opened the write is denied instead: Slack-driven runs
   (``DENY_SLACK_APPROVAL_REQUIRED``) and public-project conversations,
   which have no action requests at all (``DENY_PUBLIC_APPROVAL_REQUIRED``
   -- so on a public doc the switch makes the doc read-only for the
   agent). Read verdicts, Hidden cells, the read-only run kinds and the
   ``ui`` column (a person editing is never approval-gated, invariant 5)
   are unchanged.

Taint (invariant 1): a private conversation never gets a non-denied write
verdict on a public doc, and a conversation only creates docs in its own
mode -- :func:`creation_mode` for project docs, while user docs (always
private) cannot be created from a public conversation at all.

Callers pass ``is_public=False`` and ``project_id=None`` for the ``ui`` and
``script`` run kinds (neither runs inside a conversation), and the empty
default ``doc_source_project_ids`` for everything but a private project
conversation (the doc service loads the project's sources).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from chat.docs.constants import DOC_MODES

__all__ = [
    "APPROVAL_REQUIRED_WRITE_NOTE",
    "APPROVAL_WRITE_NOTE",
    "DENY_INFERENCE_API",
    "DENY_PUBLIC_APPROVAL_REQUIRED",
    "DENY_PUBLIC_DOC_FROM_PRIVATE",
    "DENY_READ_ONLY_SHARE",
    "DENY_SCRIPT",
    "DENY_SLACK_APPROVAL_REQUIRED",
    "DENY_SLACK_NEEDS_APPROVAL",
    "DENY_SUB_AGENT",
    "DENY_UI_READ_ONLY",
    "DENY_USER_SUBAGENT",
    "HIDDEN",
    "READ_ONLY_RUN_KINDS",
    "RUN_KINDS",
    "WRITE_VERDICTS",
    "DocAccess",
    "WriteVerdict",
    "creation_mode",
    "effective_share",
    "resolve_doc_access",
    "write_note",
]

RUN_KINDS = (
    "top_level",
    "sub_agent",
    "inference_api",
    "user_subagent",
    "script",
    "slack",
    "ui",
)
READ_ONLY_RUN_KINDS = frozenset({"sub_agent", "inference_api", "user_subagent", "script"})

WriteVerdict = Literal["free", "approval", "denied"]
WRITE_VERDICTS = ("free", "approval", "denied")

# Model/user-facing deny reasons. Tools, routes and docs reference these
# constants; the text never names or hints at a hidden doc.
DENY_SUB_AGENT = "Only the top-level agent writes docs; return the content to it."
DENY_INFERENCE_API = "Inference API runs are read-only; docs cannot be written here."
DENY_USER_SUBAGENT = "Cross-user subagent runs are read-only; return the content to the caller."
DENY_SCRIPT = (
    "Sandbox scripts have read-only access to docs; the agent writes docs with the doc tools."
)
DENY_READ_ONLY_SHARE = "You have read-only access to this shared doc."
DENY_PUBLIC_DOC_FROM_PRIVATE = "Public docs are written only from public-project conversations."
DENY_SLACK_NEEDS_APPROVAL = (
    "This doc is shared, so changes need an approval card, which Slack-driven "
    "conversations cannot open. Continue from the Quest web UI."
)
DENY_SLACK_APPROVAL_REQUIRED = (
    "The owner requires approval for every change to this doc, and "
    "Slack-driven conversations cannot open approval cards. Continue from "
    "the Quest web UI."
)
DENY_PUBLIC_APPROVAL_REQUIRED = (
    "The owner requires approval for every change to this doc, and "
    "public-project conversations cannot open approval cards."
)
DENY_UI_READ_ONLY = "You have read-only access to this doc."

APPROVAL_WRITE_NOTE = "shared private doc: use create_action_request(write_doc)"
APPROVAL_REQUIRED_WRITE_NOTE = (
    "owner requires approval for every change: use create_action_request(write_doc)"
)

_READ_ONLY_REASONS = {
    "sub_agent": DENY_SUB_AGENT,
    "inference_api": DENY_INFERENCE_API,
    "user_subagent": DENY_USER_SUBAGENT,
    "script": DENY_SCRIPT,
}

# Share permissions (constants.DOC_SHARE_PERMISSIONS), weakest to strongest.
_PERMISSION_RANK = {"read": 1, "write": 2}


@dataclass(frozen=True)
class DocAccess:
    """What one caller may do with one doc.

    ``visible=False`` means "behave as if the doc does not exist"; such a
    verdict grants nothing and carries no ``deny_reason``. ``deny_reason``
    is set exactly when a visible doc's write verdict is ``"denied"``.
    ``required_by_owner`` marks an ``"approval"`` verdict that the owner's
    require-approval switch produced (rule 7) -- the only difference is the
    wording of the notes the model sees (:func:`write_note`); it is never
    set on any other verdict.
    """

    visible: bool
    can_read: bool
    write: WriteVerdict
    deny_reason: str | None
    required_by_owner: bool = False

    def __post_init__(self) -> None:
        if self.write not in WRITE_VERDICTS:
            raise ValueError(f"unknown write verdict: {self.write!r}")
        if not self.visible and (
            self.can_read or self.write != "denied" or self.deny_reason is not None
        ):
            raise ValueError("a hidden doc grants nothing and carries no deny_reason")
        if self.visible and (self.deny_reason is not None) != (self.write == "denied"):
            raise ValueError("deny_reason is set exactly when a visible doc's write is denied")
        if self.required_by_owner and self.write != "approval":
            raise ValueError("required_by_owner is set only on an approval verdict")


HIDDEN = DocAccess(visible=False, can_read=False, write="denied", deny_reason=None)
_FREE = DocAccess(visible=True, can_read=True, write="free", deny_reason=None)
_APPROVAL = DocAccess(visible=True, can_read=True, write="approval", deny_reason=None)
_APPROVAL_REQUIRED = DocAccess(
    visible=True, can_read=True, write="approval", deny_reason=None, required_by_owner=True,
)


def _read_only(reason: str) -> DocAccess:
    return DocAccess(visible=True, can_read=True, write="denied", deny_reason=reason)


def _shares(doc: Mapping[str, Any]) -> list:
    """The doc's share rows; a missing/None ``shares`` key is a caller bug.

    Fail closed: a dict fetched without shares (``get_doc(...,
    with_shares=False)``) must never be read as "unshared", or the owner of
    a shared private doc would get a free write instead of the approval
    card. Callers always pass the full store dict.
    """
    shares = doc.get("shares")
    if shares is None:
        raise ValueError("doc dict must carry its 'shares' list (fetch with shares)")
    return shares


def effective_share(doc: Mapping[str, Any], user_id: int) -> str | None:
    """The permission ``user_id`` holds through ``doc["shares"]``.

    Considers the user's own row and the everyone row (``user_id=None``);
    when both match, the higher permission wins (``write`` > ``read``), so a
    user-specific row never downgrades an everyone grant. Rows with an
    unknown permission grant nothing. Returns ``"write"``, ``"read"`` or
    None. Ownership is not a share; callers check ``owner_id`` separately.
    """
    best: str | None = None
    for row in _shares(doc):
        grantee = row.get("user_id")
        if grantee is not None and grantee != user_id:
            continue
        permission = row.get("permission")
        rank = _PERMISSION_RANK.get(permission)
        if rank is None:
            continue
        if best is None or rank > _PERMISSION_RANK[best]:
            best = permission
    return best


def resolve_doc_access(
    doc: Mapping[str, Any],
    *,
    user_id: int,
    is_public: bool,
    project_id: str | None,
    run_kind: str,
    doc_source_project_ids: Collection[str] = (),
) -> DocAccess:
    """Decide visibility, readability and the write verdict for one doc.

    ``doc`` is the doc_store dict including ``shares`` (a missing or None
    ``shares`` raises -- never fail open on a partial dict). ``is_public`` /
    ``project_id`` describe the calling conversation; ``run_kind`` is one
    of :data:`RUN_KINDS`; ``doc_source_project_ids`` are the public
    projects whose docs the calling (private) project may read (rule 2),
    empty unless the caller is a private project conversation.

    Raises ValueError for an unknown ``run_kind`` or doc mode, or a doc
    dict without its ``shares`` list.
    """
    if run_kind not in RUN_KINDS:
        raise ValueError(f"unknown run_kind: {run_kind!r}")
    stored_mode = doc["mode"]
    if stored_mode not in DOC_MODES:
        raise ValueError(f"unknown doc mode: {stored_mode!r}")
    _shares(doc)  # fail closed on a partial dict before any verdict

    # 0. A user doc is always private, whatever its row says (defensive:
    # after migration e1b7c4d9a2f6 no public user doc exists).
    doc_project_id = doc["project_id"]
    mode = "private" if doc_project_id is None else stored_mode

    # 1. Relationship.
    is_owner = doc["owner_id"] == user_id
    share = None if is_owner else effective_share(doc, user_id)
    is_shared = len(_shares(doc)) > 0
    # Rule 7. Absent on a hand-built dict = off, like a fresh row.
    require_approval = bool(doc.get("require_approval", False))

    # 2. No relationship at all.
    if not is_owner and share is None:
        return HIDDEN

    # 3. Project docs: only from that project's conversations (or the UI),
    # except public docs of a listed doc source, which a private project's
    # conversations may read (rule 2). Public conversations stay confined
    # to their own project's docs; scripts (no conversation) see no
    # project doc; standalone conversations have no sources.
    if doc_project_id is not None and run_kind != "ui" and project_id != doc_project_id:
        if (
            mode != "public"
            or is_public
            or run_kind == "script"
            or project_id is None
            or doc_project_id not in doc_source_project_ids
        ):
            return HIDDEN

    # 4. Public conversations never learn of private docs.
    if is_public and mode == "private":
        return HIDDEN

    # 5. Visible and readable; decide the write verdict.
    if run_kind in READ_ONLY_RUN_KINDS:
        return _read_only(_READ_ONLY_REASONS[run_kind])

    may_write = is_owner or share == "write"

    if run_kind == "ui":
        return _FREE if may_write else _read_only(DENY_UI_READ_ONLY)

    if mode == "public":
        if not is_public:
            return _read_only(DENY_PUBLIC_DOC_FROM_PRIVATE)
        if not may_write:
            return _read_only(DENY_READ_ONLY_SHARE)
        if require_approval:
            # Rule 7: public conversations have no action requests, so the
            # owner's switch leaves the agent read-only here.
            return _read_only(DENY_PUBLIC_APPROVAL_REQUIRED)
        return _FREE

    # Private doc in a private conversation (step 4 hid it from public ones).
    if not may_write:
        return _read_only(DENY_READ_ONLY_SHARE)
    if require_approval:
        # Rule 7, before the matrix: the switch wins over "unshared = free"
        # and over the share-based approval (its wording names the switch).
        if run_kind == "slack":
            return _read_only(DENY_SLACK_APPROVAL_REQUIRED)
        return _APPROVAL_REQUIRED
    if is_owner and not is_shared:
        return _FREE
    if run_kind == "slack":
        return _read_only(DENY_SLACK_NEEDS_APPROVAL)
    return _APPROVAL


def write_note(access: DocAccess) -> str | None:
    """Short ``write_note`` for list/read results: None when writes are free."""
    if access.write == "free":
        return None
    if access.write == "approval":
        return APPROVAL_REQUIRED_WRITE_NOTE if access.required_by_owner else APPROVAL_WRITE_NOTE
    return access.deny_reason


def creation_mode(is_public: bool) -> str:
    """The mode of a doc created from a conversation: always its own mode.

    The creation half of the taint invariant: a private conversation can
    only create private docs and a public one only public docs. Only
    project docs can be public, so a public conversation creates docs of
    its (public) project only; user docs are always private and the
    service refuses to create one from a public conversation.
    """
    return "public" if is_public else "private"
