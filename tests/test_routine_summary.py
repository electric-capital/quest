"""Tests for _routine_summary() in db.schedule_store.

Regression tests ensuring the scheduler daemon receives all required fields
from routine summaries, especially the `model` field (Bug 1 fix: commit 409f8bf).
"""

import types

import pytest

from db.schedule_store import _routine_summary


# ---------------------------------------------------------------------------
# Helper: create a mock Routine with the attributes _routine_summary() reads
# ---------------------------------------------------------------------------

def _make_routine(**overrides) -> types.SimpleNamespace:
    """Return a SimpleNamespace mimicking a Routine ORM instance."""
    defaults = {
        "id": "routine-001",
        "project_id": "proj-001",
        "user_id": 1,
        "name": "Test Routine",
        "prompt": "Run the report",
        "guide_id": "guide-001",
        "model": "gemini-3.1-pro-preview",
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Tests: all scheduler-required fields are present
# ---------------------------------------------------------------------------

class TestRoutineSummaryIncludesAllFields:
    """Verify _routine_summary() includes every field the scheduler needs."""

    def test_model_field_included_when_set(self):
        routine = _make_routine(model="claude-haiku-4.5")
        result = _routine_summary(routine)
        assert "model" in result
        assert result["model"] == "claude-haiku-4.5"

    def test_model_field_included_when_none(self):
        routine = _make_routine(model=None)
        result = _routine_summary(routine)
        assert "model" in result
        assert result["model"] is None

    def test_all_scheduler_required_fields_present(self):
        routine = _make_routine(
            id="routine-123",
            project_id="proj-456",
            user_id=42,
            name="Daily Report",
            prompt="Generate report",
            guide_id="guide-789",
            model="gemini-3.1-pro-preview",
        )
        result = _routine_summary(routine)
        expected_keys = {
            "id", "project_id", "user_id", "name", "prompt", "guide_id", "model",
            # Joined project / owner fields for the public-project routines
            # gate and the archived-project pause.
            "project_public", "project_archived", "user_email",
        }
        assert set(result.keys()) == expected_keys
        assert result["id"] == "routine-123"
        assert result["project_id"] == "proj-456"
        assert result["user_id"] == 42
        assert result["name"] == "Daily Report"
        assert result["prompt"] == "Generate report"
        assert result["guide_id"] == "guide-789"
        assert result["model"] == "gemini-3.1-pro-preview"


# ---------------------------------------------------------------------------
# Tests: field values match routine attributes
# ---------------------------------------------------------------------------

class TestRoutineSummaryFieldValues:
    """Verify returned values exactly match the Routine's attributes."""

    def test_field_values_match_routine_attributes(self):
        routine = _make_routine(
            id="routine-123",
            project_id="proj-456",
            user_id=42,
            name="Daily Report",
            prompt="Generate report",
            guide_id="guide-789",
            model="gemini-3.1-pro-preview",
        )
        result = _routine_summary(routine)
        assert result["id"] == routine.id
        assert result["project_id"] == routine.project_id
        assert result["user_id"] == routine.user_id
        assert result["name"] == routine.name
        assert result["prompt"] == routine.prompt
        assert result["guide_id"] == routine.guide_id
        assert result["model"] == routine.model

    @pytest.mark.parametrize(
        "model_value",
        [
            "gemini-3.1-pro-preview",
            "claude-haiku-4.5",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-sonnet-5",
            "claude-sonnet-5-5",
            "claude-opus-5",
            "claude-opus-5-5",
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-3.7-flash",
            "gemini-3.8-flash",
            None,
        ],
    )
    def test_different_model_values(self, model_value):
        routine = _make_routine(model=model_value)
        result = _routine_summary(routine)
        assert result["model"] == model_value
