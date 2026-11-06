"""add unread state to conversations

Revision ID: d4b8f1a6c3e7
Revises: c5a9e3f7d2b1
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4b8f1a6c3e7'
down_revision: Union[str, Sequence[str], None] = 'c5a9e3f7d2b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add unread_seq and read_seq to conversations.

    A conversation is unread while ``unread_seq > read_seq``. Both default
    to 0, so every existing conversation starts out read.
    """
    for column in ('unread_seq', 'read_seq'):
        op.add_column(
            'conversations',
            sa.Column(column, sa.Integer(), nullable=False, server_default='0'),
        )


def downgrade() -> None:
    """Remove unread_seq and read_seq from conversations."""
    op.drop_column('conversations', 'read_seq')
    op.drop_column('conversations', 'unread_seq')
