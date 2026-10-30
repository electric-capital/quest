"""Dreaming (chat/dreaming.py, config/dreaming.py, db/dream_run_store.py,
the admin routes in chat/routes/dreaming.py) and the memory-suggestion
changes it rides on (archive-only rows, rows without a conversation).

Covers the settings store, the gate registration, the pacing rules of
``skip_reason``, the model choice, the prompt, the answer parsing and
validation, a whole dream against a fake provider (ledger row,
suggestions, event), the archive-only accept path in the store, the
inbox listing of dream rows, and the admin endpoints.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat import dreaming
from db.models import DreamRunStatus, MemorySuggestionSource


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(isolated_db, monkeypatch, tmp_path):
    """Users alice (admin) and bob, one conversation each, events captured,
    the dreaming settings and feature gates in tmp files, the daemon's
    per-process state cleared."""
    import db.conversation_store as conversation_store
    import db.dream_run_store as run_store
    import db.memory_store as memory_store
    import db.memory_suggestion_store as suggestion_store
    import config.dreaming as dreaming_config
    import config.feature_gates as fg

    models = isolated_db.models
    monkeypatch.setattr(dreaming_config, "DREAMING_SETTINGS_FILE", tmp_path / "dreaming.json")
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
    monkeypatch.setattr(dreaming, "_dreaming_users", set())
    monkeypatch.setattr(dreaming, "_dream_lock", asyncio.Lock())

    published: list[tuple[int, dict]] = []
    from chat.realtime import bus
    monkeypatch.setattr(
        bus, "publish_to_user", lambda user_id, ev: published.append((user_id, ev)),
    )

    async def _seed():
        async with isolated_db.AsyncSessionLocal() as db:
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
        _run(conversation_store.create_conversation(user["id"], cid, datetime.now(timezone.utc)))
        conversations[name] = cid

    e = SimpleNamespace()
    e.users = users
    e.conversations = conversations
    e.published = published
    e.models = models
    e.memory_store = memory_store
    e.store = suggestion_store
    e.run_store = run_store
    e.session_local = isolated_db.AsyncSessionLocal
    return e


def _uid(env, who):
    return env.users[who]["id"]


def _memories(env, who, *texts):
    return [_run(env.memory_store.create_memory(_uid(env, who), t)) for t in texts]


def _events(env):
    return [ev for _u, ev in env.published if ev["type"] == "memory_suggestions_changed"]


class FakeProvider:
    """Stands in for an LLMProvider: records the prompts, answers a canned text."""

    def __init__(self, answer: str):
        self.answer = answer
        self.calls: list[dict] = []

    def create_session(self, model, system_prompt, tools, history=None, thinking_effort=None):
        session = {
            "model": model, "system": system_prompt, "tools": tools,
            "effort": thinking_effort, "cache_writes": True,
        }
        self.calls.append(session)
        return session

    def disable_cache_writes(self, session):
        session["cache_writes"] = False

    async def send_message_stream(self, session, message):
        session["message"] = message
        # Split in two to prove chunks are joined.
        half = len(self.answer) // 2
        yield SimpleNamespace(type="text", text=self.answer[:half])
        yield SimpleNamespace(type="usage", text="")
        yield SimpleNamespace(type="text", text=self.answer[half:])

    def get_usage(self, session):
        from chat.llm.base import UsageStats
        return UsageStats(
            input_tokens=1200, output_tokens=150, cached_tokens=0,
            raw_usage={"input_tokens": 1200, "output_tokens": 150},
        )


def _patch_model(monkeypatch, provider, model="claude-opus-5-5", effort="high"):
    """Make the dream run on ``provider`` as ``model`` without touching the registry."""
    monkeypatch.setattr(dreaming, "resolve_dream_model", lambda settings: (model, effort, True))
    import chat.llm.config as llm_config
    monkeypatch.setattr(llm_config, "get_provider_for_model", lambda m: "anthropic")
    monkeypatch.setattr(llm_config, "get_provider_for_model_instance", lambda m: provider)


# ---------------------------------------------------------------------------
# Settings store and gate
# ---------------------------------------------------------------------------


class TestSettings:
    def test_defaults_round_trip_and_normalization(self, env):
        from config import dreaming as cfg

        assert cfg.read_dreaming_settings() == {"model": None, "thinking_effort": None}
        stored = cfg.save_dreaming_settings({"model": "  claude-opus-5-5 ", "thinking_effort": " HIGH "})
        assert stored == {"model": "claude-opus-5-5", "thinking_effort": "high"}
        assert cfg.read_dreaming_settings() == stored
        assert cfg.save_dreaming_settings({"model": "", "thinking_effort": 7}) == cfg.DEFAULT_SETTINGS
        assert cfg.normalize_settings("junk") == cfg.DEFAULT_SETTINGS

    def test_malformed_file_reads_as_defaults(self, env):
        from config import dreaming as cfg

        cfg.DREAMING_SETTINGS_FILE.write_text("{not json")
        assert cfg.read_dreaming_settings() == cfg.DEFAULT_SETTINGS

    def test_gate_registered_per_user(self, env):
        import config.feature_gates as fg

        assert fg.FEATURE_DREAMING in fg.KNOWN_FEATURES
        assert fg.FEATURE_DREAMING in fg.PER_USER_ACCESS_FEATURES
        assert fg.FEATURE_LABELS[fg.FEATURE_DREAMING]["label"] == "Dreaming"
        assert not fg.dreaming_enabled_for("alice@example.com")
        fg.set_feature_enabled(fg.FEATURE_DREAMING, True)
        fg.set_feature_allowed_users(fg.FEATURE_DREAMING, ["alice@example.com"])
        assert fg.dreaming_enabled_for("alice@example.com")
        assert not fg.dreaming_enabled_for("bob@example.com")


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


class TestSkipReason:
    def test_too_few_memories(self, env):
        alice = _uid(env, "alice")
        assert _run(dreaming.skip_reason(alice, scheduled=False)) == "too_few_memories"
        _memories(env, "alice", "one")
        assert _run(dreaming.skip_reason(alice, scheduled=False)) == "too_few_memories"
        _memories(env, "alice", "two")
        assert _run(dreaming.skip_reason(alice, scheduled=False)) is None

    def test_first_scheduled_dream_waits_for_a_store(self, env):
        alice = _uid(env, "alice")
        _memories(env, "alice", *[f"fact {i}" for i in range(dreaming.MIN_MEMORIES_FIRST_DREAM - 1)])
        assert _run(dreaming.skip_reason(alice, scheduled=True)) == "too_few_new_memories"
        assert _run(dreaming.skip_reason(alice, scheduled=False)) is None
        _memories(env, "alice", "one more")
        assert _run(dreaming.skip_reason(alice, scheduled=True)) is None

    def test_spacing_and_new_memories_since_last_run(self, env):
        alice = _uid(env, "alice")
        _memories(env, "alice", *[f"fact {i}" for i in range(6)])
        run = _run(env.run_store.create_run(alice, "scheduled"))
        _run(env.run_store.finish_run(run["id"], DreamRunStatus.COMPLETED))
        # Just ran: too soon, whatever the store did.
        assert _run(dreaming.skip_reason(alice, scheduled=True)) == "too_soon"
        assert _run(dreaming.skip_reason(alice, scheduled=False)) is None

        # Age the run past the spacing (and the memories it read past it):
        # now it is about memories created since.
        async def _age():
            async with env.session_local() as db:
                row = await db.get(env.models.DreamRun, run["id"])
                row.started_at = datetime.now(timezone.utc) - timedelta(hours=30)
                await db.execute(
                    env.models.Memory.__table__.update().values(
                        created_at=datetime.now(timezone.utc) - timedelta(hours=40),
                    ),
                )
                await db.commit()
        _run(_age())
        assert _run(dreaming.skip_reason(alice, scheduled=True)) == "too_few_new_memories"
        _memories(env, "alice", *[f"new {i}" for i in range(dreaming.MIN_NEW_MEMORIES)])
        assert _run(dreaming.skip_reason(alice, scheduled=True)) is None

    def test_failed_run_also_paces(self, env):
        alice = _uid(env, "alice")
        _memories(env, "alice", *[f"fact {i}" for i in range(6)])
        run = _run(env.run_store.create_run(alice, "scheduled"))
        _run(env.run_store.finish_run(run["id"], DreamRunStatus.FAILED, error="boom"))
        assert _run(dreaming.skip_reason(alice, scheduled=True)) == "too_soon"

    def test_running_and_stale_running(self, env):
        alice = _uid(env, "alice")
        _memories(env, "alice", "a", "b")
        run = _run(env.run_store.create_run(alice, "manual"))
        assert _run(dreaming.skip_reason(alice, scheduled=False)) == "running"

        async def _age():
            async with env.session_local() as db:
                row = await db.get(env.models.DreamRun, run["id"])
                row.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
                await db.commit()
        _run(_age())
        assert _run(dreaming.skip_reason(alice, scheduled=False)) is None
        dreaming._dreaming_users.add(alice)
        assert _run(dreaming.skip_reason(alice, scheduled=False)) == "running"

    def test_pending_dream_suggestions_block(self, env):
        alice = _uid(env, "alice")
        a, _b = _memories(env, "alice", "a", "b")
        _run(env.store.create_suggestion(
            alice, None, None, replaces_memory_id=a["id"], source=MemorySuggestionSource.DREAM,
        ))
        assert _run(dreaming.skip_reason(alice, scheduled=False)) == "pending_review"
        # A pending suggestion from a chat does not count.
        _run(env.store.create_suggestion(_uid(env, "bob"), env.conversations["bob"], "x"))
        _memories(env, "bob", "a", "b")
        assert _run(dreaming.skip_reason(_uid(env, "bob"), scheduled=False)) is None


# ---------------------------------------------------------------------------
# Model choice
# ---------------------------------------------------------------------------


class TestResolveDreamModel:
    def _candidates(self, monkeypatch, ids, efforts=("low", "high")):
        import chat.llm.config as llm_config
        from config import model_selection

        monkeypatch.setattr(llm_config, "get_available_models", lambda: list(ids))
        monkeypatch.setattr(
            model_selection, "is_model_allowed",
            lambda m, *, public: not m.startswith("public-only"),
        )
        monkeypatch.setattr(
            llm_config, "resolve_model",
            lambda m: SimpleNamespace(id=m, thinking_efforts=tuple(efforts)),
        )

    def test_automatic_prefers_the_list_then_anything(self, monkeypatch):
        self._candidates(monkeypatch, ["gemini-x", "claude-sonnet-5-5", "claude-opus-5-5"])
        assert dreaming.resolve_dream_model({"model": None, "thinking_effort": None}) == (
            "claude-opus-5-5", None, True,
        )
        self._candidates(monkeypatch, ["gemini-x", "other"])
        assert dreaming.resolve_dream_model({"model": None, "thinking_effort": "high"}) == (
            "gemini-x", "high", True,
        )

    def test_pinned_wins_when_dreamable_else_falls_back(self, monkeypatch):
        self._candidates(monkeypatch, ["claude-opus-5-5", "claude-sonnet-5-5"])
        assert dreaming.resolve_dream_model({"model": "claude-sonnet-5-5", "thinking_effort": "max"}) == (
            "claude-sonnet-5-5", None, False,  # "max" not listed -> default level
        )
        assert dreaming.resolve_dream_model({"model": "gone-model", "thinking_effort": "low"}) == (
            "claude-opus-5-5", "low", True,
        )

    def test_private_only_and_nothing_available(self, monkeypatch):
        self._candidates(monkeypatch, ["public-only-model"])
        with pytest.raises(dreaming.DreamingUnavailable):
            dreaming.resolve_dream_model({"model": "public-only-model", "thinking_effort": None})
        assert dreaming.dreamable_models() == []


# ---------------------------------------------------------------------------
# Prompt, parsing, validation
# ---------------------------------------------------------------------------


def _mem(id_, content, created="2026-10-01T10:00:00"):
    return {"id": id_, "content": content, "created_at": created}


class TestPromptAndParsing:
    def test_prompts_carry_memories_date_and_budget(self):
        memories = [_mem("m1", "Prefers tea"), _mem("m2", "Likes rain", "2026-09-01T00:00:00")]
        system, user = dreaming.build_dream_prompts(memories, today=datetime(2026, 10, 29))
        assert str(dreaming.MAX_PROPOSALS_PER_DREAM) in system
        assert '"kind": "replace" | "archive"' in system
        assert "Today is 2026-10-29" in user
        assert '<memory id="m1" created="2026-10-01">\nPrefers tea\n</memory>' in user
        assert '<memory id="m2" created="2026-09-01">' in user
        assert "2 active memories" in user
        assert "one-line index" not in user

    def test_over_budget_store_is_flagged(self):
        from chat.memory_prompt import PERSONAL_MEMORY_BUDGET

        memories = [_mem(f"m{i}", f"fact {i}") for i in range(PERSONAL_MEMORY_BUDGET.full_count + 1)]
        _system, user = dreaming.build_dream_prompts(memories)
        assert "one-line index" in user

    def test_parse_tolerates_fences_and_prose(self):
        body = {"proposals": [{"kind": "archive", "memory_id": "m1"}]}
        assert dreaming.parse_proposals(json.dumps(body)) == body["proposals"]
        assert dreaming.parse_proposals(f"```json\n{json.dumps(body)}\n```") == body["proposals"]
        assert dreaming.parse_proposals(f"Here you go:\n{json.dumps(body)}\nDone.") == body["proposals"]
        assert dreaming.parse_proposals('{"proposals": []}') == []

    @pytest.mark.parametrize("text", ["", "no json here", "{not json", '{"nope": 1}', '{"proposals": 3}'])
    def test_parse_rejects_junk(self, text):
        with pytest.raises(dreaming.DreamError):
            dreaming.parse_proposals(text)

    def test_validate_keeps_the_usable_and_explains_the_rest(self):
        memories = [_mem("m1", "Prefers tea"), _mem("m2", "Prefers green tea"), _mem("m3", "Likes rain")]
        proposals = [
            {"kind": "replace", "memory_id": "m1", "content": " Prefers green tea (merged) ", "reasoning": "dup"},
            {"kind": "archive", "memory_id": "m2", "reasoning": "merged into m1"},
            {"kind": "archive", "memory_id": "m2"},                       # twice
            {"kind": "replace", "memory_id": "m1", "content": "x"},       # twice
            {"kind": "replace", "memory_id": "zz", "content": "x"},       # unknown
            {"kind": "delete", "memory_id": "m1"},                        # unknown kind
            {"kind": "replace", "memory_id": "m1", "content": "   "},     # empty
            {"kind": "replace", "memory_id": "m3", "content": " Likes rain "},  # unchanged
            "junk",
        ]
        kept, rejected = dreaming.validate_proposals(proposals, memories)
        assert kept == [
            {"kind": "replace", "memory_id": "m1", "content": "Prefers green tea (merged)", "reasoning": "dup"},
            {"kind": "archive", "memory_id": "m2", "content": None, "reasoning": "merged into m1"},
        ]
        assert len(rejected) == 7
        assert any("twice" in r for r in rejected)
        assert any("unknown kind" in r for r in rejected)
        assert any("equals the current text" in r for r in rejected)

    def test_validate_caps_the_count(self):
        memories = [_mem(f"m{i}", f"fact {i}") for i in range(dreaming.MAX_PROPOSALS_PER_DREAM + 3)]
        proposals = [{"kind": "archive", "memory_id": m["id"]} for m in memories]
        kept, rejected = dreaming.validate_proposals(proposals, memories)
        assert len(kept) == dreaming.MAX_PROPOSALS_PER_DREAM
        assert len(rejected) == 3 and all("cap" in r for r in rejected)


# ---------------------------------------------------------------------------
# A whole dream
# ---------------------------------------------------------------------------


class TestRunDream:
    def test_dream_registers_suggestions_and_ledger(self, env, monkeypatch):
        alice = _uid(env, "alice")
        tea, green, rain = _memories(env, "alice", "Prefers tea", "Prefers green tea", "Hates rain")
        answer = json.dumps({"proposals": [
            {"kind": "replace", "memory_id": green["id"], "content": "Prefers green tea", "reasoning": "same"},
            {"kind": "replace", "memory_id": tea["id"], "content": "Prefers green tea", "reasoning": "merge"},
            {"kind": "archive", "memory_id": green["id"], "reasoning": "merged"},
            {"kind": "archive", "memory_id": rain["id"], "reasoning": "stale"},
        ]})
        provider = FakeProvider(answer)
        _patch_model(monkeypatch, provider)

        run = _run(dreaming.run_dream(env.users["alice"], dreaming.TRIGGER_MANUAL))

        assert run["status"] == DreamRunStatus.COMPLETED
        assert run["trigger"] == "manual" and run["model"] == "claude-opus-5-5"
        assert run["thinking_effort"] == "high"
        assert run["memories_read"] == 3
        # The unchanged replace was dropped; the merge pair and the archive stayed.
        assert run["suggestions_created"] == 3
        assert run["input_tokens"] == 1200 and run["output_tokens"] == 150
        assert run["estimated_cost_usd"] is not None and run["estimated_cost_usd"] > 0
        assert run["finished_at"] and run["error"] is None

        session = provider.calls[0]
        assert session["model"] == "claude-opus-5-5" and session["effort"] == "high"
        assert session["tools"] == [] and session["cache_writes"] is False
        assert "hygiene, not invention" in session["system"]
        assert f'id="{tea["id"]}"' in session["message"]

        pending = _run(env.store.list_pending_for_user(alice))
        assert len(pending) == 3
        assert all(s["source"] == "dream" and s["conversation_id"] is None for s in pending)
        assert all(s["dream_run_id"] == run["id"] for s in pending)
        by_memory = {s["replaces_memory_id"]: s for s in pending}
        assert by_memory[tea["id"]]["content"] == "Prefers green tea"
        assert by_memory[tea["id"]]["replaces_content"] == "Prefers tea"
        assert by_memory[green["id"]]["content"] is None
        assert by_memory[rain["id"]]["content"] is None
        assert by_memory[rain["id"]]["reasoning"] == "stale"

        # One event for the whole batch: no conversation, the user's total.
        assert _events(env) == [{
            "type": "memory_suggestions_changed", "conversation_id": None,
            "pending_count": 0, "total_pending": 3,
        }]
        # Nothing was changed in the store itself.
        assert all(not m["archived"] for m in _run(env.memory_store.list_memories(alice)))
        # And the user is now paced until they review.
        assert _run(dreaming.skip_reason(alice, scheduled=False)) == "pending_review"

    def test_empty_answer_is_a_quiet_success(self, env, monkeypatch):
        _memories(env, "alice", "a", "b")
        _patch_model(monkeypatch, FakeProvider('{"proposals": []}'))
        run = _run(dreaming.run_dream(env.users["alice"], dreaming.TRIGGER_SCHEDULED))
        assert run["status"] == DreamRunStatus.COMPLETED
        assert run["suggestions_created"] == 0
        assert _events(env) == []

    def test_unparseable_answer_fails_the_run(self, env, monkeypatch):
        _memories(env, "alice", "a", "b")
        _patch_model(monkeypatch, FakeProvider("I would rather not."))
        run = _run(dreaming.run_dream(env.users["alice"], dreaming.TRIGGER_SCHEDULED))
        assert run["status"] == DreamRunStatus.FAILED
        assert "no JSON object" in run["error"]
        assert run["input_tokens"] == 1200  # usage recorded even so
        assert _run(env.store.count_pending_for_user(_uid(env, "alice"))) == 0

    def test_provider_error_fails_the_run(self, env, monkeypatch):
        _memories(env, "alice", "a", "b")

        class Exploding(FakeProvider):
            async def send_message_stream(self, session, message):
                raise RuntimeError("quota exhausted")
                yield  # pragma: no cover

        _patch_model(monkeypatch, Exploding(""))
        run = _run(dreaming.run_dream(env.users["alice"], dreaming.TRIGGER_SCHEDULED))
        assert run["status"] == DreamRunStatus.FAILED
        assert run["error"] == "quota exhausted"
        assert run["model"] == "claude-opus-5-5"

    def test_no_model_fails_the_run(self, env, monkeypatch):
        _memories(env, "alice", "a", "b")

        def _none(settings):
            raise dreaming.DreamingUnavailable("nothing configured")
        monkeypatch.setattr(dreaming, "resolve_dream_model", _none)
        run = _run(dreaming.run_dream(env.users["alice"], dreaming.TRIGGER_SCHEDULED))
        assert run["status"] == DreamRunStatus.FAILED and run["error"] == "nothing configured"
        assert run["model"] is None

    def test_poll_dreams_only_for_admitted_due_users(self, env, monkeypatch):
        import config.feature_gates as fg

        dreamed: list[int] = []

        async def _fake_run(user, trigger):
            dreamed.append((user["id"], trigger))
            return {}
        monkeypatch.setattr(dreaming, "run_dream", _fake_run)
        monkeypatch.setattr("chat.auth.check_user_allowed", lambda email: True)
        _memories(env, "alice", *[f"a{i}" for i in range(6)])
        _memories(env, "bob", *[f"b{i}" for i in range(6)])

        # Gate closed: nobody.
        _run(dreaming._poll_once())
        assert dreamed == []
        # Open for alice only.
        fg.set_feature_enabled(fg.FEATURE_DREAMING, True)
        fg.set_feature_allowed_users(fg.FEATURE_DREAMING, ["alice@example.com"])
        _run(dreaming._poll_once())
        assert dreamed == [(_uid(env, "alice"), "scheduled")]
        # Open for all: bob too, alice again (nothing paced her in the fake).
        fg.set_feature_allowed_users(fg.FEATURE_DREAMING, None)
        dreamed.clear()
        _run(dreaming._poll_once())
        assert sorted(dreamed) == sorted([(_uid(env, "alice"), "scheduled"), (_uid(env, "bob"), "scheduled")])


# ---------------------------------------------------------------------------
# The suggestion store's new shapes
# ---------------------------------------------------------------------------


class TestArchiveOnlySuggestions:
    def test_archive_only_needs_a_memory_and_accept_just_archives(self, env):
        alice = _uid(env, "alice")
        (old,) = _memories(env, "alice", "Old fact")
        with pytest.raises(ValueError):
            _run(env.store.create_suggestion(alice, None, None))
        s = _run(env.store.create_suggestion(
            alice, None, None, reasoning="stale", replaces_memory_id=old["id"],
            source=MemorySuggestionSource.DREAM,
        ))
        assert s["content"] is None and s["source"] == "dream"
        assert _run(env.store.count_pending_for_user(alice)) == 1
        assert _run(env.store.count_pending_for_user(alice, source="conversation")) == 0

        with pytest.raises(ValueError):
            _run(env.store.accept_suggestion(alice, s["id"], "edited"))
        accepted, memory = _run(env.store.accept_suggestion(alice, s["id"]))
        assert memory is None
        assert accepted["status"] == "accepted" and accepted["memory_id"] is None
        assert _run(env.memory_store.get_memory(alice, old["id"]))["archived"] is True
        assert _run(env.memory_store.list_memories(alice)) == []

    def test_inbox_lists_dream_rows_without_a_chat(self, env):
        from chat import memory_inbox

        alice = _uid(env, "alice")
        (old,) = _memories(env, "alice", "Old fact")
        _run(env.store.create_suggestion(alice, env.conversations["alice"], "From a chat"))
        _run(env.store.create_suggestion(
            alice, None, "Rewritten", replaces_memory_id=old["id"],
            source=MemorySuggestionSource.DREAM,
        ))
        rows = _run(memory_inbox.list_pending(alice))
        assert [(r["source"], r["conversation_id"] is None) for r in rows] == [
            ("conversation", False), ("dream", True),
        ]
        assert rows[1]["conversation_title"] is None and rows[1]["project_id"] is None
        assert rows[1]["replaces_content"] == "Old fact"
        assert _run(memory_inbox.count_pending(alice))["user"] == 2

    def test_publish_with_no_conversation(self, env):
        from chat.memory_suggestions import publish_changed

        alice = _uid(env, "alice")
        _run(env.store.create_suggestion(alice, env.conversations["alice"], "x"))
        assert _run(publish_changed(alice, None)) == 0
        assert _events(env)[-1] == {
            "type": "memory_suggestions_changed", "conversation_id": None,
            "pending_count": 0, "total_pending": 1,
        }

    def test_accept_route_archive_only(self, env):
        from chat import memory_routes

        alice = _uid(env, "alice")
        (old,) = _memories(env, "alice", "Old fact")
        s = _run(env.store.create_suggestion(
            alice, None, None, replaces_memory_id=old["id"], source=MemorySuggestionSource.DREAM,
        ))
        app = FastAPI()
        app.include_router(memory_routes.router)

        async def _current():
            return env.users["alice"]
        app.dependency_overrides[memory_routes.get_current_user_cookie_or_apikey_checked] = _current
        client = TestClient(app)

        resp = client.post(f"/app/api/memory-suggestions/{s['id']}/accept", json={"content": "edit"})
        assert resp.status_code == 400 and resp.json()["detail"]["error"] == "invalid_memory"
        resp = client.post(f"/app/api/memory-suggestions/{s['id']}/accept")
        assert resp.status_code == 200
        assert resp.json()["memory"] is None
        assert resp.json()["suggestion"]["status"] == "accepted"


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


def _admin_client(env, who="alice", monkeypatch=None):
    from chat.routes import admin as admin_routes
    from chat.routes import dreaming as dreaming_routes

    monkeypatch.setattr(admin_routes, "is_admin", lambda email: email == "alice@example.com")
    app = FastAPI()
    app.include_router(dreaming_routes.router)
    user = env.users[who]

    async def _current():
        return user
    app.dependency_overrides[dreaming_routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


@pytest.fixture()
def registry(monkeypatch):
    """Two Vertex models in the registry view: Opus allowed, Flash public-only."""
    import chat.llm.config as llm_config
    from config import model_selection

    specs = [
        SimpleNamespace(
            id="claude-opus-5-5", display_name="Claude Opus 5.5", provider_label="Claude on Vertex",
            thinking_efforts=("low", "high"), thinking_effort="medium", deprecated=False, enabled=True,
        ),
        SimpleNamespace(
            id="gemini-flash", display_name="Gemini Flash", provider_label="Gemini on Vertex",
            thinking_efforts=(), thinking_effort=None, deprecated=False, enabled=True,
        ),
        SimpleNamespace(
            id="old-model", display_name="Old", provider_label="Claude on Vertex",
            thinking_efforts=(), thinking_effort=None, deprecated=True, enabled=True,
        ),
    ]
    monkeypatch.setattr(llm_config, "list_model_specs", lambda: specs)
    monkeypatch.setattr(llm_config, "get_configured_models", lambda: ["claude-opus-5-5", "gemini-flash"])
    monkeypatch.setattr(llm_config, "get_available_models", lambda: ["claude-opus-5-5", "gemini-flash"])
    monkeypatch.setattr(llm_config, "resolve_model", lambda m: next((s for s in specs if s.id == m), None))
    monkeypatch.setattr(
        model_selection, "is_model_allowed", lambda m, *, public: m != "gemini-flash",
    )
    return specs


class TestAdminRoutes:
    def test_requires_admin(self, env, monkeypatch, registry):
        client = _admin_client(env, "bob", monkeypatch)
        assert client.get("/app/api/admin/dreaming").status_code == 403
        assert client.put("/app/api/admin/dreaming", json={}).status_code == 403
        assert client.post("/app/api/admin/dreaming/run").status_code == 403

    def test_get_shape(self, env, monkeypatch, registry):
        body = _admin_client(env, monkeypatch=monkeypatch).get("/app/api/admin/dreaming").json()
        assert body["settings"] == {"model": None, "thinking_effort": None}
        assert body["resolved"] == {"model": "claude-opus-5-5", "thinking_effort": None, "automatic": True}
        assert body["unavailable_reason"] is None
        assert body["enabled"] is False and body["enabled_for_me"] is False
        assert [(m["id"], m["dreamable"], m["unavailable_reason"]) for m in body["models"]] == [
            ("claude-opus-5-5", True, None),
            ("gemini-flash", False, "not_allowed_private"),
        ]
        assert body["models"][0]["thinking_efforts"] == ["low", "high"]
        assert body["recent_runs"] == []

    def test_put_validates_and_persists(self, env, monkeypatch, registry):
        from config.dreaming import read_dreaming_settings

        client = _admin_client(env, monkeypatch=monkeypatch)
        resp = client.put("/app/api/admin/dreaming", json={"model": "nope"})
        assert resp.status_code == 400 and resp.json()["detail"]["error"] == "unknown_model"
        resp = client.put("/app/api/admin/dreaming", json={"model": "old-model"})
        assert resp.status_code == 400 and resp.json()["detail"]["error"] == "unknown_model"
        resp = client.put("/app/api/admin/dreaming", json={"model": "gemini-flash"})
        assert resp.status_code == 400 and resp.json()["detail"]["error"] == "invalid_params"
        resp = client.put(
            "/app/api/admin/dreaming", json={"model": "claude-opus-5-5", "thinking_effort": "max"},
        )
        assert resp.status_code == 400 and "thinking level" in resp.json()["detail"]["message"]

        resp = client.put(
            "/app/api/admin/dreaming", json={"model": "claude-opus-5-5", "thinking_effort": "HIGH"},
        )
        assert resp.status_code == 200
        assert resp.json()["settings"] == {"model": "claude-opus-5-5", "thinking_effort": "high"}
        assert resp.json()["resolved"] == {
            "model": "claude-opus-5-5", "thinking_effort": "high", "automatic": False,
        }
        assert read_dreaming_settings() == {"model": "claude-opus-5-5", "thinking_effort": "high"}

        resp = client.put("/app/api/admin/dreaming", json={"model": None, "thinking_effort": None})
        assert resp.status_code == 200 and resp.json()["settings"]["model"] is None

    def test_run_now(self, env, monkeypatch, registry):
        import config.feature_gates as fg

        client = _admin_client(env, monkeypatch=monkeypatch)
        resp = client.post("/app/api/admin/dreaming/run")
        assert resp.status_code == 403 and resp.json()["detail"]["error"] == "feature_disabled"

        fg.set_feature_enabled(fg.FEATURE_DREAMING, True)
        resp = client.post("/app/api/admin/dreaming/run")
        assert resp.json() == {"status": "skipped", "reason": "too_few_memories"}

        (old, _new) = _memories(env, "alice", "Prefers tea", "Prefers green tea")
        provider = FakeProvider(json.dumps({"proposals": [
            {"kind": "archive", "memory_id": old["id"], "reasoning": "dup"},
        ]}))
        _patch_model(monkeypatch, provider)
        resp = client.post("/app/api/admin/dreaming/run")
        body = resp.json()
        assert body["status"] == "completed"
        assert body["run"]["trigger"] == "manual" and body["run"]["suggestions_created"] == 1
        assert body["run"]["user_email"] == "alice@example.com"

        listing = client.get("/app/api/admin/dreaming").json()
        assert [(r["status"], r["user_email"]) for r in listing["recent_runs"]] == [
            ("completed", "alice@example.com"),
        ]
        # Paced by the unreviewed batch now.
        assert client.post("/app/api/admin/dreaming/run").json() == {
            "status": "skipped", "reason": "pending_review",
        }
