"""Tests for the paged/filtered conversation list query.

Covers ``db.conversation_store.list_conversations_meta``'s keyword-only
additions behind the paged sidebar list (GET /conversations):

1. Server-side filters: ``exclude_projects``, ``include_slack=False``,
   ``include_inference=False``, ``include_routines=False`` -- including the
   NULL-origin subtlety (legacy web rows have ``origin IS NULL`` and must
   survive origin filters).
2. Keyset pagination: ``limit`` + ``before=(last_message_at, id)`` walks the
   full list in ``(last_message_at DESC, id DESC)`` order without overlap
   or gaps, and stays stable when newer rows are prepended mid-walk.

Store tests run against the shared ``isolated_db`` fixture (a fresh SQLite
file per test, see tests/db_support.py) with asyncio.run.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.conversation_store as store_mod

    return store_mod, isolated_db.models


BASE_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_conversation(store, user_id=1, *, hours_ago=0, project_id=None,
                       origin=None, archived=False, routine_id=None):
    """Insert one conversation row; returns its id."""
    conv_id = str(uuid.uuid4())
    ts = BASE_TS - timedelta(hours=hours_ago)
    _run(store.create_conversation(
        user_id, conv_id, ts, project_id=project_id, origin=origin,
        routine_id=routine_id,
    ))
    if archived:
        _run(store.archive_conversation(user_id, conv_id))
    return conv_id


def _make_user(models_mod, store, user_id, email):
    async def _do():
        async with store.AsyncSessionLocal() as db:
            db.add(models_mod.User(
                id=user_id, email=email, api_key=f"key-{user_id}",
            ))
            await db.commit()
    _run(_do())


def _make_project(models_mod, store, user_id):
    project_id = str(uuid.uuid4())

    async def _do():
        async with store.AsyncSessionLocal() as db:
            db.add(models_mod.Project(
                id=project_id, user_id=user_id, name="P",
                created_at=BASE_TS, updated_at=BASE_TS,
            ))
            await db.commit()
    _run(_do())
    return project_id


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_filters_drop_projects_and_origins_but_keep_null_origin(_isolated_db):
    store, models_mod = _isolated_db
    _make_user(models_mod, store, 1, "a@example.com")
    project_id = _make_project(models_mod, store, 1)

    legacy_web = _make_conversation(store, hours_ago=1)  # origin IS NULL
    web = _make_conversation(store, hours_ago=2, origin="web")
    slack = _make_conversation(store, hours_ago=3, origin="slack")
    inference = _make_conversation(store, hours_ago=4, origin="inference_api")
    subagent = _make_conversation(store, hours_ago=5, origin="user_subagent")
    in_project = _make_conversation(store, hours_ago=6, project_id=project_id)

    everything = _run(store.list_conversations_meta(1))
    assert {c["id"] for c in everything} == {
        legacy_web, web, slack, inference, subagent, in_project,
    }

    filtered = _run(store.list_conversations_meta(
        1, exclude_projects=True, include_slack=False, include_inference=False,
    ))
    # NULL-origin legacy rows and user_subagent rows must survive the
    # origin filters; only slack/inference/project rows drop.
    assert [c["id"] for c in filtered] == [legacy_web, web, subagent]


def _make_routine(models_mod, store, user_id, project_id):
    routine_id = str(uuid.uuid4())

    async def _do():
        async with store.AsyncSessionLocal() as db:
            db.add(models_mod.Routine(
                id=routine_id, project_id=project_id, user_id=user_id,
                name="R", prompt="do the thing",
            ))
            await db.commit()
    _run(_do())
    return routine_id


def test_routine_rows_dropped_when_excluded(_isolated_db):
    store, models_mod = _isolated_db
    _make_user(models_mod, store, 1, "a@example.com")
    project_id = _make_project(models_mod, store, 1)
    routine_id = _make_routine(models_mod, store, 1, project_id)

    chat = _make_conversation(store, hours_ago=1)
    run = _make_conversation(
        store, hours_ago=2, project_id=project_id, routine_id=routine_id,
    )
    in_project = _make_conversation(store, hours_ago=3, project_id=project_id)

    everything = _run(store.list_conversations_meta(1))
    assert [c["id"] for c in everything] == [chat, run, in_project]
    assert [c["routine_id"] for c in everything] == [None, routine_id, None]

    no_runs = _run(store.list_conversations_meta(1, include_routines=False))
    assert [c["id"] for c in no_runs] == [chat, in_project]


def test_archived_rows_hidden_unless_included(_isolated_db):
    store, models_mod = _isolated_db
    _make_user(models_mod, store, 1, "a@example.com")

    active = _make_conversation(store, hours_ago=1)
    archived = _make_conversation(store, hours_ago=2, archived=True)

    assert [c["id"] for c in _run(store.list_conversations_meta(1))] == [active]
    both = _run(store.list_conversations_meta(1, include_archived=True))
    assert [c["id"] for c in both] == [active, archived]


def test_filters_scoped_to_user(_isolated_db):
    store, models_mod = _isolated_db
    _make_user(models_mod, store, 1, "a@example.com")
    _make_user(models_mod, store, 2, "b@example.com")

    mine = _make_conversation(store, user_id=1, hours_ago=1)
    _make_conversation(store, user_id=2, hours_ago=2)

    assert [c["id"] for c in _run(store.list_conversations_meta(1))] == [mine]


# ---------------------------------------------------------------------------
# Keyset pagination
# ---------------------------------------------------------------------------

def _walk_pages(store, user_id, page_size, **kwargs):
    """Walk the whole list via limit+before, returning the pages."""
    pages = []
    before = None
    while True:
        rows = _run(store.list_conversations_meta(
            user_id, limit=page_size, before=before, **kwargs,
        ))
        if not rows:
            break
        pages.append(rows)
        last = rows[-1]
        before = (
            datetime.fromisoformat(last["last_message_at"]), last["id"],
        )
        if len(rows) < page_size:
            break
    return pages


def test_keyset_pagination_walks_all_rows_without_overlap(_isolated_db):
    store, models_mod = _isolated_db
    _make_user(models_mod, store, 1, "a@example.com")

    expected = [_make_conversation(store, hours_ago=i) for i in range(7)]

    pages = _walk_pages(store, 1, page_size=3)
    assert [len(p) for p in pages] == [3, 3, 1]
    walked = [c["id"] for page in pages for c in page]
    assert walked == expected  # newest first, no dup, no gap


def test_keyset_pagination_ties_broken_by_id_desc(_isolated_db):
    store, models_mod = _isolated_db
    _make_user(models_mod, store, 1, "a@example.com")

    # Five rows sharing one last_message_at: order falls back to id DESC.
    ids = sorted(
        (_make_conversation(store, hours_ago=1) for _ in range(5)),
        reverse=True,
    )

    pages = _walk_pages(store, 1, page_size=2)
    walked = [c["id"] for page in pages for c in page]
    assert walked == ids


def test_keyset_page_stable_when_newer_rows_arrive(_isolated_db):
    store, models_mod = _isolated_db
    _make_user(models_mod, store, 1, "a@example.com")

    old = [_make_conversation(store, hours_ago=10 + i) for i in range(4)]

    first = _run(store.list_conversations_meta(1, limit=2))
    assert [c["id"] for c in first] == old[:2]
    last = first[-1]
    cursor = (datetime.fromisoformat(last["last_message_at"]), last["id"])

    # A conversation created (or bumped) after page 1 was fetched must not
    # shift rows into page 2 -- keyset, not OFFSET.
    _make_conversation(store, hours_ago=0)

    second = _run(store.list_conversations_meta(1, limit=2, before=cursor))
    assert [c["id"] for c in second] == old[2:]
