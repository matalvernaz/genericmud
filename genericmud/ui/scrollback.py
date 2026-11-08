"""Reading position in the output control: keeping a place, and finding a new one.

Two jobs, both pure so they can be tested on a machine that can't run the Windows UI.

The output control is append-only and trimmed from the top, and both operations move
text out from under someone who has tabbed in and is arrowing back through it: wx's
``AppendText`` drags the caret to the end, and dropping the oldest lines shifts every
surviving offset down. :func:`anchor_after_append` is that offset arithmetic.

:func:`find_line` backs the Find keys, locating the engine's matched line among the
control's lines so the caret can follow the scrollback search onto the same line.
Everything here works in whole lines, never raw character offsets: on Windows the
native control counts a line break as two characters while ``GetValue()`` renders it
as one, so a string offset walks off the caret's actual position by one character per
preceding line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def anchor_after_append(anchor: int, removed: int, last_position: int) -> int:
    """Where a caret parked at ``anchor`` belongs after an append plus a top trim.

    ``removed`` is the character count dropped off the front, which shifts every
    surviving offset down by exactly that much. The result is clamped into the control,
    so a reader whose place was itself trimmed away lands at the top of what's left
    rather than at a stale offset.
    """
    return max(0, min(anchor - removed, last_position))


def find_line(
    lines: Sequence[str],
    needle_line: str,
    current: int,
    *,
    forward: bool,
    from_edge: bool = False,
) -> int | None:
    """Index of the line equal to ``needle_line``, or None if it isn't among ``lines``.

    The engine matched a whole scrollback line, so only a control line equal to it
    counts; a longer line merely containing the text must not capture the caret.
    ``current`` is the caret's line. Repeated searches begin strictly past it so they
    advance. ``from_edge`` is for a newly-submitted dialog search: it takes the first
    (forward) or last (backward) occurrence, matching the engine cursor's reset.

    Trailing whitespace is ignored on both sides. A line's end is not something a reader can
    see or aim at, and a control that has normalised an edge space would otherwise hide a line
    that is right there on screen.
    """
    if not needle_line:
        return None
    wanted = needle_line.rstrip()
    if from_edge:
        indices = range(len(lines)) if forward else range(len(lines) - 1, -1, -1)
    elif forward:
        indices = range(max(current + 1, 0), len(lines))
    else:
        indices = range(min(current, len(lines)) - 1, -1, -1)
    for i in indices:
        if lines[i].rstrip() == wanted:
            return i
    return None


def break_width(control: object) -> int:
    """How many characters a control stores for one line break: 2 on Windows, 1 on GTK.

    Measured rather than assumed, because it differs by platform: Windows keeps a break as CRLF
    so every position after it is one further along than the plain text suggests, and GTK keeps
    one character. Getting it wrong shifts a caret by every line above it.
    """
    value = control.GetValue()
    breaks = value.count("\n")
    if not breaks:
        return 1
    extra = control.GetLastPosition() - len(value)
    return 1 + max(0, extra) // breaks


def position_of_line(lines: Sequence[str], index: int, *, width: int = 2) -> int:
    """Where line ``index`` starts, in the control's own position units, or the text's end.

    Counted from the text rather than asked of the control, because the control answers "where
    does line N start?" through a Windows message that carries at most 1,024 characters: a
    longer line is reported as several lines, so every line number after it is wrong. A MUD's
    room descriptions run past that, and the caret then landed a few lines short of the line the
    search had announced. The arithmetic is right at any length: each line's own characters, plus
    what the control stores for its break (see :func:`break_width`).
    """
    index = max(0, min(index, len(lines)))
    return sum(len(line) for line in lines[:index]) + width * index


def line_at_position(lines: Sequence[str], position: int, *, width: int = 2) -> int:
    """Which line a control position falls on, counted as :func:`position_of_line` counts.

    The reader's own place is found this way too: their caret is a position, and converting it
    through the control's line numbers has the same 1,024-character fault.
    """
    remaining = max(0, position)
    for index, line in enumerate(lines):
        cost = len(line) + width  # the line and the break after it, as the control stores them
        if remaining < cost:
            return index
        remaining -= cost
    return max(0, len(lines) - 1)


@dataclass
class FindState:
    """The last search, so the dialog reopens with it and F3 can repeat it.

    Backwards by default: the output is history, and the interesting thing is nearly
    always behind you.
    """

    term: str = ""
    forward: bool = False
    case_sensitive: bool = False
