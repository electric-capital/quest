"""Project memories: the project-scoped memory store, the non-blocking
``suggest_project_memory`` tool and its review path, the read tools (in
private and public projects alike), the prompt tiers and the REST routes (db/project_memory_store.py, db/project_memory_suggestion_store.py,
chat/project_memories.py, chat/project_memory_routes.py).
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.project_memory_suggestion_store import (
    MAX_PENDING_PER_CONVERSATION,
    SuggestionAlreadyResolvedError,
)

API_KEY = "test-api-key"
BASE_URL = "http://localhost:8000"

# The FTS5 table + triggers of migration d4f8b2a6c1e9 (create_all does not
# build virtual tables).
_FTS_DDL = [
    """CREATE VIRTUAL TABLE project_memories_fts USING fts5(
        content, content='project_memories', content_rowid='rowid')""",
    """CREATE TRIGGER project_memories_ai AFTER INSERT ON project_memories BEGIN
        INSERT INTO project_memories_fts(rowid, content) VALUES (NEW.rowid, NEW.content);
    END""",
    """CREATE TRIGGER project_memories_ad AFTER DELETE ON project_memories BEGIN
        INSERT INTO project_memories_fts(project_memories_fts, rowid, content)
        VALUES ('delete', OLD.rowid, OLD.content);
    END""",
    """CREATE TRIGGER project_memories_au AFTER UPDATE ON project_memories BEGIN
        INSERT INTO project_memories_fts(project_memories_fts, rowid, content)
        VALUES ('delete', OLD.rowid, OLD.content);
        INSERT INTO project_memories_fts(rowid, content) VALUES (NEW.rowid, NEW.content);
    END""",
]


def _run(coro):
    return asyncio.run(coro)


def _bullet(name: str) -> str:
    """How _build_dynamic_tools_section documents a callable tool."""
    return f"- **{name}** --"


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated DB: alice owns a private and a public project, each with one
    conversation; bob owns nothing. Events captured."""
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
    import db.models as models
    import db.project_memory_store as project_memory_store
    import db.project_memory_suggestion_store as suggestion_store
    import db.project_store as project_store

    models.Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        for ddl in _FTS_DDL:
            conn.execute(text(ddl))
    for mod in (
        conversation_store, memory_store, project_memory_store,
        suggestion_store, project_store,
    ):
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
    alice = users["alice"]["id"]
    project = _run(project_store.create_project(alice, "Launch"))
    public_project = _run(project_store.create_project(alice, "Open", public=True))
    cid = str(uuid.uuid4())
    _run(conversation_store.create_conversation(
        alice, cid, datetime.now(timezone.utc), project_id=project["id"],
    ))
    public_cid = str(uuid.uuid4())
    _run(conversation_store.create_conversation(
        alice, public_cid, datetime.now(timezone.utc), project_id=public_project["id"],
    ))

    class Env:
        pass

    e = Env()
    e.users = users
    e.project_id = project["id"]
    e.public_project_id = public_project["id"]
    e.cid = cid
    e.public_cid = public_cid
    e.published = published
    e.session_local = session_local
    e.models = models
    e.memories = project_memory_store
    e.user_memories = memory_store
    e.store = suggestion_store
    e.project_store = project_store
    return e


def _uid(env, who="alice"):
    return env.users[who]["id"]


def _events(env, type_name="project_memory_suggestions_changed"):
    return [ev for _uid_, ev in env.published if ev["type"] == type_name]


# ---------------------------------------------------------------------------
# Project memory store
# ---------------------------------------------------------------------------


class TestMemoryStore:
    def test_crud_search_and_scoping(self, env):
        m = env.memories
        pid = env.project_id
        a = _run(m.create_project_memory(pid, "Figures are reported in EUR"))
        b = _run(m.create_project_memory(pid, "Board meets on the first Monday"))
        other = _run(m.create_project_memory(env.public_project_id, "EUR elsewhere"))

        assert [x["id"] for x in _run(m.list_project_memories(pid))] == [b["id"], a["id"]]
        hits = _run(m.search_project_memories(pid, "EUR"))
        assert [h["id"] for h in hits] == [a["id"]]  # never the other project's
        assert _run(m.get_project_memory(pid, other["id"])) is None

        updated = _run(m.update_project_memory(pid, a["id"], "Figures are in EUR, net"))
        assert updated["content"] == "Figures are in EUR, net"
        assert _run(m.search_project_memories(pid, "net"))[0]["id"] == a["id"]

        _run(m.set_project_memory_archived(pid, a["id"], True))
        assert [x["id"] for x in _run(m.list_project_memories(pid))] == [b["id"]]
        assert len(_run(m.list_project_memories(pid, include_archived=True))) == 2
        assert _run(m.search_project_memories(pid, "EUR")) == []

        assert _run(m.delete_project_memory(env.public_project_id, a["id"])) is False
        assert _run(m.delete_project_memory(pid, a["id"])) is True
        with pytest.raises(ValueError):
            _run(m.create_project_memory(pid, "  "))
        with pytest.raises(ValueError):
            _run(m.create_project_memory(pid, "x" * 5000))

    def test_rows_follow_their_project(self, env):
        _run(env.memories.create_project_memory(env.project_id, "Fact"))
        _run(env.store.create_suggestion(_uid(env), env.project_id, env.cid, "Pending"))
        assert _run(env.project_store.delete_project(_uid(env), env.project_id))
        assert _run(env.memories.list_project_memories(env.project_id, True)) == []
        assert _run(env.store.count_pending(_uid(env), env.cid)) == 0

    def test_personal_memories_are_separate(self, env):
        _run(env.memories.create_project_memory(env.project_id, "Project fact"))
        assert _run(env.user_memories.list_memories(_uid(env))) == []


# ---------------------------------------------------------------------------
# Suggestion store + tool body
# ---------------------------------------------------------------------------


class TestSuggestions:
    def test_accept_creates_project_memory_and_archives_replaced(self, env):
        alice, pid, cid = _uid(env), env.project_id, env.cid
        old = _run(env.memories.create_project_memory(pid, "Figures in USD"))
        s = _run(env.store.create_suggestion(
            alice, pid, cid, "Figures in EUR", reasoning="said  so",
            replaces_memory_id=old["id"],
        ))
        [listed] = _run(env.store.list_pending(alice, cid))
        assert listed["replaces_content"] == "Figures in USD"
        assert listed["reasoning"] == "said so"

        suggestion, memory = _run(env.store.accept_suggestion(alice, s["id"]))
        assert suggestion["status"] == "accepted"
        assert suggestion["memory_id"] == memory["id"]
        assert memory["project_id"] == pid and memory["content"] == "Figures in EUR"
        assert _run(env.memories.get_project_memory(pid, old["id"]))["archived"] is True
        # Nothing landed in the user's personal memories.
        assert _run(env.user_memories.list_memories(alice)) == []

        with pytest.raises(SuggestionAlreadyResolvedError):
            _run(env.store.dismiss_suggestion(alice, s["id"]))
        assert _run(env.store.accept_suggestion(_uid(env, "bob"), s["id"])) is None

    def test_replacement_must_be_this_projects_memory(self, env):
        alice = _uid(env)
        elsewhere = _run(env.memories.create_project_memory(env.public_project_id, "X"))
        personal = _run(env.user_memories.create_memory(alice, "Personal"))
        for bad in (elsewhere["id"], personal["id"], "missing"):
            with pytest.raises(ValueError):
                _run(env.store.create_suggestion(
                    alice, env.project_id, env.cid, "New", replaces_memory_id=bad,
                ))

    def test_register_guards_and_event(self, env):
        from chat.project_memories import register_suggestion
        alice, pid, cid = _uid(env), env.project_id, env.cid
        saved = _run(env.memories.create_project_memory(pid, "Figures in EUR"))
        assert _run(register_suggestion(alice, pid, cid, " Figures in EUR "))[
            "status"] == "already_saved"
        # A personal memory with the same text does not block it.
        _run(env.user_memories.create_memory(alice, "Ships on Fridays"))
        first = _run(register_suggestion(alice, pid, cid, "Ships on Fridays", "why"))
        assert first["status"] == "suggested" and first["pending_count"] == 1
        again = _run(register_suggestion(alice, pid, cid, "Ships on Fridays"))
        assert again["status"] == "already_suggested"
        assert _events(env) == [{
            "type": "project_memory_suggestions_changed",
            "conversation_id": cid,
            "pending_count": 1,
            "total_pending": 1,
        }]
        # The personal-memory event is untouched.
        assert _events(env, "memory_suggestions_changed") == []
        assert saved["id"]

        for i in range(MAX_PENDING_PER_CONVERSATION - 1):
            assert _run(register_suggestion(alice, pid, cid, f"fact {i}"))["status"] == "suggested"
        capped = _run(register_suggestion(alice, pid, cid, "one more"))
        assert "pending project memory suggestions" in capped["error"]

    def test_accept_and_dismiss_publish(self, env):
        from chat.project_memories import (
            accept_suggestion, dismiss_suggestion, register_suggestion,
        )
        alice, pid, cid = _uid(env), env.project_id, env.cid
        a = _run(register_suggestion(alice, pid, cid, "A"))["suggestion_id"]
        b = _run(register_suggestion(alice, pid, cid, "B"))["suggestion_id"]
        env.published.clear()
        _run(accept_suggestion(alice, a, "A edited"))
        _run(dismiss_suggestion(alice, b))
        assert [ev["pending_count"] for ev in _events(env)] == [1, 0]
        assert [m["content"] for m in _run(env.memories.list_project_memories(pid))] == ["A edited"]


# ---------------------------------------------------------------------------
# Tools: dispatch gating + handlers
# ---------------------------------------------------------------------------


def _dispatch(env, tool, arguments, conversation_id=None, **kwargs):
    from chat.gemini_api.tool_dispatch import _dispatch_tool_call
    result, _ = _run(_dispatch_tool_call(
        app=None, provider=None, user=env.users["alice"],
        conversation_id=conversation_id or env.cid, timezone="UTC",
        tool_name="tool_call",
        args={"tool_name": tool, "arguments": arguments},
        **kwargs,
    ))
    return json.loads(result)


class TestTools:
    def test_read_tools_and_suggest_in_a_project(self, env):
        pid = env.project_id
        _run(env.memories.create_project_memory(pid, "Figures are reported in EUR"))
        listed = _dispatch(env, "project_memory_list", {}, project_id=pid)
        assert listed["memory_count"] == 1
        found = _dispatch(env, "project_memory_search", {"query": "EUR"}, project_id=pid)
        assert found["project_memories"][0]["content"] == "Figures are reported in EUR"

        result = _dispatch(
            env, "suggest_project_memory",
            {"content": "Board meets monthly", "reasoning": "Mentioned twice."},
            project_id=pid,
        )
        assert result["status"] == "suggested"
        [row] = _run(env.store.list_pending(_uid(env), env.cid))
        assert row["project_id"] == pid

    def test_no_project(self, env):
        for tool in (
            "project_memory_list", "project_memory_search", "project_memory_get",
            "suggest_project_memory",
        ):
            result = _dispatch(env, tool, {"query": "x", "content": "x", "ids": ["x"]})
            assert "project conversations" in result["error"]

    @pytest.mark.parametrize("flag", [
        "is_sub_agent", "is_user_subagent", "is_inference_api", "is_script",
    ])
    def test_suggest_is_top_level_only(self, env, flag):
        result = _dispatch(
            env, "suggest_project_memory", {"content": "x"},
            project_id=env.project_id, **{flag: True},
        )
        assert "top-level agent" in result["error"]

    def test_sub_agents_may_read(self, env):
        result = _dispatch(
            env, "project_memory_list", {}, project_id=env.project_id, is_sub_agent=True,
        )
        assert result["memory_count"] == 0
        result = _dispatch(
            env, "project_memory_get", {"ids": ["nope"]},
            project_id=env.project_id, is_sub_agent=True,
        )
        assert result == {"project_memories": [], "missing": ["nope"]}

    def test_public_conversations_use_their_own_project(self, env):
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST
        assert {
            "project_memory_search", "project_memory_list", "project_memory_get",
            "suggest_project_memory",
        } <= PUBLIC_TOOL_CALL_ALLOWLIST
        pub = env.public_project_id
        _run(env.memories.create_project_memory(pub, "Public fact about EUR"))
        _run(env.memories.create_project_memory(env.project_id, "Private fact about EUR"))
        kw = dict(project_id=pub, is_public=True, conversation_id=env.public_cid)

        found = _dispatch(env, "project_memory_search", {"query": "EUR"}, **kw)
        assert [m["content"] for m in found["project_memories"]] == ["Public fact about EUR"]
        result = _dispatch(env, "suggest_project_memory", {"content": "Uses ISO dates"}, **kw)
        assert result["status"] == "suggested"
        [row] = _run(env.store.list_pending(_uid(env), env.public_cid))
        assert row["project_id"] == pub
        # Personal memories stay out of reach.
        assert "public-project" in _dispatch(env, "memory_search", {"query": "x"}, **kw)["error"]

    def test_unknown_tool_listing_hides_them_outside_projects(self, env):
        result = _dispatch(env, "nope", {})
        assert "project_memory_search" not in result["error"]
        result = _dispatch(env, "nope", {}, project_id=env.project_id)
        assert "project_memory_search" in result["error"]


class TestGetTools:
    """memory_get and project_memory_get: the ids the prompt's memory
    index shows, fetched in full (chat/gemini_api/tool_handlers/memory.py)."""

    def test_project_memory_get_by_id(self, env):
        pid = env.project_id
        a = _run(env.memories.create_project_memory(pid, "Figures are reported in EUR"))
        b = _run(env.memories.create_project_memory(pid, "Board meets monthly"))
        archived = _run(env.memories.create_project_memory(pid, "Old convention"))
        _run(env.memories.set_project_memory_archived(pid, archived["id"], True))
        other = _run(env.memories.create_project_memory(env.public_project_id, "Elsewhere"))

        result = _dispatch(
            env, "project_memory_get",
            {"ids": [b["id"], "nope", a["id"], other["id"], archived["id"], b["id"]]},
            project_id=pid,
        )
        # In the order asked, de-duplicated; another project's id is missing.
        assert [m["content"] for m in result["project_memories"]] == [
            "Board meets monthly", "Figures are reported in EUR", "Old convention",
        ]
        assert [m["archived"] for m in result["project_memories"]] == [False, False, True]
        assert set(result["project_memories"][0]) == {
            "id", "content", "created_at", "updated_at", "archived",
        }
        assert result["missing"] == ["nope", other["id"]]

    def test_memory_get_by_id(self, env):
        import db.memory_store as memory_store
        alice = _uid(env)
        bob = env.users["bob"]["id"]
        mine = _run(memory_store.create_memory(alice, "Prefers metric units"))
        theirs = _run(memory_store.create_memory(bob, "Bob's secret"))
        result = _dispatch(env, "memory_get", {"ids": [mine["id"], theirs["id"]]})
        assert [m["content"] for m in result["memories"]] == ["Prefers metric units"]
        assert result["memories"][0]["archived"] is False
        assert result["missing"] == [theirs["id"]]
        # A single id string is accepted.
        result = _dispatch(env, "memory_get", {"ids": mine["id"]})
        assert [m["id"] for m in result["memories"]] == [mine["id"]]

    @pytest.mark.parametrize("ids,message", [
        (None, "non-empty list"),
        ([], "non-empty list"),
        ("", "id strings only"),
        ([1], "id strings only"),
        (["a", "  "], "id strings only"),
        ([f"id{i}" for i in range(21)], "At most 20 ids"),
    ])
    def test_ids_validation(self, env, ids, message):
        for tool, kw in (("memory_get", {}), ("project_memory_get", {"project_id": env.project_id})):
            result = _dispatch(env, tool, {"ids": ids}, **kw)
            assert message in result["error"], (tool, ids)

    def test_public_conversation_gets_its_own_project_only(self, env):
        pub = env.public_project_id
        mine = _run(env.memories.create_project_memory(pub, "Public fact"))
        private = _run(env.memories.create_project_memory(env.project_id, "Private fact"))
        kw = dict(project_id=pub, is_public=True, conversation_id=env.public_cid)
        result = _dispatch(env, "project_memory_get", {"ids": [mine["id"], private["id"]]}, **kw)
        assert [m["content"] for m in result["project_memories"]] == ["Public fact"]
        assert result["missing"] == [private["id"]]
        assert "public-project" in _dispatch(env, "memory_get", {"ids": ["x"]}, **kw)["error"]



# ---------------------------------------------------------------------------
# Prompt tiers
# ---------------------------------------------------------------------------


class TestPromptTiers:
    def test_project_top_level_offers_and_explains_them(self):
        from chat.gemini_api.system_prompt import get_system_prompt
        prompt = get_system_prompt(API_KEY, base_url=BASE_URL, has_project=True)
        for tool in ("project_memory_search", "project_memory_list", "suggest_project_memory"):
            assert _bullet(tool) in prompt
        assert "**Project Memories:**" in prompt
        assert "without being asked" in prompt
        assert "suggested project memories" in prompt
        assert "create_project_memory" not in prompt
        # Personal suggestions stay offered beside them.
        assert _bullet("suggest_memory") in prompt

    def test_standalone_has_none(self):
        from chat.gemini_api.system_prompt import get_system_prompt
        prompt = get_system_prompt(API_KEY, base_url=BASE_URL)
        for tool in ("project_memory_search", "project_memory_list", "suggest_project_memory"):
            assert _bullet(tool) not in prompt
        assert "Project Memories" not in prompt

    def test_routine_reads_but_does_not_suggest(self):
        from chat.gemini_api.system_prompt import get_system_prompt
        prompt = get_system_prompt(
            API_KEY, base_url=BASE_URL, has_project=True, is_routine=True,
        )
        assert _bullet("project_memory_search") in prompt
        assert _bullet("suggest_project_memory") not in prompt
        assert "**Project Memories:**" in prompt
        assert "without being asked" not in prompt

    def test_other_tiers(self):
        from chat.gemini_api.system_prompt import (
            get_inference_api_system_prompt,
            get_public_project_system_prompt,
            get_sub_agent_system_prompt,
            get_user_subagent_system_prompt,
        )
        sub = get_sub_agent_system_prompt(
            "helper", API_KEY, base_url=BASE_URL, has_project=True,
        )
        assert _bullet("project_memory_search") in sub
        assert _bullet("suggest_project_memory") not in sub
        standalone_sub = get_sub_agent_system_prompt("helper", API_KEY, base_url=BASE_URL)
        assert _bullet("project_memory_search") not in standalone_sub
        for prompt in (
            get_user_subagent_system_prompt(
                API_KEY, base_url=BASE_URL,
                target_user_email="t@example.com", caller_email="c@example.com",
            ),
            get_inference_api_system_prompt("", base_url=BASE_URL, user_email="u@example.com"),
        ):
            for tool in ("project_memory_search", "suggest_project_memory"):
                assert _bullet(tool) not in prompt

    def test_public_projects_get_them_without_personal_memories(self):
        from chat.gemini_api.system_prompt import get_public_project_system_prompt
        prompt = get_public_project_system_prompt()
        for tool in ("project_memory_search", "project_memory_list", "suggest_project_memory"):
            assert _bullet(tool) in prompt
        assert "**Project Memories:**" in prompt
        assert "without being asked" in prompt
        assert "personal memories (`suggest_memory`" not in prompt
        assert _bullet("suggest_memory") not in prompt
        routine = get_public_project_system_prompt(is_routine=True)
        assert _bullet("project_memory_search") in routine
        assert _bullet("suggest_project_memory") not in routine

    def test_skill_describes_project_memories(self):
        from chat.system_skills.catalog import _memory_content
        content = _memory_content(BASE_URL, API_KEY)
        assert "suggest_project_memory" in content
        assert "no action-request card" in content
        assert "Which store?" in content

    def test_card_is_gone(self):
        from chat.action_request_types import get_handler
        from chat.llm.tool_schemas import ACTION_REQUEST_TYPE_ENUM
        assert get_handler("create_project_memory") is None
        assert "create_project_memory" not in ACTION_REQUEST_TYPE_ENUM


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _client(env, who="alice"):
    from chat import project_memory_routes

    app = FastAPI()
    app.include_router(project_memory_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[
        project_memory_routes.get_current_user_cookie_or_apikey_checked
    ] = _current
    return TestClient(app)


class TestRoutes:
    def test_memory_crud(self, env):
        client = _client(env)
        base = f"/app/api/projects/{env.project_id}/memories"
        resp = client.post(base, json={"content": "Figures in EUR"})
        assert resp.status_code == 201
        mid = resp.json()["id"]
        assert resp.json()["project_id"] == env.project_id

        assert [m["id"] for m in client.get(base).json()["memories"]] == [mid]
        assert client.get(base, params={"q": "EUR"}).json()["memories"][0]["id"] == mid

        resp = client.put(f"{base}/{mid}", json={"content": "Figures in EUR, net"})
        assert resp.json()["content"] == "Figures in EUR, net"
        assert client.put(f"{base}/{mid}/archive").json()["archived"] is True
        assert client.get(base).json()["memories"] == []
        assert len(client.get(base, params={"include_archived": True}).json()["memories"]) == 1
        assert client.put(f"{base}/{mid}/unarchive").json()["archived"] is False

        assert client.post(base, json={"content": "  "}).status_code == 422
        assert client.put(f"{base}/missing", json={"content": "x"}).status_code == 404
        assert client.delete(f"{base}/{mid}").json() == {"success": True}
        assert client.delete(f"{base}/{mid}").status_code == 404

    def test_ownership_and_public_projects(self, env):
        base = f"/app/api/projects/{env.project_id}/memories"
        assert _client(env, "bob").get(base).status_code == 404
        assert _client(env, "bob").post(base, json={"content": "x"}).status_code == 404
        public = f"/app/api/projects/{env.public_project_id}/memories"
        assert _client(env).post(public, json={"content": "Public fact"}).status_code == 201
        assert [m["content"] for m in _client(env).get(public).json()["memories"]] == ["Public fact"]

    def test_suggestion_routes(self, env):
        alice, pid, cid = _uid(env), env.project_id, env.cid
        a = _run(env.store.create_suggestion(alice, pid, cid, "A", reasoning="why"))
        b = _run(env.store.create_suggestion(alice, pid, cid, "B"))
        client = _client(env)

        body = client.get(f"/app/api/conversations/{cid}/project-memory-suggestions").json()
        assert body["pending_count"] == 2
        assert body["suggestions"][0]["reasoning"] == "why"
        assert _client(env, "bob").get(
            f"/app/api/conversations/{cid}/project-memory-suggestions",
        ).status_code == 404

        resp = client.post(
            f"/app/api/project-memory-suggestions/{a['id']}/accept",
            json={"content": "A edited"},
        )
        assert resp.status_code == 200
        assert resp.json()["memory"]["project_id"] == pid
        resp = client.post(f"/app/api/project-memory-suggestions/{a['id']}/accept")
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "suggestion_resolved"

        assert _client(env, "bob").post(
            f"/app/api/project-memory-suggestions/{b['id']}/dismiss",
        ).status_code == 404
        resp = client.post(f"/app/api/project-memory-suggestions/{b['id']}/dismiss")
        assert resp.json()["suggestion"]["status"] == "dismissed"

        assert [m["content"] for m in _run(env.memories.list_project_memories(pid))] == ["A edited"]
        assert [ev["pending_count"] for ev in _events(env)] == [1, 0]
