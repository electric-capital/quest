"""End-to-end test of migration e7c2a9f4b1d6 (routine and archived
conversations are read).

Builds a database at the previous head in a temp data dir, seeds
conversations in each state the previous release could leave, upgrades and
checks which rows changed: a routine's conversation goes back to (0, 0),
an archived one has ``read_seq`` raised to ``unread_seq``, and everything
else is untouched. The downgrade changes nothing.

Runs in this process against the temp deployment set up by
``tests/migration_support.py``.
"""

from __future__ import annotations

from config import encryption
from tests.migration_support import PASSWORD, deployment, execute, rows  # noqa: F401

REV = "e7c2a9f4b1d6"
PREVIOUS = "d4b8f1a6c3e7"

# id -> (routine_id, archived, unread_seq, read_seq) before the upgrade.
SEEDED = {
    "unread": (None, 0, 6, 2),
    "read": (None, 0, 6, 6),
    "routine-unread": ("r1", 0, 8, 0),
    "routine-read": ("r1", 0, 8, 8),
    "routine-archived": ("r1", 1, 8, 3),
    "archived-unread": (None, 1, 5, 1),
    "archived-read": (None, 1, 5, 5),
}

EXPECTED = {
    "unread": (6, 2),
    "read": (6, 6),
    "routine-unread": (0, 0),
    "routine-read": (0, 0),
    "routine-archived": (0, 0),
    "archived-unread": (5, 5),
    "archived-read": (5, 5),
}


def _seqs(db_path) -> dict:
    return {
        conv_id: (unread_seq, read_seq)
        for conv_id, unread_seq, read_seq in rows(
            db_path, "SELECT id, unread_seq, read_seq FROM conversations",
        )
    }


def test_upgrade_marks_routine_and_archived_conversations_read(deployment):
    db_path = deployment.db_path
    # Earlier revisions in the chain encrypt stored secrets.
    encryption.create_key_file(PASSWORD, deployment.key_file)
    deployment.alembic("upgrade", PREVIOUS)
    execute(
        db_path,
        "INSERT INTO users (id, email, name, api_key, settings, created_at) "
        "VALUES (1, 'a@example.com', 'A', 'plain', '{}', CURRENT_TIMESTAMP)",
    )
    # sqlite3 does not enforce foreign keys by default, so the routine row
    # itself is not needed.
    for conv_id, (routine_id, archived, unread_seq, read_seq) in SEEDED.items():
        execute(
            db_path,
            "INSERT INTO conversations (id, user_id, routine_id, archived, "
            "unread_seq, read_seq, last_message_seq, created_at, last_message_at) "
            "VALUES (?, 1, ?, ?, ?, ?, 9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (conv_id, routine_id, archived, unread_seq, read_seq),
        )

    deployment.alembic("upgrade", REV)
    assert _seqs(db_path) == EXPECTED

    # Idempotent, and the downgrade leaves the data alone.
    deployment.alembic("downgrade", PREVIOUS)
    assert _seqs(db_path) == EXPECTED
    deployment.alembic("upgrade", "head")
    assert _seqs(db_path) == EXPECTED
