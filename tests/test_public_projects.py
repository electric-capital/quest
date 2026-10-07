"""Tests for public projects (internet sandbox, no internal data access).

Covers the prototype's three enforcement layers:

* The podman argv builder's two profiles (restricted vs public): network
  posture, credential injection, and image selection.
* The dispatch-time tool allowlist (the actual security boundary behind the
  trimmed ``PUBLIC_TOOLS`` schema).
* The public system prompt: must not leak the proxy preamble / API key,
  skills, memories, or connector documentation.
* The ``projects.public`` flag: creation-time only, immutable via update.
"""

import asyncio
import json
import os
import shutil
import tempfile

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config.feature_gates as fg

from chat.gemini_api.tool_handlers import _build_script_podman_cmd
from chat.llm.tool_schemas import (
    PUBLIC_TOOLS,
    PUBLIC_TOOL_CALL_ALLOWLIST,
    TOOL_CALL_REGISTRY,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Podman argv builder
# ---------------------------------------------------------------------------


class TestBuildScriptPodmanCmd:
    def test_restricted_profile_injects_token_and_blocks_egress(self):
        # The third positional is the per-run sandbox token (see
        # chat/sandbox_tokens.py); the builder injects whatever it is given.
        cmd = _build_script_podman_cmd("/ws", ["python3", "x.py"], "sk-secret")
        assert (
            "--network=slirp4netns:allow_host_loopback=true,outbound_addr=127.0.0.1"
            ",enable_ipv6=false"
        ) in cmd
        # No host resolv.conf (internal search domains / nameservers) may
        # leak into the container; DNS is dead in this profile anyway.
        assert "--dns=none" in cmd
        assert "QUEST_API_KEY=sk-secret" in cmd
        assert any(c.startswith("QUEST_PORT=") for c in cmd)
        assert any(c.startswith("quest-script-runner-") and "public" not in c for c in cmd)
        assert cmd[-2:] == ["python3", "x.py"]

    def test_public_profile_opens_egress_and_withholds_credentials(self):
        cmd = _build_script_podman_cmd(
            "/ws", ["python3", "x.py"], "sk-secret", public=True,
        )
        assert "--network=slirp4netns:allow_host_loopback=false,enable_ipv6=false" in cmd
        # DNS pinned to the slirp resolver with the host's search-domain
        # list cleared, so internal topology never reaches the sandbox.
        assert "--dns=10.0.2.3" in cmd
        assert "--dns-search=." in cmd
        assert "--dns=none" not in cmd
        # The load-bearing property: no sandbox token and no proxy port
        # reach a public container, even if a caller passes one by mistake.
        joined = " ".join(cmd)
        assert "sk-secret" not in joined
        assert "QUEST_API_KEY" not in joined
        assert "QUEST_PORT" not in joined
        assert any(c.startswith("quest-script-runner-public-") for c in cmd)

    def test_shared_privilege_drop_choreography(self):
        for public in (False, True):
            cmd = _build_script_podman_cmd("/ws", ["bash"], "k", public=public)
            assert "--user=0:0" in cmd
            assert "--cap-add=NET_ADMIN" in cmd
            assert "--cap-add=SETPCAP" in cmd
            assert "--userns=keep-id" in cmd
            assert f"QUEST_RUN_UID={os.getuid()}" in cmd
            assert f"QUEST_RUN_GID={os.getgid()}" in cmd

    def test_both_profiles_disable_ipv6(self):
        """outbound_addr and the entrypoint iptables rules are IPv4-only;
        slirp4netns's default IPv6 stack routed the host's loopback (fd00::2)
        and any host-side IPv6 egress around them, exposing QUEST_API_KEY.
        Both layers must be present: slirp must not serve IPv6, and the
        kernel must not have an IPv6 stack in the container's netns.
        """
        for public in (False, True):
            cmd = _build_script_podman_cmd("/ws", ["bash"], "k", public=public)
            network = next(c for c in cmd if c.startswith("--network="))
            assert "enable_ipv6=false" in network.split(":", 1)[1].split(",")
            assert "--sysctl=net.ipv6.conf.all.disable_ipv6=1" in cmd

    def test_interactive_flag(self, monkeypatch):
        import chat.gemini_api.tool_handlers.sandbox as sandbox

        monkeypatch.setattr(sandbox, "get_sandbox_runtime", lambda: None)
        cmd = _build_script_podman_cmd("/ws", ["python3", "-u", "-"], "k", interactive=True)
        assert cmd[:4] == ["podman", "run", "--rm", "-i"]
        cmd = _build_script_podman_cmd("/ws", ["python3"], "k")
        assert "-i" not in cmd

    def test_both_profiles_deny_symlink_creation_via_seccomp(self, tmp_path, monkeypatch):
        """The workspace mount is host-backed: every sandbox run must carry
        the generated seccomp profile that ERRNOs symlink/symlinkat, or a
        script could plant links that host-side file operations dereference.
        """
        import errno
        import json
        from pathlib import Path

        import chat.gemini_api.sandbox_seccomp as sandbox_seccomp
        from config import paths

        monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
        monkeypatch.setattr(sandbox_seccomp, "_generated", None)

        for public in (False, True):
            cmd = _build_script_podman_cmd("/ws", ["bash"], "k", public=public)
            opts = [c for c in cmd if c.startswith("--security-opt=seccomp=")]
            assert len(opts) == 1
            profile_path = Path(opts[0].split("=", 2)[2])
            assert profile_path == tmp_path / "sandbox-seccomp.json"
            profile = json.loads(profile_path.read_text())
            deny_groups = [
                g for g in profile["syscalls"]
                if set(g["names"]) & {"symlink", "symlinkat"}
            ]
            assert deny_groups == [deny_groups[0]]
            assert deny_groups[0]["names"] == ["symlink", "symlinkat"]
            assert deny_groups[0]["action"] == "SCMP_ACT_ERRNO"
            assert deny_groups[0]["errnoRet"] == errno.EPERM


# ---------------------------------------------------------------------------
# Tool tier + dispatch allowlist
# ---------------------------------------------------------------------------


class TestPublicToolTier:
    def test_public_tools_are_the_three_expected(self):
        assert [t["name"] for t in PUBLIC_TOOLS] == [
            "tool_call", "run_script", "run_python",
        ]

    def test_allowlist_is_subset_of_registry(self, slack_plugin):
        # send_slack_dm_to_self is served by the Slack plugin (the one
        # migrated tool in the allowlist), so the subset invariant holds
        # with the plugin registered.
        assert PUBLIC_TOOL_CALL_ALLOWLIST <= set(TOOL_CALL_REGISTRY)

    def test_self_dm_send_is_allowlisted(self):
        # The one connector write allowed in public projects: outbound-only
        # to the user themselves (fixed recipient, no internal reads).
        assert "send_slack_dm_to_self" in PUBLIC_TOOL_CALL_ALLOWLIST

    def test_allowlist_has_no_internal_data_tools(self):
        for name in (
            "memory_search", "memory_list", "authed_get", "authed_post",
            "wait_for_handles", "download_drive_file",
            "get_gmail_messages", "search_slack_messages",
        ):
            assert name not in PUBLIC_TOOL_CALL_ALLOWLIST


class TestPublicDispatchRejects:
    def _dispatch(self, tool_name, args):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call_inner
        return _run(_dispatch_tool_call_inner(
            app=None, provider=None,
            user={"id": 1, "email": "u@example.com", "api_key": "sk-secret"},
            conversation_id="conv-1", timezone="UTC",
            tool_name=tool_name, args=args, is_public=True,
        ))

    def test_blocked_dynamic_tool_rejected(self):
        result, extra = self._dispatch(
            "tool_call", {"tool_name": "memory_search", "arguments": {"query": "x"}},
        )
        parsed = json.loads(result)
        assert "not available in public-project" in parsed["error"]
        assert extra == []

    def test_blocked_direct_tool_rejected(self):
        for name in ("curl_proxy_get", "authed_get", "load_gmail_attachment",
                     "load_skills", "memory_search"):
            result, _ = self._dispatch(name, {})
            parsed = json.loads(result)
            assert "not available in public-project" in parsed["error"], name

    def test_allowed_dynamic_tool_passes_gate(self):
        # get_current_time has no external dependencies, so the full
        # dispatch succeeds and proves the allowlist lets it through.
        result, _ = self._dispatch(
            "tool_call", {"tool_name": "get_current_time", "arguments": {}},
        )
        parsed = json.loads(result)
        assert "error" not in parsed
        assert "utc" in json.dumps(parsed).lower()


class TestPublicBlockedLoopTools:
    def test_loop_blocklist_covers_spawners_and_action_requests(self):
        from chat.gemini_api.conversation import _PUBLIC_BLOCKED_LOOP_TOOLS
        for name in (
            "agent_task", "agent_task_parallel", "agent_task_parallel_template",
            "create_action_request", "send_slack_reply_and_get_response",
            "return_to_caller", "return_final_response",
        ):
            assert name in _PUBLIC_BLOCKED_LOOP_TOOLS


# ---------------------------------------------------------------------------
# Public system prompt
# ---------------------------------------------------------------------------


_DOC_TOOLS = (
    "list_docs", "search_docs", "read_doc", "create_doc", "edit_doc",
    "append_to_doc", "add_doc_image",
)


class TestPublicSystemPrompt:
    def _prompt(self, **kw):
        from chat.gemini_api.system_prompt import get_public_project_system_prompt
        return get_public_project_system_prompt(
            user_name="Ada", user_email="ada@example.com",
            project_guide="Track BTC prices.", **kw,
        )

    @pytest.mark.parametrize("docs_enabled", [False, True])
    def test_no_proxy_preamble_or_api_key(self, docs_enabled):
        p = self._prompt(docs_enabled=docs_enabled)
        assert "Authorization" not in p
        assert "QUEST" not in p
        assert "curl_proxy" not in p
        assert "localhost" not in p

    @pytest.mark.parametrize("docs_enabled", [False, True])
    def test_no_internal_tool_docs(self, docs_enabled):
        # Internal tool NAMES must not be documented as callable. (The
        # Boundaries block legitimately names Slack/email as unavailable.)
        # Holds with the Quest Docs tools and paragraph present too.
        p = self._prompt(docs_enabled=docs_enabled)
        for term in (
            "memory_search", "authed_get", "authed_post", "load_skills",
            "system:", "create_action_request(", "agent_task(",
            "load_gmail_attachment", "wait_for_handles",
        ):
            # get_response_content's registry description cross-references
            # authed_get; strip tool descriptions' cross-reference line
            # would be overkill -- assert on the doc bullet instead.
            if term == "authed_get":
                assert "- **authed_get**" not in p
                continue
            assert term not in p, term

    def test_keeps_identity_project_guide_and_public_tools(self):
        # docs_enabled=True: every allowlisted tool, the doc tools included,
        # is documented (the doc tools are gated on the Quest Docs gate).
        p = self._prompt(docs_enabled=True)
        assert "Ada" in p and "ada@example.com" in p
        assert "Track BTC prices." in p
        for name in sorted(PUBLIC_TOOL_CALL_ALLOWLIST):
            assert name in p, name
        assert "run_script" in p and "run_python" in p
        assert "internet" in p.lower()

    def test_doc_tools_hidden_when_docs_gate_closed(self):
        # Default (gate closed for the user): none of the seven doc tools
        # and no Quest Docs paragraph; the rest of the allowlist stays.
        p = self._prompt()
        for name in _DOC_TOOLS:
            assert name not in p, name
        assert "Quest Docs" not in p
        for name in sorted(PUBLIC_TOOL_CALL_ALLOWLIST - set(_DOC_TOOLS)):
            assert name in p, name

    def test_doc_tools_shown_when_docs_gate_open(self):
        p = self._prompt(docs_enabled=True)
        for name in _DOC_TOOLS:
            assert f"- **{name}**" in p, name
        # The paragraph follows the Boundaries block and says this
        # conversation only sees and creates public docs.
        boundaries = p.index("**Boundaries (this is a public project):**")
        docs = p.index("**Quest Docs:**")
        assert docs > boundaries
        assert docs < p.index("**Conversation naming")
        paragraph = p[docs:p.index("**Conversation naming")]
        assert "PUBLIC docs" in paragraph
        assert "no approval step" in paragraph
        # User docs are always private: docs are created in this project.
        assert "User docs are always private" in paragraph
        assert 'create_doc(target="project")' in paragraph
        assert "no doc's mode can be switched" in paragraph

    def test_doc_registry_roster_matches(self):
        # The prompt derives the doc tools from the registry's
        # requires_service key; pin that it finds exactly the seven.
        from chat.gemini_api.system_prompt import _doc_tool_names
        assert _doc_tool_names() == frozenset(_DOC_TOOLS)
        assert set(_DOC_TOOLS) <= PUBLIC_TOOL_CALL_ALLOWLIST

    def test_routine_run_with_docs_enabled(self):
        # is_routine and docs_enabled compose: naming tool out, docs in.
        p = self._prompt(docs_enabled=True, is_routine=True)
        assert "- **set_conversation_name**" not in p
        assert "- **append_to_doc**" in p
        assert "**Quest Docs:**" in p


# ---------------------------------------------------------------------------
# projects.public flag (store level)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated_project_store(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_public_projects_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    test_session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.project_store as project_store_mod

    models_mod.Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(project_store_mod, "AsyncSessionLocal", test_session_local)

    yield project_store_mod

    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestProjectPublicFlag:
    def test_defaults_to_private(self, _isolated_project_store):
        store = _isolated_project_store
        project = _run(store.create_project(1, "Private Things"))
        assert project["public"] is False

    def test_public_at_creation(self, _isolated_project_store):
        store = _isolated_project_store
        project = _run(store.create_project(1, "Open Research", public=True))
        assert project["public"] is True
        fetched = _run(store.get_project(1, project["id"]))
        assert fetched["public"] is True

    def test_update_cannot_flip_public(self, _isolated_project_store):
        store = _isolated_project_store
        project = _run(store.create_project(1, "Open Research", public=True))
        updated = _run(store.update_project(
            1, project["id"], name="Renamed", guide="new guide",
        ))
        assert updated["public"] is True
        assert updated["name"] == "Renamed"


# ---------------------------------------------------------------------------
# Public projects are a default feature: no gate, creatable by everyone
# ---------------------------------------------------------------------------


@pytest.fixture
def gates_file(tmp_path, monkeypatch):
    """Point the feature-gate store at a per-test file (missing = all off)."""
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


class TestPublicProjectsUngated:
    def test_no_feature_gate(self, gates_file):
        assert "public_projects" not in fg.KNOWN_FEATURES
        assert "public_projects" not in fg.FEATURE_LABELS
        # Nor a conversation flag.
        from chat.conversation_flags import KNOWN_FLAGS
        assert "public_projects" not in KNOWN_FLAGS

    def test_stale_gate_key_in_file_is_ignored(self, gates_file):
        # Installs that toggled the retired gate keep the key in
        # feature_gates.json; reads ignore it and the next write drops it.
        gates_file.write_text(json.dumps({"public_projects": False}))
        assert "public_projects" not in fg.read_feature_gates()
        fg.set_feature_enabled(fg.FEATURE_USER_SUBAGENTS, True)
        assert "public_projects" not in json.loads(gates_file.read_text())

    def test_create_allowed_with_all_gates_off(
        self, gates_file, _isolated_project_store,
    ):
        from chat.project_routes import _create_project_checked
        project = _run(_create_project_checked(
            {"id": 1, "email": "user@example.com"},
            "Open Research", public=True,
        ))
        assert project["public"] is True

    def test_visible_with_all_gates_off(self, gates_file, _isolated_project_store):
        from chat.project_routes import (
            get_user_project, list_user_projects, update_user_project,
            UpdateProjectRequest,
        )
        store = _isolated_project_store
        user = {"id": 1, "email": "user@example.com"}
        public = _run(store.create_project(1, "Open Research", public=True))
        _run(store.create_project(1, "Private Things"))

        names = [p["name"] for p in _run(list_user_projects(user=user))["projects"]]
        assert set(names) == {"Open Research", "Private Things"}
        assert _run(get_user_project(public["id"], user=user))["public"] is True
        renamed = _run(update_user_project(
            public["id"], UpdateProjectRequest(name="Renamed"), user=user,
        ))
        assert renamed["name"] == "Renamed"
        # Still owner-scoped: another user's lookup is a 404.
        with pytest.raises(HTTPException) as exc:
            _run(get_user_project(public["id"], user={"id": 2, "email": "x@example.com"}))
        assert exc.value.status_code == 404
