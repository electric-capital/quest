"""add thinking_effort to conversations

Revision ID: a9d3e7f1c5b2
Revises: f4c8e1a7d2b9
Create Date: 2026-10-15 12:00:00.000000

Adds ``conversations.thinking_effort``: the thinking level the user picked
for the conversation's model in the composer's model menu (one of the
model spec's ``thinking_efforts``; "none" = thinking off). NULL -- every
existing row -- means the model's default level, which is what those
conversations ran at before the column existed, so no backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9d3e7f1c5b2'
down_revision: Union[str, Sequence[str], None] = 'f4c8e1a7d2b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add thinking_effort to conversations."""
    op.add_column(
        'conversations',
        sa.Column('thinking_effort', sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    """Remove thinking_effort from conversations."""
    op.drop_column('conversations', 'thinking_effort')
