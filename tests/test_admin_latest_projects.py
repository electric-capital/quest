"""Tests for the Projects and AutoProjects sections of System Reports.

Drives the two route handlers of chat/routes/admin.py
(``admin_latest_active_projects`` / ``admin_latest_active_autoprojects``)
against the shared ``isolated_db`` fixture, so the store queries
(``list_latest_active_projects``, ``list_latest_active_autoprojects``,
``list_group_conversation_rows``) and the per-group usage fold
(``get_usage_by_conversation_group``) are covered together with the row
assembly.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text, update


def _run(coro):
    return asyncio.run(coro)


ADMIN = {"id": 1, "email": "admin@example.com"}


@pytest.fixture()
def env(isolated_db, monkeypatch):
    import chat.routes.admin as admin_mod
    import db.user_store as user_store

    monkeypatch.setattr(admin_mod, "is_admin", lambda email: email == ADMIN["email"])
    # Active days come from chat_history.json; tests supply them per conversation.
    days: dict[str, set] = {}
    monkeypatch.setattr(
        admin_mod.ChatStorage, "user_message_active_days",
        staticmethod(lambda conversation_id, *_a: set(days.get(conversation_id, ()))),
    )
    alice = _run(user_store.create_user("alice@example.com", "Alice", "key-a"))
    bob = _run(user_store.create_user("bob@example.com", "", "key-b"))
    return {
        "admin": admin_mod, "models": isolated_db.models, "days": days,
        "alice": alice["id"], "bob": bob["id"],
    }


def _conversation(user_id, *, last_message_at=None, **kwargs) -> str:
    import db.conversation_store as conv_store

    conv_id = str(uuid.uuid4())
    _run(conv_store.create_conversation(
        user_id, conv_id, datetime(2026, 1, 1, tzinfo=timezone.utc), **kwargs
    ))
    if last_message_at is not None:
        _run(conv_store.update_last_message_at(conv_id, last_message_at))
    return conv_id


def _record(models_mod, conversation_id, user_id, output_tokens, model="claude-opus-4-8"):
    import db.llm_call_store as store

    provider = "anthropic" if model.startswith("claude") else "gemini"
    raw = (
        {"input_tokens": 100, "output_tokens": output_tokens,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        if provider == "anthropic"
        else {"prompt_token_count": 100, "candidates_token_count": output_tokens}
    )
    _run(store.record_api_call(
        conversation_id=conversation_id, user_id=user_id, model=model,
        call_type=models_mod.ApiCallType.TOP_LEVEL, input_tokens=100,
        output_tokens=output_tokens, duration_ms=10, provider=provider,
        raw_usage=raw,
    ))


def _at(day: int) -> datetime:
    return datetime(2026, 3, day, 12, 0, tzinfo=timezone.utc)


def test_endpoints_require_admin(env):
    for handler in (
        env["admin"].admin_latest_active_projects,
        env["admin"].admin_latest_active_autoprojects,
    ):
        with pytest.raises(HTTPException) as exc:
            _run(handler(limit=20, user={"id": 2, "email": "alice@example.com"}))
        assert exc.value.status_code == 403


def test_projects_aggregate_their_conversations(env):
    import db.project_store as project_store

    alice, bob = env["alice"], env["bob"]
    older = _run(project_store.create_project(alice, "Older"))
    newer = _run(project_store.create_project(bob, "Newer", public=True))
    _run(project_store.create_project(alice, "Never used"))

    chat_a = _conversation(alice, project_id=older["id"], last_message_at=_at(1))
    chat_b = _conversation(alice, project_id=older["id"], last_message_at=_at(3))
    routine_chat = _conversation(alice, project_id=older["id"], last_message_at=_at(2))
    _set_routine(env, routine_chat)
    _conversation(bob, project_id=newer["id"], last_message_at=_at(5))  # no calls
    loose = _conversation(alice, last_message_at=_at(9))

    models_mod = env["models"]
    _record(models_mod, chat_a, alice, 1_000)
    _record(models_mod, chat_b, alice, 2_000)
    _record(models_mod, routine_chat, alice, 4_000)
    _record(models_mod, chat_b, alice, 50, model="gemini-3.5-flash")
    _record(models_mod, loose, alice, 9_000)
    env["days"].update({
        chat_a: {"2026-03-01", "2026-03-02"},
        chat_b: {"2026-03-02", "2026-03-03"},
        routine_chat: {"2026-03-08"},  # routine runs are not a person's activity
    })

    rows = _run(env["admin"].admin_latest_active_projects(limit=20, user=ADMIN))["projects"]

    # Newest conversation activity first; a project without conversations is absent.
    assert [r["name"] for r in rows] == ["Newer", "Older"]
    newer_row, older_row = rows
    assert newer_row["public"] is True and newer_row["user_email"] == "bob@example.com"
    assert newer_row["conversation_count"] == 1
    assert newer_row["usage_by_model"] == []
    assert newer_row["usage_total"]["call_count"] == 0

    assert older_row["user_name"] == "Alice"
    assert older_row["conversation_count"] == 3
    assert older_row["routine_conversation_count"] == 1
    assert older_row["last_message_at"].startswith("2026-03-03")
    assert older_row["active_days"] == 3
    # One merged entry per model across the project's conversations, routine
    # run included; the loose conversation's call is not counted.
    by_model = {m["model"]: m for m in older_row["usage_by_model"]}
    assert set(by_model) == {"claude-opus-4-8", "gemini-3.5-flash"}
    assert by_model["claude-opus-4-8"]["call_count"] == 3
    assert by_model["claude-opus-4-8"]["metrics"]["output_tokens"] == 7_000
    assert older_row["usage_total"]["call_count"] == 4
    assert older_row["usage_total"]["estimated_cost_usd"] == pytest.approx(
        sum(m["estimated_cost_usd"] for m in older_row["usage_by_model"])
    )

    assert len(_run(
        env["admin"].admin_latest_active_projects(limit=1, user=ADMIN)
    )["projects"]) == 1


def _set_routine(env, conversation_id):
    """Mark a conversation as a routine run without building a routine row."""
    import db.conversation_store as conv_store

    Conversation = env["models"].Conversation

    async def _do():
        async with conv_store.AsyncSessionLocal() as db:
            await db.execute(text("PRAGMA foreign_keys=OFF"))
            await db.execute(
                update(Conversation).where(Conversation.id == conversation_id)
                .values(routine_id="routine-1")
            )
            await db.commit()
    _run(_do())


def test_unpriced_model_nulls_the_project_total(env):
    import db.project_store as project_store

    alice = env["alice"]
    project = _run(project_store.create_project(alice, "Mixed"))
    chat = _conversation(alice, project_id=project["id"], last_message_at=_at(1))
    _record(env["models"], chat, alice, 1_000)
    _record(env["models"], chat, alice, 10, model="gemini-unpriced-model")

    row = _run(env["admin"].admin_latest_active_projects(limit=20, user=ADMIN))["projects"][0]
    assert row["usage_total"]["estimated_cost_usd"] is None
    assert row["usage_total"]["call_count"] == 2


def test_autoprojects_aggregate_their_runs(env):
    import db.autoproject_store as ap_store

    alice, bob = env["alice"], env["bob"]
    models_mod = env["models"]
    first = _run(ap_store.create_autoproject(alice, title="First"))
    second = _run(ap_store.create_autoproject(bob, title="Second"))
    empty = _run(ap_store.create_autoproject(bob, title="No runs"))

    def run(autoproject, owner, status, started_at, tokens):
        conv = _conversation(owner, autoproject_id=autoproject["id"], origin="autoproject")
        allocated = _run(ap_store.allocate_run(
            autoproject["id"], conversation_id=conv, prompt="go",
        ))
        # One active run per AutoProject, so finish each before the next.
        _stamp(
            env, models_mod.AutoProjectRun, allocated["id"],
            status=status, started_at=started_at,
        )
        _record(models_mod, conv, owner, tokens)

    run(first, alice, "completed", _at(1), 1_000)
    run(first, alice, "failed", _at(1), 2_000)
    run(first, alice, "completed", _at(4), 3_000)
    run(second, bob, "completed", _at(2), 500)
    for autoproject, day in ((first, 4), (second, 6), (empty, 5)):
        _stamp(env, models_mod.AutoProject, autoproject["id"], last_activity_at=_at(day))

    rows = _run(
        env["admin"].admin_latest_active_autoprojects(limit=20, user=ADMIN)
    )["autoprojects"]

    assert [r["title"] for r in rows] == ["Second", "No runs", "First"]
    second_row, empty_row, first_row = rows
    assert empty_row["run_count"] == 0 and empty_row["active_days"] == 0
    assert empty_row["usage_total"]["call_count"] == 0
    assert second_row["user_email"] == "bob@example.com"
    assert second_row["run_count"] == 1

    assert first_row["run_count"] == 3
    assert first_row["failed_run_count"] == 1
    assert first_row["active_days"] == 2
    assert first_row["last_activity_at"].startswith("2026-03-04")
    assert [m["model"] for m in first_row["usage_by_model"]] == ["claude-opus-4-8"]
    assert first_row["usage_by_model"][0]["metrics"]["output_tokens"] == 6_000
    assert first_row["usage_total"]["call_count"] == 3


def _stamp(env, row_cls, row_id, **values):
    import db.conversation_store as conv_store

    async def _do():
        async with conv_store.AsyncSessionLocal() as db:
            await db.execute(update(row_cls).where(row_cls.id == row_id).values(**values))
            await db.commit()
    _run(_do())
