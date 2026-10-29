"""add dreaming: dream_runs table, memory_suggestions source and archive-only rows

Revision ID: b4e8d2c7a1f3
Revises: 7103d39f4efb
Create Date: 2026-10-29 12:00:00.000000

A dream (chat/dreaming.py) is an offline review of a user's memory store
that proposes hygiene changes -- merge duplicates, archive stale or
contradicted facts, absolutise relative dates, shorten over-long entries --
as ``memory_suggestions`` rows for the user to accept or dismiss. Two
things about those rows did not fit the table as the ``suggest_memory``
tool left it:

- they belong to no conversation, so ``conversation_id`` becomes nullable
  and ``source`` says where a row came from (``conversation`` for every
  existing row, ``dream`` for a dream's), with ``dream_run_id`` pointing at
  the run that proposed it;
- an archive-only proposal has no new text, so ``content`` becomes
  nullable (NULL = accepting only archives ``replaces_memory_id``).

The rebuild also makes ``created_at`` NOT NULL, as the ORM has always
declared it (every writer sets it; c7e2a9b4d1f8 just created it nullable).

SQLite cannot relax NOT NULL in place, so ``memory_suggestions`` is rebuilt
in batch mode (copy, drop, rename). That is safe here, unlike for ``docs``
in 7103d39f4efb: no table references ``memory_suggestions``, so dropping
the old copy under ``PRAGMA foreign_keys=ON`` cascades nothing, and its
own foreign keys are table-level clauses whose ON DELETE actions survive
reflection. ``dream_runs`` is created first because the rebuilt table
references it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e8d2c7a1f3'
down_revision: Union[str, Sequence[str], None] = '7103d39f4efb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create dream_runs, then rebuild memory_suggestions with the new shape."""
    op.create_table(
        'dream_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('trigger', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('thinking_effort', sa.String(length=20), nullable=True),
        sa.Column('memories_read', sa.Integer(), nullable=False),
        sa.Column('suggestions_created', sa.Integer(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('cached_tokens', sa.Integer(), nullable=True),
        sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_dream_runs_user_id', 'dream_runs', ['user_id'])
    op.create_index(
        'ix_dream_runs_user_started', 'dream_runs', ['user_id', 'started_at'],
    )

    with op.batch_alter_table('memory_suggestions', recreate='always') as batch_op:
        batch_op.alter_column(
            'conversation_id', existing_type=sa.String(length=36), nullable=True,
        )
        batch_op.alter_column('content', existing_type=sa.Text(), nullable=True)
        # c7e2a9b4d1f8 left created_at nullable although the ORM always sets
        # it; the rebuild is the moment to make the schema say so too.
        batch_op.alter_column(
            'created_at', existing_type=sa.DateTime(), nullable=False,
        )
        batch_op.add_column(
            sa.Column(
                'source', sa.String(length=20), nullable=False,
                server_default='conversation',
            ),
        )
        batch_op.add_column(
            sa.Column('dream_run_id', sa.String(length=36), nullable=True),
        )
        batch_op.create_foreign_key(
            'fk_memory_suggestions_dream_run_id', 'dream_runs',
            ['dream_run_id'], ['id'], ondelete='SET NULL',
        )


def downgrade() -> None:
    """Drop the dream rows (nothing can hold them), restore the old shape."""
    op.execute("DELETE FROM memory_suggestions WHERE source = 'dream'")
    with op.batch_alter_table('memory_suggestions', recreate='always') as batch_op:
        batch_op.drop_constraint(
            'fk_memory_suggestions_dream_run_id', type_='foreignkey',
        )
        batch_op.drop_column('dream_run_id')
        batch_op.drop_column('source')
        batch_op.alter_column('content', existing_type=sa.Text(), nullable=False)
        batch_op.alter_column(
            'conversation_id', existing_type=sa.String(length=36), nullable=False,
        )
        batch_op.alter_column('created_at', existing_type=sa.DateTime(), nullable=True)
    op.drop_index('ix_dream_runs_user_started', table_name='dream_runs')
    op.drop_index('ix_dream_runs_user_id', table_name='dream_runs')
    op.drop_table('dream_runs')
