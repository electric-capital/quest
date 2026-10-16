"""The shared access procedure behind Quest Docs and Quest Tables.

Docs (``chat.docs.access``) and Tables (``chat.tables.access``) use the
same visibility and write matrix: owner / share recipient, private /
public mode, project scoping with doc sources and the owner's read-only
reach from standalone conversations, public-conversation invisibility,
read-only run kinds, the ``ui`` column, Slack runs and the owner's
require-approval switch. This module is that procedure, written
once. It returns a :class:`CoreVerdict` with a machine-readable ``cause``
instead of text; each resolver turns it into its own verdict type and
wording (docs say "doc", tables say "table"), and the tables resolver adds
its schema verdict on top.

Nothing outside those two resolvers may call :func:`resolve_core`: tools,
routes, handlers and prompts consume ``resolve_doc_access`` /
``resolve_table_access`` (invariant 3 of both specs). The rule itself,
cell by cell, is documented in ``chat/docs/access.py``; the tables
differences are in ``chat/tables/access.py``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

__all__ = [
    "DENY_CAUSES",
    "MODES",
    "READ_ONLY_RUN_KINDS",
    "RUN_KINDS",
    "WRITE_VERDICTS",
    "CoreVerdict",
    "DenyCause",
    "WriteVerdict",
    "effective_share",
    "resolve_core",
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

# Both artifacts store the same two modes (DOC_MODES / TABLE_MODES).
MODES = ("private", "public")

WriteVerdict = Literal["free", "approval", "denied"]
WRITE_VERDICTS = ("free", "approval", "denied")

# Why a visible artifact's write verdict is "denied". The four read-only
# run kinds are their own causes; each resolver maps every cause to text.
DenyCause = Literal[
    "sub_agent",
    "inference_api",
    "user_subagent",
    "script",
    "read_only_share",       # read share (or read-only reach) in a conversation
    "public_from_private",   # public artifact seen from a private conversation
    "outside_project",       # owner's project artifact, from a standalone conversation
    "slack_needs_approval",  # shared private artifact, Slack cannot open a card
    "slack_approval_required",   # owner switch on, Slack cannot open a card
    "public_approval_required",  # owner switch on, public convos have no cards
    "ui_read_only",          # UI, no write relationship
]
DENY_CAUSES: tuple[str, ...] = get_args(DenyCause)

# Share permissions, weakest to strongest.
_PERMISSION_RANK = {"read": 1, "write": 2}


@dataclass(frozen=True)
class CoreVerdict:
    """The procedure's answer, before any wording.

    ``cause`` is set exactly when a visible verdict's ``write`` is
    ``"denied"``; ``required_by_owner`` only on an ``"approval"`` verdict
    the owner's switch produced. A hidden verdict carries neither.
    ``is_owner`` is reported for resolvers that need it (the tables schema
    verdict in the UI).
    """

    visible: bool
    write: WriteVerdict
    cause: DenyCause | None = None
    required_by_owner: bool = False
    is_owner: bool = False


_HIDDEN = CoreVerdict(visible=False, write="denied")


def _shares(item: Mapping[str, Any], kind: str) -> list:
    """The share rows; a missing/None ``shares`` key is a caller bug.

    Fail closed: a dict fetched without shares must never be read as
    "unshared", or the owner of a shared private artifact would get a free
    write instead of the approval card.
    """
    shares = item.get("shares")
    if shares is None:
        raise ValueError(f"{kind} dict must carry its 'shares' list (fetch with shares)")
    return shares


def effective_share(item: Mapping[str, Any], user_id: int, *, kind: str = "doc") -> str | None:
    """The permission ``user_id`` holds through ``item["shares"]``.

    Considers the user's own row and the everyone row (``user_id=None``);
    when both match, the higher permission wins (``write`` > ``read``).
    Rows with an unknown permission grant nothing. Returns ``"write"``,
    ``"read"`` or None. Ownership is not a share.
    """
    best: str | None = None
    for row in _shares(item, kind):
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


def _read_only(cause: DenyCause, is_owner: bool) -> CoreVerdict:
    return CoreVerdict(visible=True, write="denied", cause=cause, is_owner=is_owner)


def resolve_core(
    item: Mapping[str, Any],
    *,
    user_id: int,
    is_public: bool,
    project_id: str | None,
    run_kind: str,
    source_project_ids: Collection[str] = (),
    kind: str = "doc",
) -> CoreVerdict:
    """Run the shared matrix for one doc or table dict.

    ``item`` carries ``owner_id``, ``project_id``, ``mode``, ``shares`` and
    optionally ``require_approval``. ``kind`` only names the artifact in
    error messages. Raises ValueError for an unknown ``run_kind`` or mode,
    or a dict without its ``shares`` list.
    """
    if run_kind not in RUN_KINDS:
        raise ValueError(f"unknown run_kind: {run_kind!r}")
    stored_mode = item["mode"]
    if stored_mode not in MODES:
        raise ValueError(f"unknown {kind} mode: {stored_mode!r}")
    shares = _shares(item, kind)  # fail closed before any verdict

    # A user (non-project) artifact is always private, whatever its row says.
    item_project_id = item["project_id"]
    mode = "private" if item_project_id is None else stored_mode

    is_owner = item["owner_id"] == user_id
    share = None if is_owner else effective_share(item, user_id, kind=kind)
    is_shared = len(shares) > 0
    require_approval = bool(item.get("require_approval", False))

    # No relationship at all.
    if not is_owner and share is None:
        return _HIDDEN

    # Project artifacts: from that project's conversations (or the UI) they
    # follow the matrix. From elsewhere they are reachable read-only in two
    # cases, else hidden: the owner's own project artifacts (any project,
    # any mode) from the owner's standalone conversations, and the public
    # artifacts of a listed source from a private project's conversations.
    # Public conversations stay confined to their own project; scripts (no
    # conversation) see no project artifact.
    if item_project_id is not None and run_kind != "ui" and project_id != item_project_id:
        if is_public or run_kind == "script":
            return _HIDDEN
        if project_id is None:
            if not is_owner:
                return _HIDDEN
        elif mode != "public" or item_project_id not in source_project_ids:
            return _HIDDEN

    # Public conversations never learn of private artifacts.
    if is_public and mode == "private":
        return _HIDDEN

    # Visible and readable; decide the write verdict.
    if run_kind in READ_ONLY_RUN_KINDS:
        return _read_only(run_kind, is_owner)  # type: ignore[arg-type]

    # A private project artifact reached from outside its project (the
    # owner's standalone conversation) is read-only; a public one reached
    # that way falls under the public-from-private rule below.
    if (
        item_project_id is not None
        and run_kind != "ui"
        and project_id != item_project_id
        and mode != "public"
    ):
        return _read_only("outside_project", is_owner)

    may_write = is_owner or share == "write"

    if run_kind == "ui":
        if may_write:
            return CoreVerdict(visible=True, write="free", is_owner=is_owner)
        return _read_only("ui_read_only", is_owner)

    if mode == "public":
        if not is_public:
            return _read_only("public_from_private", is_owner)
        if not may_write:
            return _read_only("read_only_share", is_owner)
        if require_approval:
            return _read_only("public_approval_required", is_owner)
        return CoreVerdict(visible=True, write="free", is_owner=is_owner)

    # Private artifact in a private conversation.
    if not may_write:
        return _read_only("read_only_share", is_owner)
    if require_approval:
        if run_kind == "slack":
            return _read_only("slack_approval_required", is_owner)
        return CoreVerdict(
            visible=True, write="approval", required_by_owner=True, is_owner=is_owner,
        )
    if is_owner and not is_shared:
        return CoreVerdict(visible=True, write="free", is_owner=is_owner)
    if run_kind == "slack":
        return _read_only("slack_needs_approval", is_owner)
    return CoreVerdict(visible=True, write="approval", is_owner=is_owner)
