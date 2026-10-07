"""The ``write_doc`` action request: handler, pre-card check and wiring.

Runs against the isolated ``docs_env`` fixture (tests/test_docs_service.py:
tmp SQLite file, DOCS_DIR / CHATS_DIR / PROJECTS_DIR, docs gate open,
realtime events captured). Covers:

1. ``WriteDocHandler.validate_params``: per-operation required / optional
   keys, defaults, unknown keys (incl. another operation's keys), types,
   empty values, server-injected preview keys dropped.
2. The ``approval_required`` payload of each of the three write TOOLS
   round-trips: ``validate_params(suggested_request["params"])`` returns it
   unchanged and ``doc_precard_check`` accepts it and injects the preview
   keys (content_diff, title, mode, scope, share summary, image preview).
3. Pre-card rejections: directly writable doc, hidden doc (byte-equal
   not-found text), unread doc, public (project) doc from a private
   conversation (hidden), stale old_string, read share, gate closed.
4. ``execute``: happy paths per operation (body, revision snapshot,
   ``last_write_source = action_request:<id>``, events, updated_at) and the
   approve-time re-checks (TOCTOU, share downgrade / removal, a leftover
   public user doc staying private, gate, deletion).
5. ``render_preview`` / ``summary_snippet`` / labels, registration.
6. End to end through the ``create_action_request`` dispatch arm and the
   resolve route (``request_id`` reaches execute).
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

from chat.docs.access import DENY_READ_ONLY_SHARE
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    PNG,
    body,
    docs_env,
    make_caller,
    make_legacy_public,
    seed_doc,
    svc,
    workspace_dir,
)

SHARED_BODY = "alpha beta\ngamma\n"
INJECTED = (
    "content_diff", "current_title", "doc_mode", "doc_scope",
    "share_summary", "require_approval", "image_preview",
)


def _run(coro):
    return asyncio.run(coro)


def handler():
    from chat.action_request_types.write_doc import WriteDocHandler
    return WriteDocHandler()


def shared_doc(env, content=SHARED_BODY, *, permission="read", who="bob", **kwargs):
    return seed_doc(env, content=content, shares=[(who, permission)], **kwargs)


def precard(params, caller):
    from chat.action_request_types.doc_precard import doc_precard_check

    _run(doc_precard_check(
        params, caller.user, caller.project_id, caller.conversation_id,
    ))
    return params


def propose(caller, params):
    """validate_params + pre-card, as the create_action_request arm does."""
    return precard(handler().validate_params(params), caller)


def execute(params, caller, request_id=7):
    return _run(handler().execute(
        params, caller.user,
        conversation_id=caller.conversation_id,
        project_id=caller.project_id,
        request_id=request_id,
    ))


def read(caller, doc_id):
    _run(svc().read_doc(caller, doc_id))


def tool(name, caller, args):
    from chat.gemini_api.tool_handlers import docs as doc_tools

    fn = {
        "edit_doc": doc_tools._handle_edit_doc,
        "append_to_doc": doc_tools._handle_append_to_doc,
        "add_doc_image": doc_tools._handle_add_doc_image,
    }[name]
    return json.loads(_run(fn(caller, args)))


def edit_params(doc_id, old="alpha", new="ALPHA", **extra):
    return {"operation": "edit", "doc_id": doc_id, "old_string": old,
            "new_string": new, **extra}


def append_params(doc_id, content="entry", **extra):
    return {"operation": "append", "doc_id": doc_id, "content": content, **extra}


def image_params(doc_id, path="c.png", **extra):
    return {"operation": "add_image", "doc_id": doc_id, "workspace_path": path, **extra}


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


class TestValidateParams:
    def test_edit_defaults(self):
        assert handler().validate_params(edit_params(" d1 ")) == {
            "operation": "edit", "doc_id": "d1", "old_string": "alpha",
            "new_string": "ALPHA", "replace_all": False,
        }

    def test_append_defaults(self):
        assert handler().validate_params(append_params("d1")) == {
            "operation": "append", "doc_id": "d1", "content": "entry",
            "ensure_blank_line": True,
        }

    def test_add_image_defaults(self):
        assert handler().validate_params(image_params("d1")) == {
            "operation": "add_image", "doc_id": "d1", "workspace_path": "c.png",
            "alt": "", "placement": "append",
        }

    def test_explicit_optionals_and_nulls(self):
        h = handler()
        assert h.validate_params(edit_params("d", replace_all=True))["replace_all"] is True
        assert h.validate_params(edit_params("d", replace_all=None))["replace_all"] is False
        assert h.validate_params(
            append_params("d", ensure_blank_line=False)
        )["ensure_blank_line"] is False
        out = h.validate_params(image_params("d", alt="Chart", placement="none"))
        assert (out["alt"], out["placement"]) == ("Chart", "none")
        out = h.validate_params(image_params("d", alt=None, placement=None))
        assert (out["alt"], out["placement"]) == ("", "append")

    def test_empty_new_string_deletes(self):
        assert handler().validate_params(edit_params("d", new=""))["new_string"] == ""

    @pytest.mark.parametrize("params, extra_key", [
        (edit_params("d", content="x"), "content"),
        (edit_params("d", workspace_path="x"), "workspace_path"),
        (append_params("d", old_string="x"), "old_string"),
        (append_params("d", replace_all=True), "replace_all"),
        (image_params("d", content="x"), "content"),
        (image_params("d", ensure_blank_line=True), "ensure_blank_line"),
    ])
    def test_other_operations_keys_rejected(self, params, extra_key):
        operation = params["operation"]
        with pytest.raises(ValueError) as exc:
            handler().validate_params(params)
        msg = str(exc.value)
        assert f"Unknown parameter for write_doc operation '{operation}'" in msg
        assert repr([extra_key]) in msg

    @pytest.mark.parametrize("params", [
        edit_params("d", zzz=1),
        append_params("d", title="x"),
        image_params("d", project_id="p1"),
        {"operation": "edit", "doc": "d"},
    ])
    def test_unknown_keys_rejected(self, params):
        with pytest.raises(ValueError, match="Unknown parameter for write_doc"):
            handler().validate_params(params)

    def test_unknown_key_reported_before_missing_operation(self):
        with pytest.raises(ValueError) as exc:
            handler().validate_params({"op": "edit", "doc_id": "d"})
        assert "Unknown parameter" in str(exc.value)
        assert "Missing required" not in str(exc.value)

    @pytest.mark.parametrize("params, match", [
        ({"doc_id": "d", "content": "x"}, "Missing required parameter: operation"),
        ({"operation": "replace", "doc_id": "d"}, "Invalid operation 'replace'"),
        ({"operation": ["edit"], "doc_id": "d"}, "Invalid operation"),
        ({"operation": "append", "content": "x"}, "Missing required parameter: doc_id"),
        (append_params("   "), "doc_id must be a non-empty string"),
        (append_params(5), "doc_id must be a string"),
        ({"operation": "edit", "doc_id": "d", "new_string": "x"},
         "Missing required parameter: old_string"),
        (edit_params("d", old=""), "old_string must be a non-empty string"),
        (edit_params("d", old=3), "old_string must be a string"),
        ({"operation": "edit", "doc_id": "d", "old_string": "x"},
         "Missing required parameter: new_string"),
        (edit_params("d", new=3), "new_string must be a string"),
        (edit_params("d", old="same", new="same"), "identical"),
        (edit_params("d", replace_all="true"), "replace_all must be a boolean"),
        (edit_params("d", replace_all=1), "replace_all must be a boolean"),
        ({"operation": "append", "doc_id": "d"}, "Missing required parameter: content"),
        (append_params("d", content="  \n"), "content must be a non-empty string"),
        (append_params("d", content=["x"]), "content must be a string"),
        (append_params("d", ensure_blank_line="yes"), "ensure_blank_line must be a boolean"),
        ({"operation": "add_image", "doc_id": "d"},
         "Missing required parameter: workspace_path"),
        (image_params("d", path=" "), "workspace_path must be a non-empty string"),
        (image_params("d", path=7), "workspace_path must be a string"),
        (image_params("d", placement="top"), "Invalid placement 'top'"),
        (image_params("d", placement=1), "placement must be a string"),
        (image_params("d", alt=5), "alt must be a string"),
    ])
    def test_shape_errors(self, params, match):
        with pytest.raises(ValueError, match=match):
            handler().validate_params(params)

    def test_non_dict_params(self):
        with pytest.raises(ValueError, match="params must be an object"):
            handler().validate_params(["edit"])

    def test_size_caps(self, monkeypatch):
        from chat.docs import constants

        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", 4)
        with pytest.raises(ValueError, match="new_string exceeds the maximum doc size of 4"):
            handler().validate_params(edit_params("d", new="12345"))
        with pytest.raises(ValueError, match="content exceeds the maximum doc size of 4"):
            handler().validate_params(append_params("d", content="12345"))

    @pytest.mark.parametrize("base", [
        edit_params("d"), append_params("d"), image_params("d"),
    ])
    def test_server_injected_keys_are_dropped(self, base):
        forged = {key: f"forged {key}" for key in INJECTED}
        forged["content_diff"] = {"added": 99, "removed": 0, "lines": []}
        out = handler().validate_params({**base, **forged})
        assert not set(INJECTED) & set(out)
        assert out == handler().validate_params(base)


# ---------------------------------------------------------------------------
# approval_required payloads round-trip into a valid write_doc proposal
# ---------------------------------------------------------------------------


class TestToolPayloadRoundTrip:
    def _suggested(self, payload):
        assert payload["error"] == "approval_required"
        request = payload["suggested_request"]
        assert request["request_type"] == "write_doc"
        return request["params"]

    def test_edit(self, docs_env):
        from chat.action_request_types._skill_content_edit import build_bounded_content_diff as build_content_diff

        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        params = self._suggested(tool("edit_doc", caller, {
            "doc_id": doc["id"], "old_string": "alpha", "new_string": "ALPHA",
        }))
        validated = handler().validate_params(params)
        assert validated == params
        precard(validated, caller)
        assert validated["current_title"] == "Notes"
        assert validated["doc_mode"] == "private"
        assert validated["doc_scope"] == "user"
        assert validated["share_summary"] == "shared with 1 user"
        assert validated["require_approval"] is False
        assert validated["content_diff"] == build_content_diff(
            SHARED_BODY, "ALPHA beta\ngamma\n",
        )
        assert (validated["content_diff"]["added"], validated["content_diff"]["removed"]) == (1, 1)
        assert "image_preview" not in validated
        assert body(doc["id"]) == SHARED_BODY  # nothing written

    def test_append(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        params = self._suggested(tool("append_to_doc", caller, {
            "doc_id": doc["id"], "content": "## 2026-10-05\nentry",
        }))
        validated = handler().validate_params(params)
        assert validated == params
        precard(validated, caller)
        diff = validated["content_diff"]
        added = [line["text"] for line in diff["lines"] if line["type"] == "add"]
        assert added == ["", "## 2026-10-05", "entry"]
        assert diff["removed"] == 0
        assert body(doc["id"]) == SHARED_BODY

    def test_add_image(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "c.png").write_bytes(PNG)
        params = self._suggested(tool("add_doc_image", caller, {
            "doc_id": doc["id"], "workspace_path": "c.png", "alt": "Chart",
        }))
        validated = handler().validate_params(params)
        assert validated == params
        precard(validated, caller)
        import hashlib
        assert validated["image_preview"] == {
            "workspace_path": "c.png",
            "asset_name": "c.png",
            "markdown": "![Chart](assets/c.png)",
            "size_bytes": len(PNG),
            "sha256": hashlib.sha256(PNG).hexdigest(),
        }
        last = validated["content_diff"]["lines"][-1]
        assert last == {
            "type": "add", "old_line": None, "new_line": 4,
            "text": "![Chart](assets/c.png)",
        }
        # Nothing stored yet.
        assert list((docs_env.dirs["docs"] / doc["id"] / "assets").iterdir()) == []

    def test_add_image_placement_none_has_no_diff(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "c.png").write_bytes(PNG)
        params = self._suggested(tool("add_doc_image", caller, {
            "doc_id": doc["id"], "workspace_path": "c.png", "placement": "none",
        }))
        validated = precard(handler().validate_params(params), caller)
        assert "content_diff" not in validated
        assert validated["image_preview"]["markdown"] == "![c](assets/c.png)"


# ---------------------------------------------------------------------------
# Pre-card verdicts
# ---------------------------------------------------------------------------


class TestPrecard:
    @pytest.mark.parametrize("make_params, tool_name", [
        (edit_params, "edit_doc"),
        (append_params, "append_to_doc"),
        (image_params, "add_doc_image"),
    ])
    def test_free_doc_rejected(self, docs_env, make_params, tool_name):
        doc = seed_doc(docs_env, content=SHARED_BODY)  # unshared: free
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        (workspace_dir(docs_env, caller) / "c.png").write_bytes(PNG)
        with pytest.raises(ValueError) as exc:
            propose(caller, make_params(doc["id"]))
        assert str(exc.value) == (
            f"This doc is writable directly; call the {tool_name} tool "
            "instead of proposing an action request."
        )

    def test_hidden_doc_is_byte_equal_to_missing(self, docs_env):
        doc = shared_doc(docs_env)
        carol = make_caller(docs_env, who="carol")  # no relationship
        with pytest.raises(ValueError) as hidden:
            propose(carol, append_params(doc["id"]))
        assert str(hidden.value) == doc_not_found_message(doc["id"])
        missing_id = str(uuid.uuid4())
        with pytest.raises(ValueError) as missing:
            propose(carol, append_params(missing_id))
        assert str(missing.value) == doc_not_found_message(missing_id)
        assert str(hidden.value).replace(doc["id"], "X") == str(missing.value).replace(missing_id, "X")

    def test_public_project_conversation_never_sees_private_doc(self, docs_env):
        # is_public is derived from the project row, like the turn does.
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env, project=docs_env.public_project)
        with pytest.raises(ValueError) as exc:
            propose(caller, append_params(doc["id"]))
        assert str(exc.value) == doc_not_found_message(doc["id"])

    def test_project_doc_only_from_its_project(self, docs_env):
        doc = shared_doc(docs_env, project_id=docs_env.private_project)
        inside = make_caller(docs_env, project=docs_env.private_project)
        params = propose(inside, append_params(doc["id"]))
        assert params["doc_scope"] == "project"
        for caller in (make_caller(docs_env),
                       make_caller(docs_env, project=docs_env.other_project)):
            with pytest.raises(ValueError) as exc:
                propose(caller, append_params(doc["id"]))
            assert str(exc.value) == doc_not_found_message(doc["id"])

    def test_unread_doc_for_edit(self, docs_env):
        doc = shared_doc(docs_env)
        with pytest.raises(ValueError, match="has not been read in this conversation"):
            propose(make_caller(docs_env), edit_params(doc["id"]))

    def test_public_doc_from_private_conversation(self, docs_env):
        # Public docs are public-project docs (user docs are always
        # private): every private conversation is outside that project, so
        # it never sees one (the pre-card derives is_public from the
        # project, so a "private caller in the public project" cannot occur).
        doc = shared_doc(
            docs_env, mode="public", permission="write", project_id=docs_env.public_project,
        )
        for caller in (make_caller(docs_env),
                       make_caller(docs_env, project=docs_env.private_project)):
            for params in (edit_params(doc["id"]), append_params(doc["id"])):
                with pytest.raises(ValueError) as exc:
                    propose(caller, params)
                assert str(exc.value) == doc_not_found_message(doc["id"])

    def test_public_doc_never_needs_a_card(self, docs_env):
        # From its own public project the doc is written directly (or is a
        # read-only share): write_doc never applies to a public doc.
        doc = shared_doc(
            docs_env, mode="public", permission="write", project_id=docs_env.public_project,
        )
        caller = make_caller(docs_env, project=docs_env.public_project, public=True)
        with pytest.raises(ValueError, match="writable directly"):
            propose(caller, append_params(doc["id"]))

    def test_stale_old_string(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        with pytest.raises(ValueError, match="old_string not found in the doc content"):
            propose(caller, edit_params(doc["id"], old="delta"))

    def test_read_share_recipient_denied(self, docs_env):
        doc = shared_doc(docs_env, permission="read")
        with pytest.raises(ValueError) as exc:
            propose(make_caller(docs_env, who="bob"), append_params(doc["id"]))
        assert str(exc.value) == DENY_READ_ONLY_SHARE

    def test_write_share_recipient_accepted(self, docs_env):
        doc = shared_doc(docs_env, permission="write")
        params = propose(make_caller(docs_env, who="bob"), append_params(doc["id"]))
        assert params["current_title"] == "Notes" and "content_diff" in params

    def test_gate_closed(self, docs_env):
        doc = shared_doc(docs_env)
        docs_env.fg.set_feature_enabled(docs_env.fg.FEATURE_DOCS, False)
        with pytest.raises(ValueError) as exc:
            propose(make_caller(docs_env), append_params(doc["id"]))
        assert str(exc.value) == docs_disabled_message()

    def test_require_approval_doc_gets_a_card_without_shares(self, docs_env):
        """Rule 7: the owner's unshared doc is proposable once the switch is
        on; the pre-card injects the flag (and an empty share summary) so
        the card can say why approval is needed."""
        doc = seed_doc(docs_env, content=SHARED_BODY)
        _run(docs_env.doc_store.update_doc_metadata(doc["id"], require_approval=True))
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        params = propose(caller, edit_params(doc["id"]))
        assert params["require_approval"] is True
        assert params["share_summary"] == ""
        assert params["content_diff"]["added"] == 1
        # A model-supplied copy never survives validate_params.
        assert "require_approval" not in handler().validate_params(
            {**edit_params(doc["id"]), "require_approval": True},
        )

    def test_share_summary(self):
        from chat.action_request_types.doc_precard import share_summary

        assert share_summary([]) == ""
        assert share_summary([{"user_id": None}]) == "shared with everyone"
        assert share_summary([{"user_id": 2}]) == "shared with 1 user"
        assert share_summary([{"user_id": 2}, {"user_id": 3}]) == "shared with 2 users"
        assert share_summary([{"user_id": None}, {"user_id": 3}]) == (
            "shared with everyone and 1 user"
        )


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


class TestExecute:
    def _assert_written(self, env, doc, *, request_id, expected_body):
        from chat.docs import files

        alice = env.users["alice"]["id"]
        assert body(doc["id"]) == expected_body
        row = _run(env.doc_store.get_doc(doc["id"]))
        assert row["last_write_source"] == f"action_request:{request_id}"
        assert row["updated_at"] > doc["updated_at"]
        assert row["content_size"] == len(expected_body.encode())
        revisions = files.list_revisions(doc["id"])
        assert [r.read_text() for r in revisions] == [SHARED_BODY]
        types = [(uid, ev["type"]) for uid, ev in env.published]
        assert (alice, "doc_changed") in types and (alice, "doc_list_changed") in types
        return row

    def test_edit(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        params = propose(caller, edit_params(doc["id"]))
        docs_env.published.clear()
        result = execute(params, caller, request_id=41)
        self._assert_written(
            docs_env, doc, request_id=41, expected_body="ALPHA beta\ngamma\n",
        )
        assert result["replaced"] == 1 and result["total_lines"] == 2
        assert (result["doc_id"], result["operation"], result["title"]) == (
            doc["id"], "edit", "Notes",
        )

    def test_append(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        params = propose(caller, append_params(doc["id"], content="## Day 2\nmore"))
        docs_env.published.clear()
        result = execute(params, caller, request_id=42)
        self._assert_written(
            docs_env, doc, request_id=42,
            expected_body=SHARED_BODY + "\n## Day 2\nmore\n",
        )
        assert result["appended_lines"] == 2 and result["operation"] == "append"

    def test_add_image(self, docs_env):
        from chat.docs import files

        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "c.png").write_bytes(PNG)
        params = propose(caller, image_params(doc["id"], alt="Chart"))
        docs_env.published.clear()
        result = execute(params, caller, request_id=43)
        row = self._assert_written(
            docs_env, doc, request_id=43,
            expected_body=SHARED_BODY + "\n![Chart](assets/c.png)\n",
        )
        assert row["asset_count"] == 1
        assert files.read_asset(doc["id"], "c.png") == PNG
        assert result["asset"] == "c.png" and result["operation"] == "add_image"

    def test_write_share_recipient(self, docs_env):
        doc = shared_doc(docs_env, permission="write")
        bob = make_caller(docs_env, who="bob")
        params = propose(bob, append_params(doc["id"], content="from bob"))
        execute(params, bob, request_id=44)
        assert body(doc["id"]) == SHARED_BODY + "\nfrom bob\n"

    def test_without_request_id(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        params = propose(caller, append_params(doc["id"]))
        _run(handler().execute(params, caller.user, conversation_id=caller.conversation_id))
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["last_write_source"] == "action_request"

    def test_toctou_stale_old_string(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        params = propose(caller, edit_params(doc["id"]))
        # Someone else changes the doc while the card sits open.
        _run(svc().apply_write_operation(
            caller, doc["id"], "edit", {"old_string": "alpha", "new_string": "omega"},
            write_source="ui", bypass_approval=True,
        ))
        with pytest.raises(RuntimeError, match="old_string not found in the doc content"):
            execute(params, caller)
        assert body(doc["id"]) == "omega beta\ngamma\n"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["last_write_source"] == "ui"

    def test_share_downgraded_at_approve_refuses(self, docs_env):
        doc = shared_doc(docs_env, permission="write")
        bob = make_caller(docs_env, who="bob")
        params = propose(bob, append_params(doc["id"]))
        _run(docs_env.doc_store.add_share(doc["id"], docs_env.users["bob"]["id"], "read"))
        with pytest.raises(RuntimeError) as exc:
            execute(params, bob)
        assert str(exc.value) == DENY_READ_ONLY_SHARE
        assert body(doc["id"]) == SHARED_BODY
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["last_write_source"] is None

    def test_legacy_public_user_doc_is_still_private_at_approve(self, docs_env):
        """A user doc's stored mode cannot make it public (defensive: a
        pre-migration row): the approved change still applies."""
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        params = propose(caller, append_params(doc["id"]))
        make_legacy_public(docs_env, doc["id"])
        execute(params, caller, request_id=46)
        assert body(doc["id"]) == SHARED_BODY + "\nentry\n"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["last_write_source"] == "action_request:46"

    def test_shares_removed_still_applies(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        params = propose(caller, append_params(doc["id"]))
        for share in _run(docs_env.doc_store.list_shares(doc["id"])):
            assert _run(docs_env.doc_store.remove_share(doc["id"], share["id"]))
        execute(params, caller, request_id=45)
        assert body(doc["id"]) == SHARED_BODY + "\nentry\n"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["last_write_source"] == "action_request:45"

    def test_gate_closed_at_approve(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        params = propose(caller, append_params(doc["id"]))
        docs_env.fg.set_feature_enabled(docs_env.fg.FEATURE_DOCS, False)
        with pytest.raises(RuntimeError) as exc:
            execute(params, caller)
        assert str(exc.value) == docs_disabled_message()
        assert body(doc["id"]) == SHARED_BODY

    def test_deleted_doc(self, docs_env):
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        params = propose(caller, append_params(doc["id"]))
        assert _run(docs_env.doc_store.delete_doc(doc["id"]))
        with pytest.raises(RuntimeError) as exc:
            execute(params, caller)
        assert str(exc.value) == doc_not_found_message(doc["id"])

    @pytest.mark.parametrize("params", [
        {}, {"operation": "replace", "doc_id": "d"}, {"operation": "edit"},
        {"operation": ["edit"], "doc_id": "d"},
    ])
    def test_malformed_params(self, docs_env, params):
        with pytest.raises(RuntimeError, match="Malformed write_doc request"):
            execute(params, make_caller(docs_env))


# ---------------------------------------------------------------------------
# Preview, snippet, labels, registration
# ---------------------------------------------------------------------------


def _preview(params):
    return _run(handler().render_preview(params, None))


class TestPreviewAndLabels:
    def test_require_approval_line(self):
        fields = _preview({
            **edit_params("d"), "current_title": "Notes", "require_approval": True,
        })
        assert {"key": "Approval", "value": "required by the owner for every change"} in fields
        fields = _preview({**edit_params("d"), "current_title": "Notes"})
        assert all(f["key"] != "Approval" for f in fields)

    DIFF = {
        "added": 1, "removed": 1,
        "lines": [
            {"type": "del", "old_line": 1, "new_line": None, "text": "a"},
            {"type": "add", "old_line": None, "new_line": 1, "text": "b"},
        ],
    }
    ENRICHED = {
        "current_title": "Notes", "doc_mode": "private", "doc_scope": "user",
        "share_summary": "shared with 2 users",
    }

    def test_edit_fields_and_diff_match_edit_skill(self):
        from chat.action_request_types.edit_skill import EditSkillHandler

        fields = _preview({
            **edit_params("d", replace_all=True), **self.ENRICHED, "content_diff": self.DIFF,
        })
        assert fields[:5] == [
            {"key": "Doc", "value": "Notes"},
            {"key": "Mode", "value": "private"},
            {"key": "Scope", "value": "user"},
            {"key": "Shares", "value": "shared with 2 users"},
            {"key": "Operation", "value": "Edit (replace all)"},
        ]
        skill_fields = _run(EditSkillHandler().render_preview({
            "skill_id": "s", "old_string": "a", "new_string": "b", "content_diff": self.DIFF,
        }))
        skill_diff = next(f for f in skill_fields if f.get("type") == "skill_content_diff")
        assert fields[5] == skill_diff == {
            "key": "Content", "value": "+1 / -1 line(s)",
            "type": "skill_content_diff", "diff": self.DIFF,
        }
        assert len(fields) == 6

    def test_append_fields(self):
        fields = _preview({**append_params("d"), **self.ENRICHED, "content_diff": self.DIFF})
        assert [f["key"] for f in fields] == ["Doc", "Mode", "Scope", "Shares", "Operation", "Content"]
        assert fields[4]["value"] == "Append"
        assert fields[5]["type"] == "skill_content_diff"

    def test_fallbacks_without_injected_keys(self):
        assert _preview(edit_params("d1")) == [
            {"key": "Doc", "value": "d1"},
            {"key": "Operation", "value": "Edit"},
            {"key": "Replace", "value": "alpha"},
            {"key": "With", "value": "ALPHA"},
        ]
        assert _preview(append_params("d1"))[-1] == {"key": "Append", "value": "entry"}
        assert _preview(image_params("d1"))[2:] == [
            {"key": "Image", "value": "c.png"},
            {"key": "Placement", "value": "Append to the end of the doc"},
        ]

    def test_add_image_fields(self):
        image = {
            "workspace_path": "charts/c.png", "asset_name": "c.png",
            "markdown": "![Chart](assets/c.png)", "size_bytes": 2048,
        }
        fields = _preview({
            **image_params("d", "charts/c.png", placement="append"), **self.ENRICHED,
            "image_preview": image, "content_diff": self.DIFF,
        })
        assert [f["key"] for f in fields] == [
            "Doc", "Mode", "Scope", "Shares", "Operation", "Image", "Placement", "Content",
        ]
        assert fields[4]["value"] == "Add image"
        assert fields[5] == {
            "key": "Image", "value": "c.png (2.0 KB)", "type": "doc_image", "image": image,
        }
        none = _preview({
            **image_params("d", placement="none"), **self.ENRICHED, "image_preview": image,
        })
        assert [f["key"] for f in none][-2:] == ["Image", "Placement"]
        assert none[-1]["value"] == "Store only (not placed in the body)"

    @pytest.mark.parametrize("params", [
        {},
        {"operation": ["edit"]},
        {"operation": "zzz", "doc_id": 5},
        {"operation": "add_image", "image_preview": "junk", "content_diff": "junk"},
        {"operation": "add_image", "image_preview": {"asset_name": "x.png", "size_bytes": "big"}},
        {"operation": "edit", "content_diff": {"lines": "nope"}},
        {"operation": "append", "current_title": 7, "doc_mode": ["private"]},
    ])
    def test_odd_params_never_raise_and_values_are_strings(self, params):
        fields = _preview(params)
        assert fields and all(isinstance(f["value"], str) for f in fields)

    def test_values_are_strings_on_real_previews(self, docs_env):
        # PreviewField.value is a string on the FE; structured payloads ride
        # on separate typed keys (diff / image).
        doc = shared_doc(docs_env)
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "c.png").write_bytes(PNG)
        read(caller, doc["id"])
        for params in (edit_params(doc["id"]), append_params(doc["id"]), image_params(doc["id"])):
            fields = _preview(propose(caller, params))
            assert all(isinstance(f["value"], str) for f in fields)
            json.dumps(fields)

    @pytest.mark.parametrize("params, expected", [
        ({**edit_params("d"), "current_title": "Notes"}, "Edit 'Notes'"),
        ({**append_params("d"), "current_title": "Notes"}, "Append to 'Notes'"),
        ({**image_params("d"), "current_title": "Notes"}, "Add image to 'Notes'"),
        (append_params("d-1"), "Append to 'd-1'"),
        ({"operation": "append"}, "Append to"),
        ({"doc_id": "d-1"}, "'d-1'"),
        ({"operation": ["x"]}, ""),
        ({}, ""),
    ])
    def test_summary_snippet(self, params, expected):
        assert handler().summary_snippet(params) == expected

    def test_registry_snippet_and_labels(self):
        from chat.action_request_types import get_summary_snippet

        h = handler()
        assert (h.display_name, h.approve_label, h.resolved_label) == (
            "Write Doc", "Apply", "Applied",
        )
        assert get_summary_snippet(
            "write_doc", {**edit_params("d"), "current_title": "Notes"},
        ) == "Edit 'Notes'"

    def test_registration(self):
        from chat.action_request_types import get_handler
        from chat.action_request_types.write_doc import WriteDocHandler
        from chat.llm.tool_schemas import ACTION_REQUEST_TYPE_ENUM
        from db.models import ActionRequestType

        assert isinstance(get_handler("write_doc"), WriteDocHandler)
        assert get_handler("write_doc").type_name == ActionRequestType.WRITE_DOC
        assert "write_doc" in ACTION_REQUEST_TYPE_ENUM


# ---------------------------------------------------------------------------
# End to end: create_action_request arm -> card -> resolve route
# ---------------------------------------------------------------------------


@pytest.fixture()
def ar_env(docs_env, monkeypatch):
    """docs_env + the action-request / wait-handle stores on the same DB."""
    import db.action_request_store as action_request_store
    import db.tool_wait_handle_store as tool_wait_handle_store
    from chat.storage import ChatStorage
    from chat.wait_handles import resume as wait_resume

    session_local = docs_env.doc_store.AsyncSessionLocal
    for mod in (action_request_store, tool_wait_handle_store):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(
        ChatStorage, "update_action_request_message",
        staticmethod(lambda **kwargs: None),
    )
    kicked: list = []
    monkeypatch.setattr(
        wait_resume, "maybe_kick_resume", lambda *args, **kwargs: kicked.append(args),
    )
    docs_env.action_request_store = action_request_store
    docs_env.kicked = kicked
    return docs_env


def _arm(caller, params):
    from chat.gemini_api.turn_tools import TurnState, _handle_create_action_request
    from tests.test_turn_tools_registry import _call, _make_ctx

    ctx, events = _make_ctx(
        user=caller.user, conversation_id=caller.conversation_id,
        project_id=caller.project_id,
    )
    call = _call("create_action_request", {
        "request_type": "write_doc", "params": params, "reasoning": "shared doc",
    })
    return ctx, events, _run(_handle_create_action_request(ctx, call, TurnState()))


class TestEndToEnd:
    def test_arm_rejects_free_and_hidden_docs(self, docs_env):
        free = seed_doc(docs_env, title="Mine")
        result = json.loads(_arm(make_caller(docs_env), append_params(free["id"]))[2])
        assert result == {"error": (
            "Invalid parameters: This doc is writable directly; call the "
            "append_to_doc tool instead of proposing an action request."
        )}
        shared = shared_doc(docs_env)
        result = json.loads(_arm(make_caller(docs_env, who="carol"), append_params(shared["id"]))[2])
        assert result == {"error": f"Invalid parameters: {doc_not_found_message(shared['id'])}"}

    def test_card_then_approve(self, ar_env):
        from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request
        from chat.gemini_api.turn_tools import SuspendForActionRequest
        from db import conversation_store

        doc = shared_doc(ar_env)
        caller = make_caller(ar_env)
        _run(conversation_store.create_conversation(
            user_id=caller.user["id"], conversation_id=caller.conversation_id,
            created_at=datetime.now(timezone.utc),
        ))
        payload = tool("append_to_doc", caller, {"doc_id": doc["id"], "content": "approved line"})

        with pytest.raises(SuspendForActionRequest) as suspended:
            _arm(caller, payload["suggested_request"]["params"])
        request_id = suspended.value.request_id

        row = _run(ar_env.action_request_store.get_action_request(caller.user["id"], request_id))
        assert row["request_type"] == "write_doc" and row["status"] == "open"
        assert row["params"]["current_title"] == "Notes"
        assert row["params"]["share_summary"] == "shared with 1 user"
        assert "content_diff" in row["params"]
        assert body(doc["id"]) == SHARED_BODY

        class _Req:
            app = None

        resolved = _run(resolve_user_action_request(
            request_id, ResolveRequestBody(action="execute"), _Req(), caller.user,
        ))
        assert resolved["status"] == "executed"
        assert resolved["result"]["title"] == "Notes"
        assert resolved["summary_snippet"] == "Append to 'Notes'"
        assert resolved["resolved_label"] == "Applied"
        assert body(doc["id"]) == SHARED_BODY + "\napproved line\n"
        doc_row = _run(ar_env.doc_store.get_doc(doc["id"]))
        assert doc_row["last_write_source"] == f"action_request:{request_id}"

    def test_approve_failure_keeps_request_open(self, ar_env, monkeypatch):
        from fastapi import HTTPException

        from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request
        from chat.docs import constants
        from chat.gemini_api.turn_tools import SuspendForActionRequest

        doc = shared_doc(ar_env)
        caller = make_caller(ar_env)
        with pytest.raises(SuspendForActionRequest) as suspended:
            _arm(caller, append_params(doc["id"]))
        request_id = suspended.value.request_id
        # The change no longer fits at approve time (the cap stands in for
        # a doc that grew while the card sat open).
        cap = len(SHARED_BODY) + 2
        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", cap)

        class _Req:
            app = None

        with pytest.raises(HTTPException) as exc:
            _run(resolve_user_action_request(
                request_id, ResolveRequestBody(action="execute"), _Req(), caller.user,
            ))
        assert exc.value.status_code == 500
        assert f"over the {cap}-byte limit" in exc.value.detail["message"]
        row = _run(ar_env.action_request_store.get_action_request(caller.user["id"], request_id))
        assert row["status"] == "open"
        assert body(doc["id"]) == SHARED_BODY
