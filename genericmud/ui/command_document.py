"""Text rules for optional editable entry, independent of native caret units.

Text uses LF internally. Each LF represents one logical CRLF delimiter for the
VIPMud-style three-delimiter duplicate rule. No sent/recalled identity is kept.
"""

from __future__ import annotations


def line_bounds(text: str, position: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return start, len(text) if end < 0 else end


def submit_command(text: str, position: int) -> str:
    """Return the document after ordinary Enter; dispatch is the caller's job."""
    start, end = line_bounds(text, position)
    command = text[start:end]
    if position < len(text):
        separator = "" if ord(text[-1]) < 32 else "\n"
        result = text + separator + command + "\n"
    else:
        result = text + "\n"
    # Compare only the last two completed rows, and only with three delimiters.
    last = result.rfind("\n")
    previous = result.rfind("\n", 0, last)
    earlier = result.rfind("\n", 0, previous) if previous >= 0 else -1
    if earlier >= 0 and result[earlier + 1:previous] == result[previous + 1:last]:
        result = result[:previous] + result[last:]
    return result


def down_creates_tail(text: str, position: int) -> bool:
    return not text.endswith("\n") and "\n" not in text[position:]


def insert_blank_line(text: str, position: int) -> tuple[str, int]:
    """Escape: preserve selected characters; caller passes the range's start.

    The first-line branch splits at the caret and requests position one. At
    position zero choose a valid empty-line caret rather than half of a CRLF.
    """
    previous = text.rfind("\n", 0, position)
    at = previous + 1 if previous >= 0 else position
    caret = at if previous >= 0 else min(1, position)
    return text[:at] + "\n" + text[at:], caret
