"""mark routine and archived conversations read

Revision ID: e7c2a9f4b1d6
Revises: d4b8f1a6c3e7
Create Date: 2026-11-06 12:00:00.000000

Unread state no longer applies to two kinds of conversation that could
already have taken it:

- A routine's conversation stays read through the routine's own run and
  becomes unread only after the user sends a message of their own. Its
  ``unread_seq`` being non-zero is the record that they have
  (``db/conversation_store.py``), so both seqs go back to zero rather than
  ``read_seq`` being raised.
- Archiving a conversation marks it read.

Pure data migration, idempotent.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e7c2a9f4b1d6'
down_revision: Union[str, Sequence[str], None] = 'd4b8f1a6c3e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Reset the unread seqs of routine conversations and mark archived
    conversations read."""
    op.execute(
        "UPDATE conversations SET unread_seq = 0, read_seq = 0 "
        "WHERE routine_id IS NOT NULL AND (unread_seq != 0 OR read_seq != 0)"
    )
    op.execute(
        "UPDATE conversations SET read_seq = unread_seq "
        "WHERE archived = 1 AND unread_seq > read_seq"
    )


def downgrade() -> None:
    """Irreversible: the previous seqs are not kept. Nothing to undo in the
    schema."""
