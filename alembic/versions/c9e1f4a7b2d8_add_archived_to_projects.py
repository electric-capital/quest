"""add archived column to projects

Revision ID: c9e1f4a7b2d8
Revises: a3d6f8b2c917
Create Date: 2026-10-05 12:00:00.000000

Adds an ``archived`` boolean column to ``projects``, mirroring the
``conversations.archived`` soft-hide flag: an archived project is dropped
from the default project list (the sidebar's "Show Archived" toggle brings
it back), its scheduled routines are passed over by the scheduler, and
everything else -- rows, workspace, conversations, skills -- is kept.
Unarchiving restores it exactly.

The column is NOT NULL with a server default of false: every existing
project stays visible, which preserves current behavior exactly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9e1f4a7b2d8'
down_revision: Union[str, Sequence[str], None] = 'a3d6f8b2c917'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add archived column to projects."""
    op.add_column(
        'projects',
        sa.Column(
            'archived', sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Remove archived from projects."""
    op.drop_column('projects', 'archived')
