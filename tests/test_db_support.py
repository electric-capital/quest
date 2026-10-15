"""The shared ``isolated_db`` fixture (tests/db_support.py).

Pins what every store test relies on: a fresh database per test with the
real ORM schema, foreign keys enforced, the store modules and ``db.engine``
pointed at it, and the real session factory back in place afterwards.
"""

import asyncio
import uuid

import db.engine as engine_mod
from tests.db_support import SESSION_LOCAL_MODULES

_REAL_SESSION_LOCAL = engine_mod.AsyncSessionLocal


def _run(coro):
    return asyncio.run(coro)


def test_every_listed_module_shares_the_test_session_factory(isolated_db):
    import importlib

    for name in SESSION_LOCAL_MODULES:
        mod = importlib.import_module(name)
        assert mod.AsyncSessionLocal is isolated_db.AsyncSessionLocal, name
    assert engine_mod.AsyncSessionLocal is isolated_db.AsyncSessionLocal
    assert engine_mod.engine is isolated_db.engine
    from config import paths

    assert paths.DATABASE_PATH == isolated_db.path


def test_fresh_schema_with_foreign_keys_enforced(isolated_db):
    """A user's conversations go away with the user: PRAGMA foreign_keys is
    ON for both the async pool and the sync engine."""
    import db.conversation_store as conversation_store
    from datetime import datetime, timezone
    from sqlalchemy import text

    models = isolated_db.models

    async def _scenario():
        async with isolated_db.AsyncSessionLocal() as db:
            user = models.User(email=f"{uuid.uuid4().hex}@example.com", api_key=uuid.uuid4().hex)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            user_id = user.id
        await conversation_store.create_conversation(
            user_id, str(uuid.uuid4()), datetime.now(timezone.utc),
        )
        assert len(await conversation_store.list_conversations_meta(user_id)) == 1
        async with isolated_db.AsyncSessionLocal() as db:
            await db.delete(await db.get(models.User, user_id))
            await db.commit()
        return user_id

    user_id = _run(_scenario())
    with isolated_db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        rows = conn.execute(
            text("SELECT count(*) FROM conversations WHERE user_id = :uid"), {"uid": user_id},
        ).scalar()
    assert rows == 0


def test_real_session_factory_is_restored_after_the_fixture():
    """Runs after the tests above (file order): nothing from them leaked."""
    assert engine_mod.AsyncSessionLocal is _REAL_SESSION_LOCAL
    import db.user_store as user_store

    assert user_store.AsyncSessionLocal is _REAL_SESSION_LOCAL
