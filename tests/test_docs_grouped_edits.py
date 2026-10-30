"""Grouped doc edits: ``edit_doc(edits=[...])`` and ``write_doc``
``operation: "multi_edit"``.

Runs against the isolated ``docs_env`` fixture (tests/test_docs_service.py).
Covers:

1. ``apply_content_edits``: every old_string matched against the starting
   content (order-independent), replace_all counts, overlap refusal, all
   failing edits reported at once, the empty / size checks.
2. The tool: a free doc gets one write, one History version and the
   ``edits_applied`` result; ``edits`` and ``old_string`` together, the
   cap, a long note, an unread doc and a JSON-string ``edits`` argument.
3. The approval handoff: the ``approval_required`` payload round-trips
   through ``validate_params``, the pre-card injects ``edit_previews``
   (one bounded diff per edit, old-side line numbers of the live body)
   and no ``content_diff``.
4. ``validate_params`` on the handler: item keys, the cap, position-
   prefixed errors, ``edit_previews`` dropped when the model echoes it.
5. ``execute``: all edits land as one write attributed to the request; a
   stale edit fails the approve naming its position, body untouched.
6. ``render_preview`` / ``summary_snippet`` and the skill text.
"""

import asyncio
import json

import pytest

from chat.docs.constants import DOC_EDIT_NOTE_MAX_LEN, DOC_MAX_EDITS_PER_CALL
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    body,
    docs_env,
    make_caller,
    seed_doc,
    svc,
)
from tests.test_write_doc_action_request import (
    execute,
    handler,
    propose,
    read,
    tool,
)

BODY = "# Title\n\nalpha beta\ngamma\ndelta alpha\n\nomega\n"


def _run(coro):
    return asyncio.run(coro)


def edit(old, new, **extra):
    return {"old_string": old, "new_string": new, **extra}


THREE = [
    edit("gamma", "GAMMA", note="Shout gamma"),
    edit("omega", "OMEGA"),
    edit("alpha", "A", replace_all=True),
]
THREE_RESULT = "# Title\n\nA beta\nGAMMA\ndelta A\n\nOMEGA\n"


def multi_params(doc_id, edits=THREE):
    return {"operation": "multi_edit", "doc_id": doc_id, "edits": edits}


# ---------------------------------------------------------------------------
# apply_content_edits
# ---------------------------------------------------------------------------


class TestApplyContentEdits:
    def _apply(self, content, edits):
        from chat.action_request_types._skill_content_edit import apply_content_edits
        return apply_content_edits(content, edits, noun="doc", reread_hint="re-read it")

    def test_independent_of_order_and_counts(self):
        new, counts = self._apply(BODY, THREE)
        assert new == THREE_RESULT and counts == [1, 1, 2]
        reordered, counts2 = self._apply(BODY, list(reversed(THREE)))
        assert reordered == new and counts2 == [2, 1, 1]

    def test_matches_the_starting_content_not_an_earlier_result(self):
        # The second edit targets text the first one removes: both match
        # the starting body, so they overlap instead of chaining.
        with pytest.raises(ValueError) as exc:
            self._apply(BODY, [edit("gamma", "GAMMA"), edit("GAMMA", "x")])
        assert "edits[1]: old_string not found" in str(exc.value)

    def test_overlap_refused(self):
        with pytest.raises(ValueError) as exc:
            self._apply(BODY, [edit("alpha beta", "x"), edit("beta\ngamma", "y")])
        msg = str(exc.value)
        assert msg.startswith("edits[0] and edits[1] match overlapping text")
        assert "merge the two into one edit" in msg
        # The same text twice is an overlap too, reported once.
        with pytest.raises(ValueError) as exc:
            self._apply(BODY, [edit("gamma", "x"), edit("gamma", "y")])
        assert str(exc.value).count("overlapping") == 1

    def test_every_failure_reported_at_once(self):
        with pytest.raises(ValueError) as exc:
            self._apply(BODY, [edit("alpha", "x"), edit("gamma", "y"), edit("zzz", "z")])
        lines = str(exc.value).split("\n")
        assert lines[0].startswith("edits[0]: old_string appears 2 times")
        assert lines[1].startswith("edits[2]: old_string not found")
        assert len(lines) == 2

    def test_empty_and_oversized_results(self):
        with pytest.raises(ValueError, match="empty"):
            self._apply("abc\n", [edit("abc", ""), edit("\n", "")])
        from chat.action_request_types._skill_content_edit import apply_content_edits
        with pytest.raises(ValueError, match="maximum size"):
            apply_content_edits("abc", [edit("b", "x" * 10)], max_size=8)


# ---------------------------------------------------------------------------
# The tool on a free doc
# ---------------------------------------------------------------------------


class TestEditDocTool:
    def test_grouped_edit_is_one_write(self, docs_env):
        from chat.docs import files

        doc = seed_doc(docs_env, content=BODY)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        docs_env.published.clear()
        result = tool("edit_doc", caller, {"doc_id": doc["id"], "edits": THREE})
        assert result == {
            "edits_applied": 3, "replaced": 4, "total_lines": 7,
            "updated_at": result["updated_at"],
        }
        assert body(doc["id"]) == THREE_RESULT
        # One History version, one pair of events.
        assert [r.read_text() for r in files.list_revisions(doc["id"])] == [BODY]
        assert [ev["type"] for _, ev in docs_env.published] == [
            "doc_changed", "doc_list_changed",
        ]
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["last_write_source"] == f"conversation:{caller.conversation_id}"

    def test_failure_leaves_the_doc_untouched_and_names_every_edit(self, docs_env):
        doc = seed_doc(docs_env, content=BODY)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        result = tool("edit_doc", caller, {
            "doc_id": doc["id"],
            "edits": [edit("gamma", "x"), edit("nope", "y"), edit("alpha", "z")],
        })
        assert "edits[1]: old_string not found in the doc content" in result["error"]
        assert "re-read it with read_doc" in result["error"]
        assert "edits[2]: old_string appears 2 times" in result["error"]
        assert body(doc["id"]) == BODY

    def test_argument_shapes(self, docs_env):
        doc = seed_doc(docs_env, content=BODY)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        both = tool("edit_doc", caller, {
            "doc_id": doc["id"], "old_string": "gamma", "new_string": "x",
            "edits": [edit("omega", "y")],
        })
        assert both["error"].startswith("Pass either old_string / new_string")
        assert tool("edit_doc", caller, {"doc_id": doc["id"], "edits": []})["error"] == (
            "edits must be a non-empty list of {old_string, new_string} objects."
        )
        assert tool("edit_doc", caller, {"doc_id": doc["id"], "edits": "oops"})["error"] == (
            "edits must be a list."
        )
        too_many = [edit(f"w{i}", "x") for i in range(DOC_MAX_EDITS_PER_CALL + 1)]
        result = tool("edit_doc", caller, {"doc_id": doc["id"], "edits": too_many})
        assert f"the maximum per call is {DOC_MAX_EDITS_PER_CALL}" in result["error"]
        result = tool("edit_doc", caller, {
            "doc_id": doc["id"], "edits": [edit("gamma", "x", note="n" * (DOC_EDIT_NOTE_MAX_LEN + 1))],
        })
        assert result["error"] == f"edits[0]: note must be at most {DOC_EDIT_NOTE_MAX_LEN} characters."
        result = tool("edit_doc", caller, {"doc_id": doc["id"], "edits": [edit("", "x")]})
        assert result["error"] == "edits[0]: old_string must not be empty."
        result = tool("edit_doc", caller, {"doc_id": doc["id"], "edits": ["gamma"]})
        assert result["error"] == "edits[0] must be an object."
        # A provider that stringifies nested arguments.
        result = tool("edit_doc", caller, {
            "doc_id": doc["id"], "edits": json.dumps([edit("gamma", "GAMMA")]),
        })
        assert result["edits_applied"] == 1 and "GAMMA" in body(doc["id"])

    def test_requires_a_prior_read(self, docs_env):
        doc = seed_doc(docs_env, content=BODY)
        caller = make_caller(docs_env)
        result = tool("edit_doc", caller, {"doc_id": doc["id"], "edits": THREE})
        assert "has not been read in this conversation" in result["error"]
        assert body(doc["id"]) == BODY


# ---------------------------------------------------------------------------
# The approval handoff and the pre-card
# ---------------------------------------------------------------------------


def shared(env, **kwargs):
    return seed_doc(env, content=BODY, shares=[("bob", "read")], **kwargs)


class TestHandoff:
    def test_payload_round_trips_and_precard_injects_previews(self, docs_env):
        doc = shared(docs_env)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        result = tool("edit_doc", caller, {"doc_id": doc["id"], "edits": THREE})
        assert result["error"] == "approval_required"
        suggested = result["suggested_request"]
        assert suggested == {
            "request_type": "write_doc",
            "params": {
                "operation": "multi_edit", "doc_id": doc["id"],
                "edits": [
                    {"old_string": "gamma", "new_string": "GAMMA", "replace_all": False, "note": "Shout gamma"},
                    {"old_string": "omega", "new_string": "OMEGA", "replace_all": False, "note": ""},
                    {"old_string": "alpha", "new_string": "A", "replace_all": True, "note": ""},
                ],
            },
        }
        assert body(doc["id"]) == BODY
        validated = handler().validate_params(suggested["params"])
        assert validated == suggested["params"]

        params = propose(caller, dict(suggested["params"]))
        assert "content_diff" not in params
        previews = params["edit_previews"]
        assert [(p["index"], p["note"], p["replace_all"], p["replaced"]) for p in previews] == [
            (1, "Shout gamma", False, 1), (2, "", False, 1), (3, "", True, 2),
        ]
        # Each diff is the live body vs that edit alone, old-side numbers
        # as read_doc counts them (gamma is line 4; alpha on lines 3 and 5).
        first = previews[0]["diff"]
        assert (first["added"], first["removed"]) == (1, 1)
        assert [(l["type"], l["old_line"]) for l in first["lines"] if l["type"] == "del"] == [("del", 4)]
        assert first["total_old_lines"] == 7
        third = previews[2]["diff"]
        assert [l["old_line"] for l in third["lines"] if l["type"] == "del"] == [3, 5]
        assert params["current_title"] == "Notes" and params["share_summary"] == "shared with 1 user"

    def test_precard_dry_run_refuses_a_bad_group(self, docs_env):
        doc = shared(docs_env)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        with pytest.raises(ValueError) as exc:
            propose(caller, multi_params(doc["id"], [edit("gamma", "x"), edit("gamma", "y")]))
        assert "match overlapping text" in str(exc.value)

    def test_free_doc_is_rejected_at_precard(self, docs_env):
        doc = seed_doc(docs_env, content=BODY)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        with pytest.raises(ValueError, match="call the edit_doc tool instead"):
            propose(caller, multi_params(doc["id"]))


class TestValidateParams:
    def test_item_keys_and_defaults(self):
        out = handler().validate_params(multi_params("d1", [edit("a", "b")]))
        assert out == {
            "operation": "multi_edit", "doc_id": "d1",
            "edits": [{"old_string": "a", "new_string": "b", "replace_all": False, "note": ""}],
        }

    @pytest.mark.parametrize("edits, message", [
        ([], "edits must be a non-empty list"),
        ("x", "edits must be a non-empty list"),
        ([edit("a", "b", extra=1)], "edits[0]: Unknown parameter"),
        ([edit("a", "b"), edit("", "b")], "edits[1]: old_string must be a non-empty string."),
        ([edit("a", "a")], "edits[0]: old_string and new_string are identical"),
        ([edit("a", "b", note="n" * (DOC_EDIT_NOTE_MAX_LEN + 1))], "edits[0]: note must be at most"),
        (["a"], "edits[0] must be an object."),
        ([edit(f"w{i}", "x") for i in range(DOC_MAX_EDITS_PER_CALL + 1)],
         f"the maximum per request is {DOC_MAX_EDITS_PER_CALL}"),
    ])
    def test_rejections(self, edits, message):
        with pytest.raises(ValueError) as exc:
            handler().validate_params(multi_params("d1", edits))
        assert message in str(exc.value)

    def test_other_operations_keys_and_echoed_previews(self):
        with pytest.raises(ValueError, match="Unknown parameter"):
            handler().validate_params({**multi_params("d1"), "old_string": "a"})
        out = handler().validate_params({**multi_params("d1"), "edit_previews": [1]})
        assert "edit_previews" not in out


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


class TestExecute:
    def test_applies_the_group_as_one_write(self, docs_env):
        from chat.docs import files

        doc = shared(docs_env)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        params = propose(caller, multi_params(doc["id"]))
        docs_env.published.clear()
        result = execute(params, caller, request_id=51)
        assert body(doc["id"]) == THREE_RESULT
        assert result["edits_applied"] == 3 and result["replaced"] == 4
        assert (result["operation"], result["title"]) == ("multi_edit", "Notes")
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["last_write_source"] == "action_request:51"
        assert [r.read_text() for r in files.list_revisions(doc["id"])] == [BODY]

    def test_stale_edit_fails_the_approve(self, docs_env):
        doc = shared(docs_env)
        caller = make_caller(docs_env)
        read(caller, doc["id"])
        params = propose(caller, multi_params(doc["id"]))
        # The doc moves under the open card: edits[1]'s text is gone.
        from chat.docs import files
        files.write_body(doc["id"], BODY.replace("omega", "OMEGA already"), write_source="ui")
        with pytest.raises(RuntimeError) as exc:
            execute(params, caller, request_id=52)
        assert "edits[1]: old_string not found" in str(exc.value)
        assert "omega" not in body(doc["id"]) or "OMEGA already" in body(doc["id"])
        assert "GAMMA" not in body(doc["id"])


# ---------------------------------------------------------------------------
# Preview, labels, prompting
# ---------------------------------------------------------------------------


def _preview(params):
    return _run(handler().render_preview(params, None))


class TestPreviewAndLabels:
    PREVIEWS = [
        {"index": 1, "note": "One", "replace_all": False, "replaced": 1,
         "diff": {"added": 1, "removed": 1, "lines": [], "total_old_lines": 7, "total_new_lines": 7}},
        {"index": 2, "note": "", "replace_all": True, "replaced": 2,
         "diff": {"added": 2, "removed": 2, "lines": [], "total_old_lines": 7, "total_new_lines": 7}},
    ]

    def test_group_field(self):
        fields = _preview({
            **multi_params("d", [edit("a", "b"), edit("c", "d", replace_all=True)]),
            "current_title": "Notes", "edit_previews": self.PREVIEWS,
        })
        assert fields[0] == {"key": "Doc", "value": "Notes"}
        assert {"key": "Operation", "value": "Edit"} in fields
        assert fields[-1] == {
            "key": "Changes", "value": "2 changes, +3 / -3 line(s)",
            "type": "doc_edit_group", "edits": self.PREVIEWS,
        }

    def test_fallback_without_previews(self):
        fields = _preview(multi_params("d", [edit("a", "b", note="Why"), edit("c", "")]))
        assert fields[-4:] == [
            {"key": "Replace 1 (Why)", "value": "a"},
            {"key": "With 1", "value": "b"},
            {"key": "Replace 2", "value": "c"},
            {"key": "With 2", "value": ""},
        ]

    def test_snippet(self):
        h = handler()
        assert h.summary_snippet({**multi_params("d"), "current_title": "Notes"}) == (
            "Edit 'Notes' (3 changes)"
        )
        assert h.summary_snippet(multi_params("d", [edit("a", "b")])) == "Edit 'd' (1 change)"

    def test_skill_and_tool_description(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        from chat.system_skills import CATALOG

        content = CATALOG["system:quest_docs"].content_builder("http://x", "k")
        assert "edit_doc(doc_id, edits=[...])" in content
        assert f"up to {DOC_MAX_EDITS_PER_CALL} objects" in content
        assert '"operation": "multi_edit"' in content
        spec = TOOL_CALL_REGISTRY["edit_doc"]
        assert "edits" in spec["description"]
        assert spec["parameters"]["required"] == ["doc_id"]
        items = spec["parameters"]["properties"]["edits"]["items"]
        assert set(items["properties"]) == {"old_string", "new_string", "replace_all", "note"}
