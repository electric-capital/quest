"""Small helpers shared by plugin tool modules (``plugins/<id>/tools.py``).

Every plugin tool declares the same ``intent_message`` parameter, reads
loosely typed arguments from the model, and answers failures with a JSON
error string. These are the pieces that are the same in every plugin:

- :func:`intent_param` -- the ``intent_message`` parameter schema;
- :func:`arg_str` / :func:`arg_int` -- coerce a model-supplied argument;
- :func:`tool_error` -- the ``{"error": code, "message": ...}`` result.

Boolean arguments are left to each plugin: some tools need a tri-state
(absent is different from false) and some accept only a literal ``true``.

Like ``config/plugin_types.py``, this module is stdlib-only.
"""

from __future__ import annotations

import json


def intent_param(example: str) -> dict:
    """The ``intent_message`` parameter schema, with a tool-specific example."""
    return {
        "type": "string",
        "description": (
            "A brief, user-friendly summary of your intent (max 50 characters). "
            f"Example: '{example}'."
        ),
    }


def arg_str(value) -> str | None:
    """An optional string argument, stripped; None when absent or blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def arg_int(value, default: int, *, lo: int, hi: int) -> int:
    """An optional integer argument clamped to ``lo..hi``; ``default`` when
    absent or not a number."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def tool_error(code: str, message: str, **extra) -> str:
    """A tool result reporting a failure: ``{"error": code, "message": message}``
    plus any ``extra`` keys, as a JSON string."""
    return json.dumps({"error": code, "message": message, **extra})
