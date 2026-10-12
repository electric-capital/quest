"""multi-connection user_service_credentials (account, description, is_default)

Revision ID: e3c9b7a1d5f2
Revises: b4d7e2a9c6f1
Create Date: 2026-10-12 12:00:00.000000

A user may now hold several connections to one service. Adds to
``user_service_credentials``:

- ``account`` (String(320), NOT NULL, default ''): the connection's account
  address or user-chosen name, stored lower-cased. Legacy rows get ''.
- ``description`` (Text, NULL): optional user-written description.
- ``is_default`` (Boolean, NOT NULL, default 1): the default-connection
  marker. Every legacy row is the only row of its (user, service) and
  therefore the default.

Index swap: the unique ``(user_id, service)`` index is replaced by a unique
``(user_id, service, account)`` index plus a partial unique ``(user_id,
service) WHERE is_default`` index, so two defaults can never coexist.
``ix_user_service_credentials_user_id`` stays.

Runs in ``batch_alter_table`` (``recreate="auto"``): adding columns and
swapping indexes are native SQLite operations, so the upgrade alters the
table in place; the downgrade's column drops make Alembic rebuild the
table. A rebuild copies every row with a plain ``INSERT ... SELECT`` at the
SQL level, so the encrypted ``secret`` / ``oauth_blob`` envelopes are copied
verbatim (no decrypt / re-encrypt). The rebuilt table is a child table that
no other table references, so dropping the old copy cascades nowhere even
with ``PRAGMA foreign_keys=ON``.

Downgrade refuses (RuntimeError) while any (user, service) has more than one
row: the old schema cannot hold them, and picking one to keep would silently
discard credentials.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3c9b7a1d5f2'
down_revision: Union[str, Sequence[str], None] = 'b4d7e2a9c6f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'user_service_credentials'
_OLD_UNIQUE = 'ix_user_service_credentials_user_id_service'
_ACCOUNT_UNIQUE = 'ix_user_service_credentials_user_service_account'
_DEFAULT_UNIQUE = 'ix_user_service_credentials_user_service_default'


def upgrade() -> None:
    """Add the connection columns and swap the unique indexes."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column(
            'account', sa.String(length=320), nullable=False,
            server_default=sa.text("''"),
        ))
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column(
            'is_default', sa.Boolean(), nullable=False,
            server_default=sa.text('1'),
        ))
        batch_op.drop_index(_OLD_UNIQUE)
        batch_op.create_index(
            _ACCOUNT_UNIQUE, ['user_id', 'service', 'account'], unique=True,
        )
        batch_op.create_index(
            _DEFAULT_UNIQUE, ['user_id', 'service'], unique=True,
            sqlite_where=sa.text('is_default'),
        )


def downgrade() -> None:
    """Drop the connection columns, restoring one row per (user, service).

    Refuses while any (user, service) holds more than one connection.
    """
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(
        f"SELECT user_id, service, COUNT(*) FROM {_TABLE} "
        "GROUP BY user_id, service HAVING COUNT(*) > 1"
    )).fetchall()
    if duplicates:
        listed = ", ".join(
            f"user {user_id} / {service} ({count} rows)"
            for user_id, service, count in duplicates[:10]
        )
        more = f" and {len(duplicates) - 10} more" if len(duplicates) > 10 else ""
        raise RuntimeError(
            "Cannot downgrade user_service_credentials: the old schema holds "
            "one connection per (user, service), but these have several: "
            f"{listed}{more}. Delete the extra connections first."
        )

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_index(_DEFAULT_UNIQUE)
        batch_op.drop_index(_ACCOUNT_UNIQUE)
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column('is_default')
        batch_op.drop_column('description')
        batch_op.drop_column('account')
    op.create_index(_OLD_UNIQUE, _TABLE, ['user_id', 'service'], unique=True)
