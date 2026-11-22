"""Markers the markup parsers leave in a line's text, for the engine to turn into positions.

A parser cannot report where something sits as a character offset: the MSP, ANSI and password
redaction passes that follow all rewrite the text. So it leaves a marker where an image sits, and
one where each link's words start and end, and the engine lifts them out before any of those
passes runs, carrying each marker's position through what they remove. None of them is ever
spoken, logged or shown.

A link's markers name the link: both are the code point for its number, drawn from Unicode's
Supplementary Private Use Area-B, which no font or MUD convention uses. A link is placed only when
its number appears exactly twice in the line, so a marker that went missing, or a number used
twice, leaves that link unplaced rather than moving it onto another link's words. Text from the
MUD can never carry one: every parser removes them from what it reads, and the engine removes any
that reach it on a line no parser read.
"""

from __future__ import annotations

import re

# Where an inline image sits in the text. Images are placed by order: the n-th marker is the
# n-th image.
IMAGE_MARK = ""

# A link's number becomes the code point LINK_MARK_BASE + number.
LINK_MARK_BASE = 0x100000
# U+100000 to U+10FFFD: the last two code points of the plane are noncharacters.
LINK_MARK_COUNT = 0xFFFE

_LINK_MARKS = re.compile("[\U00100000-\U0010fffd]")
_ALL_MARKS = re.compile("[\U00100000-\U0010fffd" + IMAGE_MARK + "]")


def link_mark(number: int) -> str:
    """The marker for link ``number``."""
    return chr(LINK_MARK_BASE + number)


def link_mark_number(char: str) -> int | None:
    """The link number ``char`` marks, or None when it is not a link marker."""
    code = ord(char) - LINK_MARK_BASE
    return code if 0 <= code < LINK_MARK_COUNT else None


def is_link_mark(char: str) -> bool:
    return link_mark_number(char) is not None


def has_marks(text: str) -> bool:
    """Whether ``text`` holds any marker, a link's or an image's."""
    return _ALL_MARKS.search(text) is not None


def without_link_marks(text: str) -> str:
    """``text`` without its link markers. Image markers stay: they are counted as text where
    they always were."""
    return _LINK_MARKS.sub("", text)


def without_marks(text: str) -> str:
    """``text`` without any marker: what a reader of it gets."""
    return _ALL_MARKS.sub("", text)


def lift_marks(text: str) -> tuple[str, list[tuple[int, str]]]:
    """``text`` without its markers, and each marker with the position it held once they're gone.

    A position is a boundary between characters of the returned text, so a marker at the very end
    of a line is at its length.
    """
    if not has_marks(text):
        return text, []
    kept: list[str] = []
    marks: list[tuple[int, str]] = []
    length = 0
    for char in text:
        if char == IMAGE_MARK or is_link_mark(char):
            marks.append((length, char))
        else:
            kept.append(char)
            length += 1
    return "".join(kept), marks


def restore_marks(text: str, marks: list[tuple[int, str]]) -> str:
    """Put markers back into ``text`` at their positions, in the order they were lifted."""
    if not marks:
        return text
    pieces: list[str] = []
    position = 0
    for at, char in marks:
        pieces.append(text[position:at])
        pieces.append(char)
        position = at
    pieces.append(text[position:])
    return "".join(pieces)


def remap_through_kept(positions: list[int], kept: list[bool]) -> list[int]:
    """Where each boundary position lands once the characters not ``kept`` are taken out.

    A position inside a removed stretch lands where that stretch was.
    """
    if all(kept):
        return list(positions)
    before = [0] * (len(kept) + 1)
    count = 0
    for index, keep in enumerate(kept):
        before[index] = count
        if keep:
            count += 1
    before[len(kept)] = count
    return [before[min(max(position, 0), len(kept))] for position in positions]


def remap_through_edits(
    positions: list[int], edits: list[tuple[int, int, int]]
) -> list[int]:
    """Where each boundary position lands after replacements.

    ``edits`` are (start, end, new length) for each replaced stretch of the old text, in order
    and not overlapping. A position inside a replaced stretch lands at the end of its
    replacement, so a link whose words were all inside one ends up empty.
    """
    if not edits:
        return list(positions)
    # One pass over the positions in order and the edits in order, so a line with thousands of
    # each costs what they are and not one times the other.
    mapped = [0] * len(positions)
    shift = 0
    upcoming = 0
    for index in sorted(range(len(positions)), key=positions.__getitem__):
        position = positions[index]
        while upcoming < len(edits) and edits[upcoming][1] <= position:
            start, end, new_length = edits[upcoming]
            shift += new_length - (end - start)
            upcoming += 1
        if upcoming < len(edits) and edits[upcoming][0] < position:
            start, _end, new_length = edits[upcoming]
            mapped[index] = start + shift + new_length
        else:
            mapped[index] = position + shift
    return mapped
