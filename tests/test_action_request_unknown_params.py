"""Per-handler tests for the unknown-parameter rejection behaviour.

Every ``handler.validate_params`` runs ``reject_unknown_params`` first
(see ``chat/action_request_types/_param_validation.py``), so an unknown
top-level key fails fast with ``ValueError`` before any required-field
or type-coercion check runs. The dispatch arm at
``chat/gemini_api/conversation.py:1202-1211`` already turns that
``ValueError`` into a same-turn ``{"error": "Invalid parameters: ..."}``
synchronous tool result -- no DB row, no wait handle, no card, no
suspend.

This file covers the core handlers plus the shared helpers (including
the IO-attachments composite-shape validators, which live in core for
plugin handlers to reuse). Plugin handlers pin the
same behaviour in their own suites.
"""

import pytest


# ---------------------------------------------------------------------------
# Helper-level: reject_unknown_params produces the documented message
# format and lists keys deterministically.
# ---------------------------------------------------------------------------


def test_reject_unknown_params_lists_extras_sorted():
    from chat.action_request_types._param_validation import reject_unknown_params

    with pytest.raises(ValueError) as excinfo:
        reject_unknown_params(
            "create_memory",
            {"content": "hi", "zeta": 1, "alpha": 2},
            frozenset({"content"}),
        )
    msg = str(excinfo.value)
    # Sorted listing means alpha appears before zeta in the rendered list.
    assert "['alpha', 'zeta']" in msg
    assert "Unknown parameter for create_memory" in msg
    assert "Allowed parameters: ['content']" in msg


def test_reject_unknown_params_no_extras_returns_silently():
    from chat.action_request_types._param_validation import reject_unknown_params

    # Should not raise.
    reject_unknown_params(
        "create_memory", {"content": "hi"}, frozenset({"content"}),
    )


def test_reject_unknown_params_path_prefix_used_for_nested():
    from chat.action_request_types._param_validation import reject_unknown_params

    with pytest.raises(ValueError) as excinfo:
        reject_unknown_params(
            "attachments[0]",
            {"path": "report.pdf", "file_name": "x"},
            frozenset({"path", "filename"}),
            path="attachments[0]",
        )
    msg = str(excinfo.value)
    # Nested-row format uses the path prefix.
    assert "attachments[0].file_name" in msg


# ---------------------------------------------------------------------------
# Per-handler unknown-field tests. Each test passes a known-valid base
# payload plus a single unknown key, asserts ValueError is raised and
# that the message names the unknown key + the request_type.
# ---------------------------------------------------------------------------


# The Slack handler tests moved to the Slack plugin's suite
# (plugins/slack/tests/test_slack_handlers.py).


def test_create_calendar_invite_rejects_unknown_field():
    from chat.action_request_types.create_calendar_invite import CreateCalendarInviteHandler

    handler = CreateCalendarInviteHandler()
    with pytest.raises(ValueError, match="Unknown parameter for create_calendar_invite"):
        handler.validate_params({
            "summary": "Team",
            "start": "2026-01-01T10:00:00Z",
            "end": "2026-01-01T11:00:00Z",
            "calendar_name": "Primary",  # server-injected
        })


def test_create_memory_rejects_unknown_field():
    from chat.action_request_types.create_memory import CreateMemoryHandler

    handler = CreateMemoryHandler()
    with pytest.raises(ValueError, match="Unknown parameter for create_memory"):
        handler.validate_params({"content": "hi", "tags": ["x"]})


def test_create_skill_rejects_unknown_field():
    from chat.action_request_types.create_skill import CreateSkillHandler

    handler = CreateSkillHandler()
    with pytest.raises(ValueError, match="Unknown parameter for create_skill"):
        handler.validate_params({
            "name": "My Skill",
            "content": "do X",
            "project_id": "p1",  # server/dispatch-supplied, not a model param
        })


def test_edit_skill_rejects_unknown_field():
    from chat.action_request_types.edit_skill import EditSkillHandler

    handler = EditSkillHandler()
    with pytest.raises(ValueError, match="Unknown parameter for edit_skill"):
        handler.validate_params({
            "skill_id": "s1",
            "name": "Renamed",
            "current_skill_name": "Old",  # server-injected; model must not supply
        })


def test_edit_skill_empty_payload_still_rejected():
    from chat.action_request_types.edit_skill import EditSkillHandler

    handler = EditSkillHandler()
    with pytest.raises(ValueError, match="No editable fields provided"):
        handler.validate_params({"skill_id": "s1"})


def test_upload_to_drive_rejects_unknown_field():
    from chat.action_request_types.upload_to_drive import UploadToDriveHandler

    handler = UploadToDriveHandler()
    with pytest.raises(ValueError, match="Unknown parameter for upload_to_drive"):
        handler.validate_params({
            "path": "report.pdf",
            "folder_name": "Reports",  # server-injected; model must not supply
        })


def test_create_drive_folder_rejects_unknown_field():
    from chat.action_request_types.create_drive_folder import CreateDriveFolderHandler

    handler = CreateDriveFolderHandler()
    with pytest.raises(ValueError, match="Unknown parameter for create_drive_folder"):
        handler.validate_params({
            "name": "Reports",
            "parent_folder_name": "Parent",  # server-injected; model must not supply
        })


def test_forward_gmail_message_rejects_unknown_field():
    from chat.action_request_types.forward_gmail_message import ForwardGmailMessageHandler

    handler = ForwardGmailMessageHandler()
    with pytest.raises(ValueError, match="Unknown parameter for forward_gmail_message"):
        handler.validate_params({
            "message_id": "18f3c2a9b7e1d4f0",
            "to": "receipts@example.com",
            "original_subject": "Receipt",  # server-injected; model must not supply
        })


# ---------------------------------------------------------------------------
# Multi-unknown-field deterministic listing.
# ---------------------------------------------------------------------------


def test_create_memory_lists_multiple_unknown_fields_sorted():
    from chat.action_request_types.create_memory import CreateMemoryHandler

    handler = CreateMemoryHandler()
    with pytest.raises(ValueError) as exc:
        handler.validate_params({
            "content": "hi",
            "zoo": 1,
            "apple": 2,
        })
    msg = str(exc.value)
    assert msg.index("apple") < msg.index("zoo")


# ---------------------------------------------------------------------------
# IO attachments -- nested unknown-key tests against the four
# composite-shape validators.
# ---------------------------------------------------------------------------


def test_attachments_param_rejects_unknown_inner_key():
    from chat.action_request_types._io_attachments import validate_attachments_param

    with pytest.raises(ValueError) as exc:
        validate_attachments_param(
            {"attachments": [{"path": "report.pdf", "size": 1234}]},
            "attachments",
        )
    assert "attachments[0].size" in str(exc.value)


def test_link_attachments_param_rejects_unknown_inner_key():
    from chat.action_request_types._io_attachments import (
        reject_unknown_link_attachment_keys,
    )

    with pytest.raises(ValueError) as exc:
        reject_unknown_link_attachment_keys(
            {
                "link_attachments": [
                    {"url": "https://x.com", "name": "X", "format": "html"},
                ],
            },
            "link_attachments",
        )
    assert "link_attachments[0].format" in str(exc.value)


def test_rename_attachments_param_rejects_unknown_inner_key():
    from chat.action_request_types._io_attachments import (
        reject_unknown_rename_attachment_keys,
    )

    with pytest.raises(ValueError) as exc:
        reject_unknown_rename_attachment_keys(
            {
                "rename_attachments": [
                    {"id": 1, "filename": "new.pdf", "old_filename": "old.pdf"},
                ],
            },
            "rename_attachments",
        )
    assert "rename_attachments[0].old_filename" in str(exc.value)


def test_rename_link_attachments_param_rejects_unknown_inner_key():
    from chat.action_request_types._io_attachments import (
        reject_unknown_rename_link_attachment_keys,
    )

    with pytest.raises(ValueError) as exc:
        reject_unknown_rename_link_attachment_keys(
            {
                "rename_link_attachments": [
                    {"id": 1, "name": "New", "is_primary": True},
                ],
            },
            "rename_link_attachments",
        )
    assert "rename_link_attachments[0].is_primary" in str(exc.value)


# ---------------------------------------------------------------------------
# Type-coercion ordering: a typoed required-field name (e.g. `contents`
# instead of `content`) should fail with "Unknown parameter: contents"
# rather than triggering "Missing required parameter: content". Confirms
# reject_unknown_params runs FIRST.
# ---------------------------------------------------------------------------


def test_unknown_field_check_runs_before_required_check():
    from chat.action_request_types.create_memory import CreateMemoryHandler

    handler = CreateMemoryHandler()
    with pytest.raises(ValueError) as exc:
        handler.validate_params({"contents": "hi"})
    msg = str(exc.value)
    # Must surface as unknown-key, not as missing-required.
    assert "Unknown parameter" in msg
    assert "contents" in msg
    assert "Missing required parameter" not in msg
