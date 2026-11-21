"""Shared helpers for plugin tool modules (config/plugin_tool_helpers.py)."""

import json

from config.plugin_tool_helpers import arg_int, arg_str, intent_param, tool_error


def test_intent_param_carries_the_example():
    assert intent_param("List devices") == {
        "type": "string",
        "description": (
            "A brief, user-friendly summary of your intent (max 50 characters). "
            "Example: 'List devices'."
        ),
    }


def test_arg_str_strips_and_maps_blank_to_none():
    assert arg_str("  abc ") == "abc"
    assert arg_str(42) == "42"
    assert arg_str(None) is None
    assert arg_str("") is None
    assert arg_str("   ") is None


def test_arg_int_clamps_and_falls_back():
    assert arg_int(5, 10, lo=1, hi=50) == 5
    assert arg_int("7", 10, lo=1, hi=50) == 7
    assert arg_int(0, 10, lo=1, hi=50) == 1
    assert arg_int(500, 10, lo=1, hi=50) == 50
    assert arg_int(None, 10, lo=1, hi=50) == 10
    assert arg_int("many", 10, lo=1, hi=50) == 10


def test_tool_error_is_a_json_string_with_extras():
    assert json.loads(tool_error("not_found", "No such device")) == {
        "error": "not_found", "message": "No such device",
    }
    assert json.loads(tool_error("upstream", "Boom", status_code=502)) == {
        "error": "upstream", "message": "Boom", "status_code": 502,
    }
