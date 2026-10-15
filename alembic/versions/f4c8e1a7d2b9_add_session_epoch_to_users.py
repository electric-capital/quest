"""add session_epoch to users

Revision ID: f4c8e1a7d2b9
Revises: d4f8b2a6c1e9
Create Date: 2026-10-14 20:00:00.000000

Adds ``users.session_epoch``, the per-account session generation. Every
session cookie embeds the epoch it was issued under and is rejected once
the stored value differs, so bumping the column (sign-out, password
change) ends every session of the account server-side.

NOT NULL with a server default of 0: existing rows start at generation 0.
Cookies issued before this revision carry no epoch and fail verification
anyway (the cookie format changed alongside it), so no backfill is needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4c8e1a7d2b9'
down_revision: Union[str, Sequence[str], None] = 'd4f8b2a6c1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add session_epoch to users."""
    op.add_column(
        'users',
        sa.Column(
            'session_epoch', sa.Integer(), nullable=False,
            server_default='0',
        ),
    )


def downgrade() -> None:
    """Remove session_epoch from users."""
    op.drop_column('users', 'session_epoch')
