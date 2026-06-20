"""Strip terminal escape sequences from MUD output.

The native output box is plain text (no colour rendering yet), so escape sequences
are removed rather than parsed: CSI (SGR colour, cursor moves, and MXP line-mode
``\\x1b[...z``), OSC, and other escapes. Colour-to-spans parsing is a later feature.
"""

from __future__ import annotations

import re

_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_REMAINING = re.compile(r"\x1b.?")


def strip_ansi(text: str) -> str:
    text = _CSI.sub("", text)
    text = _OSC.sub("", text)
    return _REMAINING.sub("", text)
