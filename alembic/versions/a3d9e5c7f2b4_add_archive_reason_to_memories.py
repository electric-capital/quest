"""add archive_reason to memories and project_memories

Revision ID: a3d9e5c7f2b4
Revises: e7c2a9f4b1d6
Create Date: 2026-11-10 12:00:00.000000

A nullable text column on both memory tables: why the memory was archived,
set by the ``archive_memory`` action request and cleared on unarchive.
Memories archived any other way (the Settings table, an accepted
suggestion) keep it NULL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3d9e5c7f2b4'
down_revision: Union[str, Sequence[str], None] = 'e7c2a9f4b1d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('memories', sa.Column('archive_reason', sa.Text(), nullable=True))
    op.add_column('project_memories', sa.Column('archive_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('project_memories', 'archive_reason')
    op.drop_column('memories', 'archive_reason')
