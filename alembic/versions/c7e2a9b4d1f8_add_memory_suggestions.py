"""add memory_suggestions table

Revision ID: c7e2a9b4d1f8
Revises: e3c9b7a1d5f2
Create Date: 2026-10-14 12:00:00.000000

Model-proposed memories registered on the side by the non-blocking
``suggest_memory`` tool (chat/memory_suggestions.py). One row per
suggestion; ``status`` moves from ``pending`` to ``accepted`` (then
``memory_id`` points at the created ``memories`` row and an optional
``replaces_memory_id`` memory is archived) or ``dismissed``. Rows cascade
with their user and conversation; the two memory references are nulled
when the memory is deleted so a suggestion never dangles.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e2a9b4d1f8'
down_revision: Union[str, Sequence[str], None] = 'e3c9b7a1d5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the memory_suggestions table and its indexes."""
    op.create_table(
        'memory_suggestions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('replaces_memory_id', sa.String(length=36), nullable=True),
        sa.Column(
            'status', sa.String(length=20), nullable=False,
            server_default='pending',
        ),
        sa.Column('memory_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['replaces_memory_id'], ['memories.id'], ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(['memory_id'], ['memories.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_memory_suggestions_user_id', 'memory_suggestions', ['user_id'],
    )
    op.create_index(
        'ix_memory_suggestions_conversation_status', 'memory_suggestions',
        ['conversation_id', 'status'],
    )


def downgrade() -> None:
    """Drop the memory_suggestions table (pending suggestions are lost)."""
    op.drop_index(
        'ix_memory_suggestions_conversation_status',
        table_name='memory_suggestions',
    )
    op.drop_index('ix_memory_suggestions_user_id', table_name='memory_suggestions')
    op.drop_table('memory_suggestions')
