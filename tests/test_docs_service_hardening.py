"""Review-driven hardening tests for chat/docs/service.py.

Pins the fixes made after the service review:

1. Title uniqueness is scoped per mode, so a public conversation's
   ``create_doc`` can never learn whether a private doc of that title exists
   (invariant 2), and a mode switch into a colliding title is refused.
2. ``add_doc_image`` never echoes a server-side path in an error.
3. A write cancelled mid-flight still lands its DB bump (shielded).
4. Access is re-resolved under the per-doc lock (a share added while a
   write queues turns it into an approval).
5. Lone surrogates fail as a DocError, not a raw codec error.
6. Hidden and missing ids do the same DB work.
"""

import asyncio
import uuid

import pytest

from chat.docs.access import DENY_PUBLIC_DOC_FROM_PRIVATE
from chat.docs.constants import doc_not_found_message
from chat.docs.service import DocApprovalRequired, DocError
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


class TestTitleNamespacePerMode:
    def test_public_caller_cannot_probe_private_titles(self, docs_env):
        seed_doc(docs_env, title="Acquisition of Acme", mode="private")
        public_caller = make_caller(docs_env, project=docs_env.public_project, public=True)
        # The same title in the public namespace succeeds: the collision error
        # would otherwise leak the private doc's existence one bit at a time.
        created = _run(svc().create_doc(public_caller, "acquisition of acme", "pub\n"))
        assert created["mode"] == "public"
        # And the private namespace still refuses its own duplicates.
        private_caller = make_caller(docs_env)
        with pytest.raises(DocError, match="already exists"):
            _run(svc().create_doc(private_caller, "ACQUISITION OF ACME", "x\n"))

    def test_mode_switch_into_colliding_title_refused(self, docs_env):
        store = docs_env.doc_store
        private = seed_doc(docs_env, title="Report", mode="private")
        seed_doc(docs_env, title="report", mode="public")
        with pytest.raises(store.DuplicateDocTitleError):
            _run(store.set_doc_mode(private["id"], "public"))
        assert _run(store.get_doc(private["id"]))["mode"] == "private"


class TestErrorShaping:
    def test_add_doc_image_never_leaks_server_paths(self, docs_env):
        doc = seed_doc(docs_env)
        caller = make_caller(docs_env)
        workspace_dir(docs_env, caller)
        for bad in ("a" * 300 + ".png", "nul\x00.png"):
            with pytest.raises(DocError) as exc:
                _run(svc().add_doc_image(caller, doc["id"], bad))
            text = str(exc.value)
            assert str(docs_env.dirs["chats"]) not in text
            assert str(docs_env.tmp) not in text

    def test_lone_surrogate_is_a_doc_error(self, docs_env):
        caller = make_caller(docs_env)
        with pytest.raises(DocError):
            _run(svc().create_doc(caller, "Bad", "oops \ud800\n"))
        doc = seed_doc(docs_env, content="ok\n")
        with pytest.raises(DocError):
            _run(svc().append_to_doc(caller, doc["id"], "oops \ud800"))
        assert body(doc["id"]) == "ok\n"

    def test_hidden_and_missing_do_the_same_db_work(self, docs_env, monkeypatch):
        import db.doc_store as store
        private = seed_doc(docs_env, title="Secret")
        caller = make_caller(docs_env, project=docs_env.public_project, public=True)
        calls: list[str] = []
        real = store._load_shares

        async def counting(db, ids):
            calls.append("shares")
            return await real(db, ids)

        monkeypatch.setattr(store, "_load_shares", counting)
        with pytest.raises(DocError):
            _run(svc().read_doc(caller, private["id"]))
        hidden_calls = len(calls)
        calls.clear()
        with pytest.raises(DocError):
            _run(svc().read_doc(caller, str(uuid.uuid4())))
        assert len(calls) == hidden_calls == 1


class TestLockedWrite:
    def test_cancelled_write_still_lands_db_bump(self, docs_env, monkeypatch):
        """Cancel the caller while the file write runs: the shielded inner
        task finishes file -> DB -> events, so content_size stays honest."""
        from chat.docs import files as doc_files

        doc = seed_doc(docs_env, content="start\n")
        caller = make_caller(docs_env)
        real_modify = doc_files.modify_body
        started = asyncio.Event()

        def slow_modify(doc_id, fn, **kw):
            started._loop.call_soon_threadsafe(started.set)
            import time
            time.sleep(0.2)
            return real_modify(doc_id, fn, **kw)

        monkeypatch.setattr(doc_files, "modify_body", slow_modify)

        async def scenario():
            started._loop = asyncio.get_running_loop()
            task = asyncio.create_task(svc().append_to_doc(caller, doc["id"], "entry"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Give the shielded inner task time to complete.
            for _ in range(50):
                await asyncio.sleep(0.02)
                row = await docs_env.doc_store.get_doc(doc["id"])
                if row["content_size"] == len("start\n\nentry\n"):
                    return row
            return await docs_env.doc_store.get_doc(doc["id"])

        row = _run(scenario())
        assert body(doc["id"]) == "start\n\nentry\n"
        assert row["content_size"] == (docs_env.dirs["docs"] / doc["id"] / "doc.md").stat().st_size
        assert row["last_write_source"] == f"conversation:{caller.conversation_id}"

    def test_share_added_while_queued_turns_write_into_approval(self, docs_env):
        """A second writer queued behind the lock sees the share that landed
        meanwhile and gets approval_required instead of a free write."""
        doc = seed_doc(docs_env, content="start\n")
        caller = make_caller(docs_env)
        bob = docs_env.users["bob"]["id"]
        lock = svc()._write_lock(doc["id"])

        async def scenario():
            async with lock:  # hold the doc lock so the write queues
                writer = asyncio.create_task(svc().append_to_doc(caller, doc["id"], "entry"))
                await asyncio.sleep(0.05)  # writer passed its first resolve, now waits
                assert not writer.done()
                await docs_env.doc_store.add_share(doc["id"], bob, "read")
            with pytest.raises(DocApprovalRequired):
                await writer

        _run(scenario())
        assert body(doc["id"]) == "start\n"

    def test_mode_flip_while_queued_denies_write(self, docs_env):
        doc = seed_doc(docs_env, content="start\n")
        caller = make_caller(docs_env)
        lock = svc()._write_lock(doc["id"])

        async def scenario():
            async with lock:
                writer = asyncio.create_task(svc().append_to_doc(caller, doc["id"], "entry"))
                await asyncio.sleep(0.05)
                await docs_env.doc_store.set_doc_mode(doc["id"], "public")
            with pytest.raises(DocError) as exc:
                await writer
            assert str(exc.value) == DENY_PUBLIC_DOC_FROM_PRIVATE

        _run(scenario())
        assert body(doc["id"]) == "start\n"

    def test_hidden_doc_creates_no_write_lock(self, docs_env):
        private = seed_doc(docs_env, title="Secret")
        caller = make_caller(docs_env, project=docs_env.public_project, public=True)
        with pytest.raises(DocError) as exc:
            _run(svc().append_to_doc(caller, private["id"], "x"))
        assert str(exc.value) == doc_not_found_message(private["id"])
        assert private["id"] not in list(svc()._write_locks.keys())
