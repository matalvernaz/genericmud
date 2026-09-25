"""How a variable's value reads once it's filled into a command or into speech."""

from __future__ import annotations

import json


def format_value(value: object) -> str:
    """A value as ``${...}`` expansion puts it into a command or speech.

    Tables and lists become compact JSON, booleans lowercase (``true``), a missing
    value the empty string, anything else ``str``.
    """
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    return "" if value is None else str(value)
