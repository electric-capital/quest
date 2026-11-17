"""add autoproject_runs.context and autoproject_runs.context_brief_version

Revision ID: 50a994f1ef52
Revises: 4b57cca1158c
Create Date: 2026-11-10 18:00:00.000000

An AutoProject run's system prompt carries a ``## AutoProject`` block built from the AutoProject's
instructions, brief and log. The block is frozen on the run row on the run's
first turn, with the brief version it shows, so a run resumed after a server
restart (a question answered days later) rebuilds the prompt it started with
rather than one built from the AutoProject as it now is.

Existing rows need no backfill: NULL means "not frozen yet", and a run with
no frozen context freezes one on its next turn.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '50a994f1ef52'
down_revision: Union[str, Sequence[str], None] = '4b57cca1158c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the frozen run context and its brief version to autoproject_runs."""
    op.add_column('autoproject_runs', sa.Column('context', sa.Text(), nullable=True))
    op.add_column(
        'autoproject_runs',
        sa.Column('context_brief_version', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Remove the frozen run context columns from autoproject_runs."""
    with op.batch_alter_table('autoproject_runs', schema=None) as batch_op:
        batch_op.drop_column('context_brief_version')
        batch_op.drop_column('context')
