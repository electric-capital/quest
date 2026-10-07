"""Tests for the admin Model Selection layer.

Covers ``config/model_selection.py`` (the ``model_selection.json`` store:
absent-file defaults, normalization, slot uniqueness, legacy-id lookup,
the usage check), the selection fields ``public_model_catalog()`` adds to
every GET /app/api/config entry, and the admin endpoints in
``chat/routes/admin.py`` (row universe, availability flags, validation,
full-replacement semantics, admin gating). Everything runs against
tmp_path (the autouse fixture in tests/conftest.py redirects the store).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

import config.inference_providers as ip
import config.model_selection as ms
import config.server_config as scfg


DEEPSEEK = "deepseek/deepseek-v4-flash-0731"
ADMIN_USER = {"id": 1, "email": "admin@example.com"}
NOBODY = {"id": 2, "email": "user@example.com"}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point server_config.json and the Vertex env lookups at tmp_path."""
    monkeypatch.setattr(scfg, "SERVER_CONFIG_FILE", tmp_path / "server_config.json")
    for var in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "GEMINI_VERTEX_PROJECT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "gcloud"))
    return ms.MODEL_SELECTION_FILE


@pytest.fixture
def health_store(tmp_path, monkeypatch):
    import chat.llm.health as health

    fresh = health.ModelHealthStore(tmp_path / "model_health.json")
    monkeypatch.setattr(health, "_store", fresh)
    return fresh


@pytest.fixture
def admin_routes(store, health_store, monkeypatch):
    import chat.routes.admin as admin

    monkeypatch.setattr(admin, "is_admin", lambda email: email == ADMIN_USER["email"])
    return admin


def _entry(**overrides):
    return {**ms.UNSET_ENTRY, **overrides}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_normalize_entry_shapes():
    assert ms.normalize_entry(None) == ms.UNSET_ENTRY
    assert ms.normalize_entry({}) == ms.UNSET_ENTRY
    assert ms.normalize_entry({"slot": 3, "public_slot": 1, "descriptor": "  Smart ($$$) "}) == _entry(
        slot=3, public_slot=1, descriptor="Smart ($$$)"
    )
    # Out-of-range / non-int slots are dropped, not clamped (both menus)
    for field in ("slot", "public_slot"):
        assert ms.normalize_entry({field: 0})[field] is None
        assert ms.normalize_entry({field: ms.MAX_TOP_LEVEL_SLOTS + 1})[field] is None
        assert ms.normalize_entry({field: "2"})[field] is None
        assert ms.normalize_entry({field: True})[field] is None
    # A slot for a visibility the model is not allowed in is cleared
    assert ms.normalize_entry({"slot": 1, "allow_private": False}) == _entry(allow_private=False)
    assert ms.normalize_entry({"public_slot": 1, "allow_public": False}) == _entry(allow_public=False)
    assert ms.normalize_entry({"slot": 1, "public_slot": 2, "allow_public": False}) == _entry(
        slot=1, allow_public=False
    )
    # Descriptors are trimmed and capped; non-strings become empty
    assert ms.normalize_entry({"descriptor": 42})["descriptor"] == ""
    long = "x" * (ms.MAX_DESCRIPTOR_LENGTH + 10)
    assert len(ms.normalize_entry({"descriptor": long})["descriptor"]) == ms.MAX_DESCRIPTOR_LENGTH
    # Only an explicit False restricts usage
    assert ms.normalize_entry({"allow_private": False})["allow_private"] is False
    assert ms.normalize_entry({"allow_private": None})["allow_private"] is True
    assert ms.normalize_entry({"allow_public": 0})["allow_public"] is True


def test_read_absent_file_gives_historical_defaults(store):
    assert not store.exists()
    entries = ms.read_model_selection()
    assert entries == ms.DEFAULT_MODEL_SELECTION
    assert entries is not ms.DEFAULT_MODEL_SELECTION  # a copy
    assert entries["claude-opus-4-8"]["slot"] == 1
    assert entries["claude-opus-4-8"]["public_slot"] == 1
    assert entries["claude-opus-4-8"]["descriptor"] == "Smart ($$$)"
    assert entries["claude-sonnet-5"]["slot"] == 2
    assert entries["gemini-3.8-flash"]["slot"] == 3
    assert entries["gemini-3.8-flash"]["public_slot"] == 3


def test_read_malformed_file_is_empty_not_defaults(store):
    store.write_text("not json")
    assert ms.read_model_selection() == {}
    store.write_text(json.dumps({"models": ["nope"]}))
    assert ms.read_model_selection() == {}
    store.write_text(json.dumps([1, 2]))
    assert ms.read_model_selection() == {}


def test_read_drops_duplicate_slots_from_later_entries(store):
    store.write_text(json.dumps({
        "version": 1,
        "models": {
            "claude-opus-4-8": {"slot": 1, "public_slot": 2},
            # Same private slot -> dropped; public slots are a separate space
            "claude-sonnet-5": {"slot": 1, "public_slot": 1, "descriptor": "Dup"},
            "gemini-3.8-flash": {"public_slot": 2},
            "": {"slot": 2},
        },
    }))
    entries = ms.read_model_selection()
    assert entries["claude-opus-4-8"] == _entry(slot=1, public_slot=2)
    assert entries["claude-sonnet-5"] == _entry(slot=None, public_slot=1, descriptor="Dup")
    assert entries["gemini-3.8-flash"] == _entry(public_slot=None)
    assert "" not in entries


def test_save_roundtrip_and_unset_entries_dropped(store):
    stored = ms.save_model_selection({
        "claude-opus-4-8": {"slot": 2, "public_slot": 1, "descriptor": "Best"},
        "claude-sonnet-5": {"allow_public": False},
        "gemini-3.8-flash": {},  # fully unset -> not persisted
    })
    assert stored == {
        "claude-opus-4-8": _entry(slot=2, public_slot=1, descriptor="Best"),
        "claude-sonnet-5": _entry(allow_public=False),
    }
    assert store.exists()
    on_disk = json.loads(store.read_text())
    assert on_disk["version"] == 1
    assert set(on_disk["models"]) == {"claude-opus-4-8", "claude-sonnet-5"}
    assert ms.read_model_selection() == stored
    # No stray temp files from the atomic write
    assert [p.name for p in store.parent.iterdir() if p.name.startswith(".model_selection")] == []


def test_save_empty_clears_defaults_for_good(store):
    ms.save_model_selection({})
    assert store.exists()
    assert ms.read_model_selection() == {}


def test_save_rejects_duplicate_slots_without_writing(store):
    with pytest.raises(ValueError, match="Private top-level slot 1"):
        ms.save_model_selection({
            "claude-opus-4-8": {"slot": 1},
            "claude-sonnet-5": {"slot": 1},
        })
    with pytest.raises(ValueError, match="Public top-level slot 2"):
        ms.save_model_selection({
            "claude-opus-4-8": {"public_slot": 2},
            "claude-sonnet-5": {"public_slot": 2},
        })
    assert not store.exists()
    # The same number in the two menus is fine
    ms.save_model_selection({"claude-opus-4-8": {"slot": 1}, "claude-sonnet-5": {"public_slot": 1}})
    assert store.exists()


def test_selection_for_falls_back_to_qualified_legacy_id(store):
    ms.save_model_selection({f"openrouter:{DEEPSEEK}": {"allow_public": False}})
    assert ms.selection_for(f"openrouter:{DEEPSEEK}")["allow_public"] is False
    # A bare pre-instances id stored on an old conversation gets the same rule
    assert ms.selection_for(DEEPSEEK)["allow_public"] is False
    assert ms.selection_for("claude-opus-4-8") == ms.UNSET_ENTRY
    assert ms.selection_for("") == ms.UNSET_ENTRY
    assert ms.selection_for(None) == ms.UNSET_ENTRY


def test_is_model_allowed(store):
    assert ms.is_model_allowed("claude-opus-4-8", public=True)
    assert ms.is_model_allowed("claude-opus-4-8", public=False)
    ms.save_model_selection({
        "claude-opus-4-8": {"allow_public": False},
        "gemini-3.8-flash": {"allow_private": False},
    })
    assert not ms.is_model_allowed("claude-opus-4-8", public=True)
    assert ms.is_model_allowed("claude-opus-4-8", public=False)
    assert ms.is_model_allowed("gemini-3.8-flash", public=True)
    assert not ms.is_model_allowed("gemini-3.8-flash", public=False)
    # Unknown models are the provider layer's problem, not a selection rule
    assert ms.is_model_allowed("no-such-model", public=False)


# ---------------------------------------------------------------------------
# Public catalog (GET /app/api/config `models`)
# ---------------------------------------------------------------------------

def test_public_model_catalog_carries_selection_fields(store):
    from chat.llm.config import public_model_catalog

    rows = {m["id"]: m for m in public_model_catalog()}
    assert rows["claude-opus-4-8"]["slot"] == 1
    assert rows["claude-opus-4-8"]["public_slot"] == 1
    assert rows["claude-opus-4-8"]["descriptor"] == "Smart ($$$)"
    assert rows["claude-opus-4-8"]["allow_private"] is True
    assert rows["claude-opus-4-8"]["allow_public"] is True
    assert rows["claude-haiku-4.5"]["slot"] is None
    assert rows["claude-haiku-4.5"]["descriptor"] == ""

    ms.save_model_selection({
        "claude-haiku-4.5": {"slot": 5, "descriptor": "Cheap", "allow_public": False},
        "gemini-3.8-flash": {"public_slot": 1},
    })
    rows = {m["id"]: m for m in public_model_catalog()}
    assert rows["claude-haiku-4.5"]["slot"] == 5
    assert rows["claude-haiku-4.5"]["public_slot"] is None
    assert rows["claude-haiku-4.5"]["descriptor"] == "Cheap"
    assert rows["claude-haiku-4.5"]["allow_public"] is False
    assert rows["gemini-3.8-flash"]["public_slot"] == 1
    # Saving replaced the defaults wholesale
    assert rows["claude-opus-4-8"]["slot"] is None


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

def test_admin_endpoints_require_admin(admin_routes):
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_get_model_selection(user=NOBODY))
    assert exc_info.value.status_code == 403
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_update_model_selection(
            admin_routes.ModelSelectionUpdate(models=[]), user=NOBODY,
        ))
    assert exc_info.value.status_code == 403


def test_admin_get_rows_and_availability(admin_routes, health_store):
    from chat.llm.config import resolve_model

    # Gemini-only Vertex project (the anthropic->gemini project fallback runs
    # the other way, so Claude stays unconfigured here)
    scfg.SERVER_CONFIG_FILE.write_text(json.dumps({"gemini_vertex": {"vertex_project_id": "p"}}))
    ip.set_vertex_disabled_models(["claude-opus-4-6"])
    ip.write_inference_credentials("openrouter", {"api_key": "sk"})
    _run(health_store.record("gemini-3.5-flash-lite", False, "boom"))

    result = _run(admin_routes.admin_get_model_selection(user=ADMIN_USER))
    assert result["max_slots"] == ms.MAX_TOP_LEVEL_SLOTS
    assert result["max_descriptor_length"] == ms.MAX_DESCRIPTOR_LENGTH
    rows = {m["id"]: m for m in result["models"]}
    # Deprecated and admin-disabled models are not curatable rows
    assert not any(resolve_model(m).deprecated for m in rows)
    assert "claude-opus-4-6" not in rows
    # Instance models are rows too (the legacy openrouter instance is synthesized)
    assert f"openrouter:{DEEPSEEK}" in rows
    assert rows[f"openrouter:{DEEPSEEK}"]["instance_id"] == "openrouter"
    assert rows[f"openrouter:{DEEPSEEK}"]["wire_id"] == DEEPSEEK
    assert rows[f"openrouter:{DEEPSEEK}"]["available"] is True
    # Availability mirrors get_available_models: Gemini configured, Claude not
    assert rows["gemini-3.8-flash"]["available"] is True
    assert rows["gemini-3.8-flash"]["unavailable_reason"] is None
    assert rows["claude-opus-4-8"]["available"] is False
    assert rows["claude-opus-4-8"]["unavailable_reason"] == "not_configured"
    assert rows["gemini-3.5-flash-lite"]["available"] is False
    assert rows["gemini-3.5-flash-lite"]["unavailable_reason"] == "failing"
    # Selection fields come from the (default) store
    assert rows["claude-opus-4-8"]["slot"] == 1
    assert rows["claude-opus-4-8"]["public_slot"] == 1
    assert rows["claude-opus-4-8"]["descriptor"] == "Smart ($$$)"
    assert rows["claude-haiku-4.5"]["slot"] is None
    assert rows["claude-haiku-4.5"]["allow_private"] is True
    ms.save_model_selection({"claude-haiku-4.5": {"allow_public": False, "public_slot": 4}})
    result = _run(admin_routes.admin_get_model_selection(user=ADMIN_USER))
    rows = {m["id"]: m for m in result["models"]}
    assert rows["claude-haiku-4.5"]["allow_public"] is False


def test_admin_put_full_replacement(admin_routes):
    body = admin_routes.ModelSelectionUpdate(models=[
        admin_routes.ModelSelectionEntry(id="claude-haiku-4.5", slot=1, public_slot=2, descriptor=" Cheapest ($) "),
        # public_slot on a public-disallowed model is cleared, not rejected
        admin_routes.ModelSelectionEntry(id="claude-opus-4-8", public_slot=1, allow_public=False),
        admin_routes.ModelSelectionEntry(id="gemini-3.8-flash"),
    ])
    result = _run(admin_routes.admin_update_model_selection(body, user=ADMIN_USER))
    rows = {m["id"]: m for m in result["models"]}
    assert rows["claude-haiku-4.5"]["slot"] == 1
    assert rows["claude-haiku-4.5"]["public_slot"] == 2
    assert rows["claude-haiku-4.5"]["descriptor"] == "Cheapest ($)"
    assert rows["claude-opus-4-8"]["slot"] is None  # default slot gone
    assert rows["claude-opus-4-8"]["public_slot"] is None
    assert rows["claude-opus-4-8"]["allow_public"] is False
    assert rows["claude-sonnet-5"]["slot"] is None  # unlisted -> unset
    assert ms.read_model_selection() == {
        "claude-haiku-4.5": _entry(slot=1, public_slot=2, descriptor="Cheapest ($)"),
        "claude-opus-4-8": _entry(allow_public=False),
    }


@pytest.mark.parametrize("models, error", [
    ([{"id": "no-such-model", "slot": 1}], "unknown_model"),
    ([{"id": "claude-opus-4-8", "slot": 0}], "invalid_params"),
    ([{"id": "claude-opus-4-8", "slot": ms.MAX_TOP_LEVEL_SLOTS + 1}], "invalid_params"),
    ([{"id": "claude-opus-4-8", "public_slot": 0}], "invalid_params"),
    ([{"id": "claude-opus-4-8", "public_slot": ms.MAX_TOP_LEVEL_SLOTS + 1}], "invalid_params"),
    ([{"id": "claude-opus-4-8", "slot": 1}, {"id": "claude-sonnet-5", "slot": 1}], "invalid_params"),
    ([{"id": "claude-opus-4-8", "public_slot": 3}, {"id": "claude-sonnet-5", "public_slot": 3}], "invalid_params"),
    ([{"id": "claude-opus-4-8"}, {"id": "claude-opus-4-8"}], "invalid_params"),
    ([{"id": "claude-opus-4-8", "descriptor": "x" * (ms.MAX_DESCRIPTOR_LENGTH + 1)}], "invalid_params"),
])
def test_admin_put_validation_400(admin_routes, models, error):
    ms.save_model_selection({"claude-sonnet-5": {"slot": 4}})
    body = admin_routes.ModelSelectionUpdate(
        models=[admin_routes.ModelSelectionEntry(**m) for m in models],
    )
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_update_model_selection(body, user=ADMIN_USER))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == error
    # Nothing written on a rejected update
    assert ms.read_model_selection() == {"claude-sonnet-5": _entry(slot=4)}
