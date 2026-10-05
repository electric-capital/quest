"""create docs and doc_shares tables

Revision ID: 55983a10e266
Revises: c9e1f4a7b2d8
Create Date: 2026-10-05 18:00:00.000000

Quest Docs (phase 1). A doc is a ``docs`` row plus a directory
``<data_dir>/docs/<doc_id>/`` holding ``doc.md``, ``assets/`` and
``revisions/`` (chat/docs/files.py); the body never lives in the DB.

``docs`` carries the metadata the list views and the access rule need:
owner, optional project (NULL = a user doc), title/description, the
``private``/``public`` mode, the cached body size and asset count, and the
source of the last write. Title uniqueness is case-insensitive per
``(owner_id, project_id, mode)`` and enforced in db/doc_store.py, because the
NULL ``project_id`` case defeats a plain unique index.

``doc_shares`` grants ``read`` or ``write`` to one user, or to everyone on
the install when ``user_id`` is NULL. SQLite treats NULLs as distinct in
the unique ``(doc_id, user_id)`` index, so the store additionally enforces
"at most one everyone row per doc".

Both tables are new, so existing installs see no behavior change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '55983a10e266'
down_revision: Union[str, Sequence[str], None] = 'c9e1f4a7b2d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create docs and doc_shares."""
    op.create_table(
        'docs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('content_size', sa.Integer(), nullable=False),
        sa.Column('asset_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_write_source', sa.String(length=80), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_docs_owner_id', 'docs', ['owner_id'], unique=False)
    op.create_index('ix_docs_project_id', 'docs', ['project_id'], unique=False)
    op.create_index('ix_docs_updated_at', 'docs', ['updated_at'], unique=False)

    op.create_table(
        'doc_shares',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('doc_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('permission', sa.String(length=8), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['doc_id'], ['docs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_doc_shares_doc_id_user_id', 'doc_shares', ['doc_id', 'user_id'],
        unique=True,
    )
    op.create_index('ix_doc_shares_user_id', 'doc_shares', ['user_id'], unique=False)
    # SQLite treats NULLs as distinct in the unique index above, so the
    # "at most one everyone row per doc" rule gets its own partial index.
    op.create_index(
        'ix_doc_shares_everyone', 'doc_shares', ['doc_id'], unique=True,
        sqlite_where=sa.text('user_id IS NULL'),
    )


def downgrade() -> None:
    """Drop doc_shares and docs."""
    op.drop_index('ix_doc_shares_everyone', table_name='doc_shares')
    op.drop_index('ix_doc_shares_user_id', table_name='doc_shares')
    op.drop_index('ix_doc_shares_doc_id_user_id', table_name='doc_shares')
    op.drop_table('doc_shares')
    op.drop_index('ix_docs_updated_at', table_name='docs')
    op.drop_index('ix_docs_project_id', table_name='docs')
    op.drop_index('ix_docs_owner_id', table_name='docs')
    op.drop_table('docs')
