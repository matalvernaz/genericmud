"""Line and scrollback Buffer — the single source of truth for output.

A :class:`Line` is one logical output line. ``plain_text`` carries the content
used for trigger matching, speech, and review; ``spans`` holds the same text
split into colour runs (from the ANSI parser) so colour-aware triggers can
inspect style. The :class:`Buffer` is a bounded ring: the live self-voice reacts
to append *events*, while review reads the buffer, keeping the two decoupled (the
anti-flooding design).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from genericmud.render.ansi import Span

DEFAULT_CAPACITY = 50_000


@dataclass(frozen=True)
class Link:
    """One MXP link: the words it shows, and what following it does.

    ``insert`` is ``<SEND PROMPT>``: the command belongs in the player's own command box, to
    finish or abandon, rather than going to the MUD as it stands. A link that carried half a
    command ("tell Ariadne ") would otherwise send that half the moment it was followed.

    ``choices`` is a ``<SEND>`` menu: several commands instead of one, each with the caption
    the MUD gave it. Following a menu never sends anything by itself -- it asks which of them
    the player meant. ``command`` stays the first choice, so anything that does not know about
    menus still has a sensible answer.
    """

    text: str
    command: str
    insert: bool = False
    hint: str = ""  # the caption the link carries for itself ("Click to buy a sword")
    choices: tuple[tuple[str, str], ...] = ()  # (command, caption) for a menu


@dataclass
class Line:
    plain_text: str
    channel: str = "main"
    gagged: bool = False  # suppressed from self-voice
    display_when_gagged: bool = False  # gagged from voice but kept visible/reviewable
    ts: float = field(default_factory=time.time)
    spans: list[Span] = field(default_factory=list)  # colour runs; empty if unparsed
    seq: int = -1  # monotonic all-time position, stamped by Buffer.append (-1 = unbuffered)
    # A pack's replacement (VIPMud #Sub), shown and spoken instead once every trigger has
    # seen the line the MUD sent.
    substitute: str | None = None
    # Each MXP link on this line, so review can follow one without a mouse. Keeping them here
    # rather than in a session-wide list means a link stays with the line it came from: reading
    # back through the scrollback still finds it, and no expiry bookkeeping can leave an entry
    # pointing at the wrong message.
    links: list[Link] = field(default_factory=list)


class Buffer:
    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._lines: deque[Line] = deque(maxlen=capacity)
        self._appended = 0  # all-time append count; stamps each line's monotonic seq

    def append(self, line: Line) -> None:
        # A monotonic seq lets review cursors anchor to a specific line that survives both
        # new appends and ring eviction, instead of an absolute index that slides under them.
        line.seq = self._appended
        self._appended += 1
        self._lines.append(line)

    def lines(self) -> list[Line]:
        return list(self._lines)

    def __len__(self) -> int:
        return len(self._lines)

    def __getitem__(self, index: int) -> Line:
        return self._lines[index]
