"""Tests for deprecated-model handling.

gemini-3.1-pro-preview is deprecated: it is hidden from the new-conversation
picker (excluded from get_available_models()) but stays in MODEL_REGISTRY so
existing conversations, routines, and stored defaults keep working. As part of
the deprecation its transport moved from the genapi developer endpoint to the
Gemini Vertex backend.

Registry entries with a ``discontinued_on`` date (the provider's announced
shutdown) turn deprecated on that date automatically.
"""

from datetime import date
from unittest.mock import patch

import pytest

import chat.llm.config as llm_config
from chat.llm.config import (
    MODEL_REGISTRY,
    get_available_models,
    get_backend_for_model,
    get_provider_for_model,
    is_discontinued,
    resolve_model,
)


class _EmptyHealthStore:
    def get_all(self):
        return {}


def _available_with_all_credentials() -> list[str]:
    fake_config = {
        "gemini_vertex": {"vertex_project_id": "proj"},
        "anthropic": {"vertex_project_id": "proj"},
    }
    with patch("config.server_config.load_server_config", return_value=fake_config), \
         patch("chat.llm.health.get_model_health_store", return_value=_EmptyHealthStore()), \
         patch(
             "config.inference_providers.effective_api_key",
             return_value=("sk-or-v1-test", "store"),
         ):
        return get_available_models()


class TestGemini31ProDeprecation:
    def test_still_registered(self):
        """The model stays in the registry so existing rows keep resolving."""
        assert "gemini-3.1-pro-preview" in MODEL_REGISTRY
        assert get_provider_for_model("gemini-3.1-pro-preview") == "gemini"

    def test_flagged_deprecated(self):
        assert MODEL_REGISTRY["gemini-3.1-pro-preview"].get("deprecated") is True

    def test_served_via_vertex(self):
        """The deprecated model now runs on the Gemini Vertex backend."""
        assert get_backend_for_model("gemini-3.1-pro-preview") == "vertex"

    def test_all_gemini_models_on_vertex(self):
        """Every Gemini model runs on Vertex (the genapi transport was
        removed; thought signatures don't cross the Vertex/AI Studio border)."""
        for model_id, entry in MODEL_REGISTRY.items():
            if entry["provider"] == "gemini":
                assert get_backend_for_model(model_id) == "vertex", model_id

    def test_excluded_from_available_models_even_with_credentials(self):
        """With every backend credentialed, the deprecated model is still
        absent from available_models while all other registry models appear."""
        available = _available_with_all_credentials()

        assert "gemini-3.1-pro-preview" not in available
        expected = [
            m for m, e in MODEL_REGISTRY.items()
            if not (e.get("deprecated") or is_discontinued(e))
        ]
        assert available == expected


class TestDiscontinuationDates:
    """Gemini 3.6 Flash and 3.7 Flash carry Google's shutdown dates."""

    def test_every_date_is_iso(self):
        for model_id, entry in MODEL_REGISTRY.items():
            if "discontinued_on" in entry:
                date.fromisoformat(entry["discontinued_on"])  # raises if malformed

    def test_announced_dates(self):
        assert MODEL_REGISTRY["gemini-3.6-flash"]["discontinued_on"] == "2026-11-19"
        assert MODEL_REGISTRY["gemini-3.7-flash"]["discontinued_on"] == "2027-01-28"
        assert "discontinued_on" not in MODEL_REGISTRY["gemini-3.8-flash"]

    @pytest.mark.parametrize(
        ("today", "expected"),
        [
            (date(2026, 11, 18), False),
            (date(2026, 11, 19), True),
            (date(2026, 11, 20), True),
        ],
    )
    def test_deprecated_from_the_date_on(self, monkeypatch, today, expected):
        monkeypatch.setattr(llm_config, "_today_utc", lambda: today)
        spec = resolve_model("gemini-3.6-flash")
        assert spec.deprecated is expected
        assert spec.discontinued_on == "2026-11-19"

    def test_entry_without_date_never_discontinues(self):
        assert is_discontinued(MODEL_REGISTRY["gemini-3.8-flash"], date(2100, 1, 1)) is False

    def test_disappears_from_available_models_on_the_date(self, monkeypatch):
        monkeypatch.setattr(llm_config, "_today_utc", lambda: date(2026, 11, 18))
        before = _available_with_all_credentials()
        assert "gemini-3.6-flash" in before
        assert "gemini-3.7-flash" in before

        monkeypatch.setattr(llm_config, "_today_utc", lambda: date(2026, 11, 19))
        between = _available_with_all_credentials()
        assert "gemini-3.6-flash" not in between
        assert "gemini-3.7-flash" in between

        monkeypatch.setattr(llm_config, "_today_utc", lambda: date(2027, 1, 28))
        after = _available_with_all_credentials()
        assert "gemini-3.6-flash" not in after
        assert "gemini-3.7-flash" not in after
        assert "gemini-3.8-flash" in after

    def test_still_resolves_after_the_date(self, monkeypatch):
        """Old conversations keep their display name and provider lookup."""
        monkeypatch.setattr(llm_config, "_today_utc", lambda: date(2027, 6, 1))
        assert get_provider_for_model("gemini-3.7-flash") == "gemini"
        assert resolve_model("gemini-3.7-flash").display_name == "Gemini 3.7 Flash"
