"""create data_tables and data_table_shares

Revision ID: e5c97b362821
Revises: c7e2a9b4d1f8
Create Date: 2026-10-14 18:00:00.000000

Quest Tables (phase A1). A table is a ``data_tables`` row plus a directory
``<data_dir>/tables/<table_id>/`` holding ``table.db`` (rows, change log,
meta); the rows never live in quest.db.

``data_tables`` carries the metadata the list views and the access rule
need: owner, optional project (NULL = a user table), title/description, the
``private``/``public`` mode, the ordered column list (``schema`` JSON) with
its ``schema_version`` concurrency token, the cached ``row_count``, the
owner's ``require_approval`` switch and the source of the last write. The
SQL name is ``data_tables`` because ``tables`` reads badly next to
SQLAlchemy metadata. Title uniqueness is case-insensitive per
``(owner_id, project_id, mode)`` and enforced in db/table_store.py, because
the NULL ``project_id`` case defeats a plain unique index.

``data_table_shares`` grants ``read`` or ``write`` to one user, or to
everyone on the install when ``user_id`` is NULL; a partial unique index
backs the store's "at most one everyone row per table" rule.

Both tables are new, so existing installs see no behavior change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5c97b362821'
down_revision: Union[str, Sequence[str], None] = 'c7e2a9b4d1f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create data_tables and data_table_shares."""
    op.create_table(
        'data_tables',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('schema', sa.JSON(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('row_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'require_approval', sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column('last_write_source', sa.String(length=80), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_data_tables_owner_id', 'data_tables', ['owner_id'], unique=False)
    op.create_index('ix_data_tables_project_id', 'data_tables', ['project_id'], unique=False)
    op.create_index('ix_data_tables_updated_at', 'data_tables', ['updated_at'], unique=False)

    op.create_table(
        'data_table_shares',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('table_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('permission', sa.String(length=8), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['table_id'], ['data_tables.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_data_table_shares_table_id_user_id', 'data_table_shares',
        ['table_id', 'user_id'], unique=True,
    )
    op.create_index(
        'ix_data_table_shares_user_id', 'data_table_shares', ['user_id'],
        unique=False,
    )
    # SQLite treats NULLs as distinct in the unique index above, so the
    # "at most one everyone row per table" rule gets its own partial index.
    op.create_index(
        'ix_data_table_shares_everyone', 'data_table_shares', ['table_id'],
        unique=True, sqlite_where=sa.text('user_id IS NULL'),
    )


def downgrade() -> None:
    """Drop data_table_shares and data_tables."""
    op.drop_index('ix_data_table_shares_everyone', table_name='data_table_shares')
    op.drop_index('ix_data_table_shares_user_id', table_name='data_table_shares')
    op.drop_index('ix_data_table_shares_table_id_user_id', table_name='data_table_shares')
    op.drop_table('data_table_shares')
    op.drop_index('ix_data_tables_updated_at', table_name='data_tables')
    op.drop_index('ix_data_tables_project_id', table_name='data_tables')
    op.drop_index('ix_data_tables_owner_id', table_name='data_tables')
    op.drop_table('data_tables')
