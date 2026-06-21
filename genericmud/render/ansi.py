"""Terminal escape handling: strip to plain text, or parse SGR colour into spans.

The screen reader speaks plain text, so :func:`strip_ansi` stays the fast path for
content used in matching/speech. :func:`parse_ansi` additionally keeps the colour:
it splits a line into :class:`Span` runs carrying fg/bg/bold, which lets soundpacks
write colour-aware triggers (inspect ``ctx.line.spans``). Non-SGR escapes (cursor
moves, MXP line-mode ``\\x1b[...z``, OSC) are consumed without affecting style.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_REMAINING = re.compile(r"\x1b.?")

# Any escape sequence (for span splitting); SGR is recognized separately to read colour.
_ESC_SEQ = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI (any final byte)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b.?"  # other / lone ESC
)
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_BASE = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")


def strip_ansi(text: str) -> str:
    text = _CSI.sub("", text)
    text = _OSC.sub("", text)
    return _REMAINING.sub("", text)


@dataclass(frozen=True)
class Span:
    """A run of text sharing one style. ``fg``/``bg`` are colour names (``"red"``,
    ``"bright_cyan"``) or ``"256:N"`` / ``"rgb:r,g,b"`` for extended colour."""

    text: str
    fg: str | None = None
    bg: str | None = None
    bold: bool = False


def parse_ansi(text: str) -> list[Span]:
    """Split ``text`` into styled spans. ``"".join(s.text ...)`` equals strip_ansi(text)."""
    spans: list[Span] = []
    fg: str | None = None
    bg: str | None = None
    bold = False
    last = 0
    for match in _ESC_SEQ.finditer(text):
        literal = text[last:match.start()]
        if literal:
            spans.append(Span(literal, fg, bg, bold))
        sgr = _SGR.fullmatch(match.group(0))
        if sgr is not None:
            fg, bg, bold = _apply_sgr(sgr.group(1), fg, bg, bold)
        last = match.end()
    tail = text[last:]
    if tail:
        spans.append(Span(tail, fg, bg, bold))
    return spans


def _apply_sgr(
    params: str, fg: str | None, bg: str | None, bold: bool
) -> tuple[str | None, str | None, bool]:
    codes = [int(p) if p else 0 for p in params.split(";")]
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            fg, bg, bold = None, None, False
        elif code == 1:
            bold = True
        elif code == 22:
            bold = False
        elif 30 <= code <= 37:
            fg = _BASE[code - 30]
        elif 90 <= code <= 97:
            fg = "bright_" + _BASE[code - 90]
        elif 40 <= code <= 47:
            bg = _BASE[code - 40]
        elif 100 <= code <= 107:
            bg = "bright_" + _BASE[code - 100]
        elif code == 39:
            fg = None
        elif code == 49:
            bg = None
        elif code in (38, 48):
            extended, consumed = _extended_colour(codes, index)
            if code == 38:
                fg = extended
            else:
                bg = extended
            index += consumed
        index += 1
    return fg, bg, bold


def _extended_colour(codes: list[int], index: int) -> tuple[str | None, int]:
    """Read a 38/48 extended-colour argument; returns (value, extra codes consumed)."""
    if index + 1 < len(codes) and codes[index + 1] == 5 and index + 2 < len(codes):
        return f"256:{codes[index + 2]}", 2
    if index + 1 < len(codes) and codes[index + 1] == 2 and index + 4 < len(codes):
        r, g, b = codes[index + 2], codes[index + 3], codes[index + 4]
        return f"rgb:{r},{g},{b}", 4
    return None, 0
