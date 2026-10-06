"""Second review round: write_doc card cost, locked UI mutations, hidden
public-project docs by id, pinned image bytes at approve time.
"""

import asyncio
import hashlib
import time
import uuid

import pytest

from chat.docs.constants import doc_not_found_message
from tests.test_docs_service import (  # noqa: F401  (fixture re-export)
    PNG,
    _run,
    body,
    docs_env,
    make_caller,
    seed_doc,
    svc,
    workspace_dir,
)
from tests.test_write_doc_action_request import (
    append_params,
    execute,
    image_params,
    propose,
    shared_doc,
)
from tests.test_docs_routes import client, detail, open_public_projects


class TestBoundedCardDiff:
    def test_precard_append_on_large_doc_is_fast_and_small(self, docs_env):
        big = "".join(f"paragraph {i}\n\n" for i in range(20000))  # ~300 KB
        doc = shared_doc(docs_env, content=big)
        caller = make_caller(docs_env)
        started = time.monotonic()
        validated = propose(caller, append_params(doc["id"], content="## 2026-10-05\nnew entry"))
        elapsed = time.monotonic() - started
        diff = validated["content_diff"]
        assert elapsed < 2.0
        assert diff["added"] == 2 and diff["removed"] == 0  # body already ends with a blank line
        assert len(diff["lines"]) <= 10
        assert diff["truncated"] is False
        assert diff["total_new_lines"] == diff["total_old_lines"] + 2
        # Line numbers match read_doc's 1-based lines.
        first_added = next(l for l in diff["lines"] if l["type"] == "add")
        assert first_added["new_line"] == diff["total_old_lines"] + 1

    def test_heavy_edit_diff_is_capped(self, docs_env):
        from chat.action_request_types._skill_content_edit import build_bounded_content_diff

        old = "\n".join(f"line {i}" for i in range(3000)) + "\n"
        new = "\n".join(f"LINE {i}" for i in range(3000)) + "\n"
        diff = build_bounded_content_diff(old, new)
        assert diff["truncated"] is True
        assert len(diff["lines"]) == 400
        assert diff["added"] == 3000 and diff["removed"] == 3000


class TestLockedUiMutations:
    def test_mode_switch_waits_for_in_flight_write(self, docs_env):
        doc = seed_doc(docs_env, content="start\n")
        lock = svc()._write_lock(doc["id"])
        alice = docs_env.users["alice"]

        async def scenario():
            async with lock:
                switch = asyncio.create_task(svc().switch_doc_mode(alice, doc["id"], "public"))
                await asyncio.sleep(0.05)
                assert not switch.done()
            updated = await switch
            assert updated["mode"] == "public"

        _run(scenario())

    def test_queued_private_write_cannot_land_after_flip_to_public(self, docs_env):
        """A model write that passed its first access check before the
        owner flipped the doc public is denied once it gets the lock."""
        doc = seed_doc(docs_env, content="start\n")
        caller = make_caller(docs_env)
        alice = docs_env.users["alice"]
        lock = svc()._write_lock(doc["id"])

        async def scenario():
            async with lock:
                writer = asyncio.create_task(
                    svc().append_to_doc(caller, doc["id"], "INTERNAL SECRET"),
                )
                await asyncio.sleep(0.05)  # writer resolved once, now queued
                switch = asyncio.create_task(svc().switch_doc_mode(alice, doc["id"], "public"))
                await asyncio.sleep(0.05)
            # Both queued behind us; the writer was first in line.
            results = await asyncio.gather(writer, switch, return_exceptions=True)
            return results

        writer_result, switch_result = _run(scenario())
        final = body(doc["id"])
        mode = _run(docs_env.doc_store.get_doc(doc["id"]))["mode"]
        assert mode == "public"
        if isinstance(writer_result, Exception):
            # Switch won the lock first: the write was denied.
            assert "INTERNAL SECRET" not in final
        else:
            # Writer won: its content landed while the doc was still private
            # and the switch (which the owner explicitly confirmed) followed.
            assert final.endswith("INTERNAL SECRET\n")

    def test_delete_route_uses_locked_path(self, docs_env):
        doc = seed_doc(docs_env, "Gone")
        c = client(docs_env)
        resp = c.delete(f"/app/api/docs/{doc['id']}")
        assert resp.status_code == 200
        assert _run(docs_env.doc_store.get_doc(doc["id"])) is None
        assert not (docs_env.dirs["docs"] / doc["id"]).exists()
        resp = c.delete(f"/app/api/docs/{doc['id']}")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "doc_not_found"

    def test_mode_switch_route_errors(self, docs_env):
        open_public_projects(docs_env)  # switching to public needs the gate
        private = seed_doc(docs_env, title="Report", mode="private")
        seed_doc(docs_env, title="REPORT", mode="public")
        c = client(docs_env)
        resp = c.put(f"/app/api/docs/{private['id']}/mode", json={"mode": "public"})
        assert resp.status_code == 409
        assert detail(resp)["error"] == "duplicate_title"
        resp = client(docs_env, "bob").put(
            f"/app/api/docs/{private['id']}/mode", json={"mode": "public"},
        )
        assert resp.status_code == 404  # bob cannot see alice's doc at all


class TestHiddenPublicProjectDocsById:
    @pytest.fixture()
    def hidden_doc(self, docs_env):
        open_public_projects(docs_env)
        doc = seed_doc(
            docs_env, "Open doc", mode="public", project_id=docs_env.public_project,
            content="open\n",
        )
        from chat.docs import files
        files.add_asset(doc["id"], "c.png", PNG)
        # Close the public-projects gate: the project and its docs vanish.
        docs_env.fg.set_feature_enabled(docs_env.fg.FEATURE_PUBLIC_PROJECTS, False)
        return doc

    def test_every_by_id_route_404s_like_missing(self, docs_env, hidden_doc):
        c = client(docs_env)
        doc_id = hidden_doc["id"]
        missing = str(uuid.uuid4())
        calls = [
            lambda i: c.get(f"/app/api/docs/{i}"),
            lambda i: c.put(f"/app/api/docs/{i}", json={"title": "x"}),
            lambda i: c.put(f"/app/api/docs/{i}/mode", json={"mode": "private"}),
            lambda i: c.get(f"/app/api/docs/{i}/assets/c.png"),
            lambda i: c.get(f"/app/api/docs/{i}/download?format=md"),
            lambda i: c.delete(f"/app/api/docs/{i}"),
        ]
        for call in calls:
            hidden = call(doc_id)
            absent = call(missing)
            assert hidden.status_code == 404 == absent.status_code
            assert hidden.text.replace(doc_id, "<id>") == absent.text.replace(missing, "<id>")
        # Nothing was deleted or changed.
        assert _run(docs_env.doc_store.get_doc(doc_id))["title"] == "Open doc"
        assert body(doc_id) == "open\n"

    def test_reopening_the_gate_restores_access(self, docs_env, hidden_doc):
        open_public_projects(docs_env)
        resp = client(docs_env).get(f"/app/api/docs/{hidden_doc['id']}")
        assert resp.status_code == 200
        assert resp.json()["content"] == "open\n"


class TestPinnedImageAtApprove:
    def test_swapped_file_is_refused(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        ws = workspace_dir(docs_env, caller)
        (ws / "c.png").write_bytes(PNG)
        validated = propose(caller, image_params(doc["id"], alt="Chart"))
        assert validated["image_preview"]["sha256"] == hashlib.sha256(PNG).hexdigest()
        # Another conversation of the same project workspace swaps the file.
        other = PNG + b"\x01" * 16
        (ws / "c.png").write_bytes(other)
        with pytest.raises(RuntimeError, match="changed since"):
            execute(validated, caller)
        assert list((docs_env.dirs["docs"] / doc["id"] / "assets").iterdir()) == []
        assert "assets/c.png" not in body(doc["id"])

    def test_unchanged_file_is_stored(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "c.png").write_bytes(PNG)
        validated = propose(caller, image_params(doc["id"], alt="Chart"))
        result = execute(validated, caller)
        assert result["asset"] == "c.png"
        assert (docs_env.dirs["docs"] / doc["id"] / "assets" / "c.png").read_bytes() == PNG

    def test_suggested_request_never_carries_the_pin(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "c.png").write_bytes(PNG)
        with pytest.raises(svc().DocApprovalRequired) as exc:
            _run(svc().add_doc_image(caller, doc["id"], "c.png"))
        assert "expected_sha256" not in exc.value.suggested_request["params"]


class TestProjectLookupFailsClosed:
    def test_missing_project_row_refuses_execute(self, docs_env):
        from chat.action_request_types.write_doc import project_is_public

        with pytest.raises(RuntimeError):
            _run(project_is_public(docs_env.users["alice"], str(uuid.uuid4())))
        assert _run(project_is_public(docs_env.users["alice"], None)) is False
        assert _run(project_is_public(docs_env.users["alice"], docs_env.public_project)) is True


def test_precard_missing_project_is_a_same_turn_rejection(docs_env):
    from chat.action_request_types.doc_precard import doc_precard_check

    doc = shared_doc(docs_env)
    caller = make_caller(docs_env, project=str(uuid.uuid4()))
    params = {**append_params(doc["id"]), "ensure_blank_line": True}
    with pytest.raises(ValueError, match="project was not found"):
        _run(doc_precard_check(params, caller.user, caller.project_id, caller.conversation_id))
