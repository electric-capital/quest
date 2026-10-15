"""Memory suggestions: the non-blocking ``suggest_memory`` tool and its
review path (db/memory_suggestion_store.py, chat/memory_suggestions.py, the
suggestion routes in chat/memory_routes.py).

Covers the store (create / list / accept with memory creation + replace /
dismiss / resolved-twice / ownership), the tool body's duplicate guards and
cap, the ``memory_suggestions_changed`` event, the dispatch backstop for
non-top-level runs, the prompt tiers that hide the tool, and the REST
routes (404 for a foreign conversation / suggestion, 409 on a resolved
suggestion, 400 on an empty edit).
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.memory_suggestion_store import (
    MAX_PENDING_PER_CONVERSATION,
    SuggestionAlreadyResolvedError,
)

API_KEY = "test-api-key"
BASE_URL = "http://localhost:8000"


def _run(coro):
    return asyncio.run(coro)


def _bullet(name: str) -> str:
    """How _build_dynamic_tools_section documents a callable tool."""
    return f"- **{name}** --"


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated DB with users alice + bob, one conversation each, events captured."""
    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.conversation_store as conversation_store
    import db.memory_store as memory_store
    import db.memory_suggestion_store as suggestion_store
    import db.models as models

    models.Base.metadata.create_all(sync_engine)
    for mod in (conversation_store, memory_store, suggestion_store):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

    published: list[tuple[int, dict]] = []
    from chat.realtime import bus
    monkeypatch.setattr(
        bus, "publish_to_user", lambda user_id, ev: published.append((user_id, ev)),
    )

    async def _seed():
        async with session_local() as db:
            users = [
                models.User(email=f"{name}@example.com", api_key=f"k-{uuid.uuid4().hex}")
                for name in ("alice", "bob")
            ]
            db.add_all(users)
            await db.commit()
            for u in users:
                await db.refresh(u)
            return {u.email.split("@")[0]: {"id": u.id, "email": u.email} for u in users}

    users = _run(_seed())
    conversations = {}
    for name, user in users.items():
        cid = str(uuid.uuid4())
        _run(conversation_store.create_conversation(
            user["id"], cid, datetime.now(timezone.utc),
        ))
        conversations[name] = cid

    class Env:
        pass

    e = Env()
    e.users = users
    e.conversations = conversations
    e.published = published
    e.session_local = session_local
    e.models = models
    e.memory_store = memory_store
    e.store = suggestion_store
    return e


def _uid(env, who):
    return env.users[who]["id"]


def _events(env, type_name="memory_suggestions_changed"):
    return [ev for _uid_, ev in env.published if ev["type"] == type_name]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_create_list_count(self, env):
        store = env.store
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        first = _run(store.create_suggestion(
            alice, cid, "  Prefers window seats  ", reasoning="said  so\ntwice",
        ))
        second = _run(store.create_suggestion(alice, cid, "Drinks pour-over coffee"))

        assert first["status"] == "pending"
        assert first["content"] == "Prefers window seats"
        assert first["reasoning"] == "said so twice"
        assert first["memory_id"] is None and first["replaces_memory_id"] is None

        pending = _run(store.list_pending(alice, cid))
        assert [s["id"] for s in pending] == [first["id"], second["id"]]
        assert [s["replaces_content"] for s in pending] == [None, None]
        assert _run(store.count_pending(alice, cid)) == 2
        # Scoped to the conversation and the user.
        assert _run(store.count_pending(alice, env.conversations["bob"])) == 0
        assert _run(store.list_pending(_uid(env, "bob"), cid)) == []

    def test_create_rejects_bad_content_and_unknown_replacement(self, env):
        store = env.store
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        with pytest.raises(ValueError):
            _run(store.create_suggestion(alice, cid, "   "))
        with pytest.raises(ValueError):
            _run(store.create_suggestion(alice, cid, "x" * 5000))
        with pytest.raises(ValueError):
            _run(store.create_suggestion(
                alice, cid, "New fact", replaces_memory_id="nope",
            ))
        # Another user's memory is not a valid replacement target either.
        bobs = _run(env.memory_store.create_memory(_uid(env, "bob"), "Bob's note"))
        with pytest.raises(ValueError):
            _run(store.create_suggestion(
                alice, cid, "New fact", replaces_memory_id=bobs["id"],
            ))

    def test_accept_creates_memory_and_archives_replaced(self, env):
        store = env.store
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        old = _run(env.memory_store.create_memory(alice, "Drinks drip coffee"))
        s = _run(store.create_suggestion(
            alice, cid, "Drinks pour-over coffee", replaces_memory_id=old["id"],
        ))
        # The list carries the replaced memory's text for the panel's diff.
        [listed] = _run(store.list_pending(alice, cid))
        assert listed["replaces_memory_id"] == old["id"]
        assert listed["replaces_content"] == "Drinks drip coffee"

        suggestion, memory = _run(store.accept_suggestion(alice, s["id"]))
        assert suggestion["status"] == "accepted"
        assert suggestion["memory_id"] == memory["id"]
        assert suggestion["resolved_at"] is not None
        assert memory["content"] == "Drinks pour-over coffee"
        assert memory["archived"] is False

        replaced = _run(env.memory_store.get_memory(alice, old["id"]))
        assert replaced["archived"] is True
        active = _run(env.memory_store.list_memories(alice))
        assert [m["content"] for m in active] == ["Drinks pour-over coffee"]
        assert _run(store.count_pending(alice, cid)) == 0

    def test_accept_with_edited_content(self, env):
        store = env.store
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        s = _run(store.create_suggestion(alice, cid, "Likes tea"))
        _suggestion, memory = _run(store.accept_suggestion(alice, s["id"], "Likes green tea"))
        assert memory["content"] == "Likes green tea"
        with pytest.raises(ValueError):
            s2 = _run(store.create_suggestion(alice, cid, "Likes tea"))
            _run(store.accept_suggestion(alice, s2["id"], "   "))
        # The failed edit left the suggestion pending.
        assert _run(store.get_suggestion(alice, s2["id"]))["status"] == "pending"

    def test_resolve_twice_and_ownership(self, env):
        store = env.store
        alice, bob = _uid(env, "alice"), _uid(env, "bob")
        cid = env.conversations["alice"]
        s = _run(store.create_suggestion(alice, cid, "Fact"))

        assert _run(store.accept_suggestion(bob, s["id"])) is None
        assert _run(store.dismiss_suggestion(bob, s["id"])) is None
        assert _run(store.get_suggestion(bob, s["id"])) is None

        dismissed = _run(store.dismiss_suggestion(alice, s["id"]))
        assert dismissed["status"] == "dismissed"
        with pytest.raises(SuggestionAlreadyResolvedError) as exc:
            _run(store.accept_suggestion(alice, s["id"]))
        assert exc.value.status == "dismissed"
        with pytest.raises(SuggestionAlreadyResolvedError):
            _run(store.dismiss_suggestion(alice, s["id"]))
        # Dismissing saved nothing.
        assert _run(env.memory_store.list_memories(alice)) == []

    def test_rows_follow_their_conversation(self, env):
        """FK cascade: the rows go with their conversation (account deletion
        cascades through users the same way)."""
        from sqlalchemy import delete
        store = env.store
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        _run(store.create_suggestion(alice, cid, "Fact"))

        async def _drop():
            async with env.session_local() as db:
                await db.execute(
                    delete(env.models.Conversation).where(env.models.Conversation.id == cid)
                )
                await db.commit()

        _run(_drop())
        assert _run(store.count_pending(alice, cid)) == 0

    def test_memory_delete_nulls_references(self, env):
        store = env.store
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        old = _run(env.memory_store.create_memory(alice, "Old"))
        s = _run(store.create_suggestion(alice, cid, "New", replaces_memory_id=old["id"]))
        assert _run(env.memory_store.delete_memory(alice, old["id"]))
        row = _run(store.get_suggestion(alice, s["id"]))
        assert row["replaces_memory_id"] is None and row["status"] == "pending"
        # Accepting still works: nothing left to archive.
        _suggestion, memory = _run(store.accept_suggestion(alice, s["id"]))
        assert memory["content"] == "New"


# ---------------------------------------------------------------------------
# Tool body (chat/memory_suggestions.py)
# ---------------------------------------------------------------------------


class TestRegisterSuggestion:
    def test_suggested_and_event(self, env):
        from chat.memory_suggestions import register_suggestion
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        result = _run(register_suggestion(
            alice, cid, "Prefers Python examples", reasoning="asked twice",
        ))
        assert result["status"] == "suggested"
        assert result["pending_count"] == 1
        assert "nothing is saved" in result["message"].lower()
        row = _run(env.store.get_suggestion(alice, result["suggestion_id"]))
        assert row["reasoning"] == "asked twice"
        assert env.published == [(alice, {
            "type": "memory_suggestions_changed",
            "conversation_id": cid,
            "pending_count": 1,
            "total_pending": 1,
        })]

    def test_duplicate_guards(self, env):
        from chat.memory_suggestions import register_suggestion
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        saved = _run(env.memory_store.create_memory(alice, "Prefers Python examples"))
        # Exact text of an active memory: nothing registered.
        result = _run(register_suggestion(alice, cid, " Prefers Python examples "))
        assert result == {
            "status": "already_saved",
            "memory_id": saved["id"],
            "message": "An active memory with exactly this text already exists.",
        }
        # An archived memory does not block a fresh suggestion.
        _run(env.memory_store.archive_memory(alice, saved["id"]))
        first = _run(register_suggestion(alice, cid, "Prefers Python examples"))
        assert first["status"] == "suggested"
        # Same text pending in the same conversation: points at the row.
        again = _run(register_suggestion(alice, cid, "Prefers Python examples"))
        assert again == {
            "status": "already_suggested",
            "suggestion_id": first["suggestion_id"],
            "pending_count": 1,
        }
        # ... but another conversation of the same user may suggest it too.
        other = str(uuid.uuid4())
        import db.conversation_store as conversation_store
        _run(conversation_store.create_conversation(alice, other, datetime.now(timezone.utc)))
        assert _run(register_suggestion(alice, other, "Prefers Python examples"))["status"] == "suggested"
        assert _run(env.store.count_pending(alice, cid)) == 1
        assert len(_events(env)) == 2

    def test_bad_input_and_cap(self, env):
        from chat.memory_suggestions import register_suggestion
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        assert "empty" in _run(register_suggestion(alice, cid, ""))["error"]
        assert "empty" in _run(register_suggestion(alice, cid, None))["error"]
        assert "replaces_memory_id" in _run(
            register_suggestion(alice, cid, "x", replaces_memory_id=5)
        )["error"]
        assert "not one of the user's active memories" in _run(
            register_suggestion(alice, cid, "x", replaces_memory_id="missing")
        )["error"]
        for i in range(MAX_PENDING_PER_CONVERSATION):
            assert _run(register_suggestion(alice, cid, f"fact {i}"))["status"] == "suggested"
        capped = _run(register_suggestion(alice, cid, "one more"))
        assert "pending memory suggestions" in capped["error"]
        assert _run(env.store.count_pending(alice, cid)) == MAX_PENDING_PER_CONVERSATION

    def test_accept_and_dismiss_publish(self, env):
        from chat.memory_suggestions import (
            accept_suggestion, dismiss_suggestion, register_suggestion,
        )
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        a = _run(register_suggestion(alice, cid, "A"))["suggestion_id"]
        b = _run(register_suggestion(alice, cid, "B"))["suggestion_id"]
        env.published.clear()
        _run(accept_suggestion(alice, a))
        _run(dismiss_suggestion(alice, b))
        assert [ev["pending_count"] for ev in _events(env)] == [1, 0]
        assert _run(accept_suggestion(_uid(env, "bob"), a)) is None
        assert len(_events(env)) == 2


# ---------------------------------------------------------------------------
# Dispatch backstop + prompt tiers
# ---------------------------------------------------------------------------


class TestDispatchGating:
    @pytest.mark.parametrize("flag", [
        "is_sub_agent", "is_user_subagent", "is_inference_api", "is_script",
    ])
    def test_non_top_level_runs_are_refused(self, flag):
        import json
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call
        result, attachments = _run(_dispatch_tool_call(
            app=None, provider=None, user={"id": 1, "email": "t@example.com"},
            conversation_id="c1", timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "suggest_memory", "arguments": {"content": "x"}},
            **{flag: True},
        ))
        assert attachments == []
        assert "top-level agent" in json.loads(result)["error"]

    def test_public_conversations_are_refused(self):
        import json
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST
        assert "suggest_memory" not in PUBLIC_TOOL_CALL_ALLOWLIST
        result, _ = _run(_dispatch_tool_call(
            app=None, provider=None, user={"id": 1, "email": "t@example.com"},
            conversation_id="c1", timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "suggest_memory", "arguments": {"content": "x"}},
            is_public=True,
        ))
        assert "public-project" in json.loads(result)["error"]

    def test_no_conversation_context(self):
        import json
        from chat.gemini_api.tool_handlers.memory import _handle_suggest_memory
        result = json.loads(_run(_handle_suggest_memory(1, None, "x")))
        assert "conversation context" in result["error"]


class TestPromptTiers:
    def test_top_level_offers_it_but_routines_do_not(self):
        from chat.gemini_api.system_prompt import get_system_prompt
        standard = get_system_prompt(API_KEY, base_url=BASE_URL)
        assert _bullet("suggest_memory") in standard
        assert 'tool_name="suggest_memory"' in standard
        assert "memory_search" in standard  # search-before-suggest guidance
        routine = get_system_prompt(API_KEY, base_url=BASE_URL, is_routine=True)
        assert _bullet("suggest_memory") not in routine

    def test_headless_tiers_hide_it(self):
        from chat.gemini_api.system_prompt import (
            get_inference_api_system_prompt,
            get_public_project_system_prompt,
            get_sub_agent_system_prompt,
            get_user_subagent_system_prompt,
        )
        prompts = [
            get_sub_agent_system_prompt("helper", API_KEY, base_url=BASE_URL),
            get_user_subagent_system_prompt(
                API_KEY, base_url=BASE_URL,
                target_user_email="t@example.com", caller_email="c@example.com",
            ),
            get_inference_api_system_prompt("", base_url=BASE_URL, user_email="u@example.com"),
            get_public_project_system_prompt(),
        ]
        for prompt in prompts:
            assert _bullet("suggest_memory") not in prompt

    def test_skill_describes_the_guidance(self):
        from chat.system_skills.catalog import _memory_content
        content = _memory_content(BASE_URL, API_KEY)
        assert "suggest_memory" in content
        assert "What NOT to suggest" in content
        assert "replaces_memory_id" in content


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _client(env, who="alice"):
    from chat import memory_routes

    app = FastAPI()
    app.include_router(memory_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[memory_routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


class TestRoutes:
    def test_list_scoped_to_owned_conversation(self, env):
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        s = _run(env.store.create_suggestion(alice, cid, "Fact", reasoning="why"))
        old = _run(env.memory_store.create_memory(alice, "Old fact"))
        r = _run(env.store.create_suggestion(
            alice, cid, "New fact", replaces_memory_id=old["id"],
        ))
        resp = _client(env).get(f"/app/api/conversations/{cid}/memory-suggestions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending_count"] == 2
        assert body["suggestions"][0]["id"] == s["id"]
        assert body["suggestions"][0]["reasoning"] == "why"
        assert body["suggestions"][0]["replaces_content"] is None
        assert body["suggestions"][1]["id"] == r["id"]
        assert body["suggestions"][1]["replaces_content"] == "Old fact"
        # Bob cannot even see that the conversation exists.
        resp = _client(env, "bob").get(f"/app/api/conversations/{cid}/memory-suggestions")
        assert resp.status_code == 404

    def test_accept_dismiss_and_conflicts(self, env):
        alice, cid = _uid(env, "alice"), env.conversations["alice"]
        a = _run(env.store.create_suggestion(alice, cid, "A"))
        b = _run(env.store.create_suggestion(alice, cid, "B"))
        client = _client(env)

        resp = client.post(f"/app/api/memory-suggestions/{a['id']}/accept", json={"content": "A edited"})
        assert resp.status_code == 200
        assert resp.json()["memory"]["content"] == "A edited"
        assert resp.json()["suggestion"]["status"] == "accepted"

        resp = client.post(f"/app/api/memory-suggestions/{a['id']}/accept")
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "suggestion_resolved"
        assert resp.json()["detail"]["status"] == "accepted"

        resp = client.post(f"/app/api/memory-suggestions/{b['id']}/accept", json={"content": "  "})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_memory"

        resp = _client(env, "bob").post(f"/app/api/memory-suggestions/{b['id']}/dismiss")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "suggestion_not_found"

        resp = client.post(f"/app/api/memory-suggestions/{b['id']}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["suggestion"]["status"] == "dismissed"
        resp = client.post(f"/app/api/memory-suggestions/{b['id']}/dismiss")
        assert resp.status_code == 409

        assert [m["content"] for m in _run(env.memory_store.list_memories(alice))] == ["A edited"]
        assert [ev["pending_count"] for ev in _events(env)] == [1, 0]
