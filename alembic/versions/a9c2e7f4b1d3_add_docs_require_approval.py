"""add docs.require_approval

Revision ID: a9c2e7f4b1d3
Revises: e1b7c4d9a2f6
Create Date: 2026-10-07 12:00:00.000000

Per-doc "require approval" switch (Quest Docs). When set by the owner,
every model-initiated write to the doc goes through a ``write_doc``
approval card even where the access matrix would allow a free write (the
owner's own unshared private doc, a public doc from its project's
conversations). Human edits in the UI are unaffected. Default off, so
existing docs keep their current verdicts.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c2e7f4b1d3'
down_revision: Union[str, Sequence[str], None] = 'e1b7c4d9a2f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add require_approval to docs."""
    op.add_column(
        'docs',
        sa.Column(
            'require_approval', sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Remove require_approval from docs."""
    op.drop_column('docs', 'require_approval')
