"""Line and scrollback Buffer — the single source of truth for output.

A :class:`Line` is one logical output line; styled spans (from the ANSI parser)
attach later, so for now ``plain_text`` carries the content used for trigger
matching and review. The :class:`Buffer` is a bounded ring: the live self-voice
reacts to append *events*, while review reads the buffer, keeping the two
decoupled (the anti-flooding design).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

DEFAULT_CAPACITY = 50_000


@dataclass
class Line:
    plain_text: str
    channel: str = "main"
    gagged: bool = False  # suppressed from self-voice
    display_when_gagged: bool = False  # gagged from voice but kept visible/reviewable
    ts: float = field(default_factory=time.time)


class Buffer:
    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._lines: deque[Line] = deque(maxlen=capacity)

    def append(self, line: Line) -> None:
        self._lines.append(line)

    def lines(self) -> list[Line]:
        return list(self._lines)

    def __len__(self) -> int:
        return len(self._lines)

    def __getitem__(self, index: int) -> Line:
        return self._lines[index]
