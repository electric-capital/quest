"""user docs are always private

Revision ID: e1b7c4d9a2f6
Revises: 55983a10e266
Create Date: 2026-10-06 12:00:00.000000

Quest Docs dropped the user-doc mode switch: a user doc (``project_id``
NULL) is always ``private``, and the only public docs are the docs of a
public project (which copy the project's immutable ``public`` flag). This
flips every leftover public user doc to private. The access rule
(chat/docs/access.py) already evaluates such a row as private; this makes
the stored mode agree.

Titles are unique case-insensitively per ``(owner_id, project_id, mode)``
(enforced in db/doc_store.py, not by an index), so a user could own a
private and a public user doc with the same title. A flipped row whose
title would collide with one of the owner's private user docs is renamed
with a ``(formerly public)`` suffix (numbered on a further collision) so the
invariant still holds afterwards. ``updated_at`` is left alone: this is a
data correction, not an edit.

Downgrade is a no-op: the previous mode is not recoverable.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1b7c4d9a2f6'
down_revision: Union[str, Sequence[str], None] = '55983a10e266'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# chat/docs/constants.py DOC_TITLE_MAX_LEN; a migration keeps its own copy
# so a later change to the constant cannot alter what it did.
_TITLE_MAX_LEN = 200
_SUFFIX = " (formerly public)"


def _fold(title: str) -> str:
    # Same comparison as doc_store._title_taken (Python casefold, not
    # SQLite's ASCII-only lower()).
    return title.strip().casefold()


def _free_title(title: str, taken: set[str]) -> str:
    """``title`` if free among ``taken`` (folded), else a suffixed variant."""
    if _fold(title) not in taken:
        return title
    n = 1
    while True:
        suffix = _SUFFIX if n == 1 else f" (formerly public {n})"
        candidate = title.strip()[: _TITLE_MAX_LEN - len(suffix)].rstrip() + suffix
        if _fold(candidate) not in taken:
            return candidate
        n += 1


def upgrade() -> None:
    """Set ``mode = 'private'`` on every user doc, renaming on a title clash."""
    bind = op.get_bind()
    flipped = bind.execute(sa.text(
        "SELECT id, owner_id, title FROM docs "
        "WHERE project_id IS NULL AND mode != 'private' "
        "ORDER BY created_at, id"
    )).fetchall()
    taken: dict[int, set[str]] = {}
    for doc_id, owner_id, title in flipped:
        if owner_id not in taken:
            taken[owner_id] = {
                _fold(t) for (t,) in bind.execute(
                    sa.text(
                        "SELECT title FROM docs WHERE owner_id = :owner "
                        "AND project_id IS NULL AND mode = 'private'"
                    ),
                    {"owner": owner_id},
                )
            }
        new_title = _free_title(title, taken[owner_id])
        taken[owner_id].add(_fold(new_title))
        bind.execute(
            sa.text("UPDATE docs SET mode = 'private', title = :title WHERE id = :id"),
            {"title": new_title, "id": doc_id},
        )


def downgrade() -> None:
    """No-op: the flipped docs' previous mode is not recoverable."""
