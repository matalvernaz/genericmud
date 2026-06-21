"""Speedwalking + breadcrumb navigation — a daily blind-player utility.

Pure, UI-agnostic movement helpers: expand a compact speedwalk run (``3n2e``)
into individual directions, invert a direction, and a :class:`Navigator` that
records a breadcrumb trail as the player walks so it can retrace the way back.
Optional GMCP ``room.info`` feeds a spoken "where am I". The app drives all of
this; there is no I/O here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# Compass + vertical directions paired with their opposites (used to retrace).
OPPOSITE = {
    "n": "s", "s": "n", "e": "w", "w": "e",
    "ne": "sw", "sw": "ne", "nw": "se", "se": "nw",
    "u": "d", "d": "u",
}
DIRECTIONS = frozenset(OPPOSITE)
# Two-char directions must be tried before one-char so "ne" doesn't read as "n"+"e".
_TOKEN = re.compile(r"(\d*)(ne|nw|se|sw|n|s|e|w|u|d)")


def expand_speedwalk(run: str) -> list[str]:
    """Expand ``3n2e`` -> ``['n','n','n','e','e']``.

    Returns ``[]`` if the run isn't a clean speedwalk (any unrecognized char),
    so the caller can fall back to treating the input as an ordinary command.
    """
    run = run.strip().lower()
    if not run:
        return []
    directions: list[str] = []
    position = 0
    for match in _TOKEN.finditer(run):
        if match.start() != position:
            return []  # a gap means an unrecognized character -> not a speedwalk
        count = int(match.group(1)) if match.group(1) else 1
        directions.extend([match.group(2)] * count)
        position = match.end()
    return directions if position == len(run) else []


def invert(direction: str) -> str | None:
    """The opposite direction, or None if ``direction`` isn't a known one."""
    return OPPOSITE.get(direction.strip().lower())


def simplify_directions(directions: list[str]) -> list[str]:
    """Collapse out-and-back side trips: drop adjacent opposite pairs.

    ``[n, e, w, s]`` (walked east then straight back, then back south) reduces to
    ``[]``; ``[n, n, s]`` to ``[n]``. A stack fold, so nested backtracks cancel too.
    """
    stack: list[str] = []
    for direction in directions:
        if stack and stack[-1] == OPPOSITE.get(direction):
            stack.pop()
        else:
            stack.append(direction)
    return stack


@dataclass
class Navigator:
    """A breadcrumb trail plus the last-known room. Directions record as walked."""

    trail: list[str] = field(default_factory=list)
    room: dict | None = None
    simplify_retrace: bool = True  # weed redundant side trips out of the way back

    def record(self, direction: str) -> bool:
        """Append a movement step. Returns False (and ignores) a non-direction."""
        direction = direction.strip().lower()
        if direction not in DIRECTIONS:
            return False
        self.trail.append(direction)
        return True

    def retrace(self) -> list[str]:
        """The path back to the start: trail (optionally simplified) reversed + inverted."""
        trail = simplify_directions(self.trail) if self.simplify_retrace else list(self.trail)
        return [OPPOSITE[step] for step in reversed(trail)]

    def clear(self) -> None:
        """Drop a fresh breadcrumb here (forget the trail walked so far)."""
        self.trail.clear()

    def update_room(self, data: dict) -> None:
        self.room = data

    def where(self) -> str:
        """A spoken summary of the current room (from GMCP) + steps from the mark."""
        parts: list[str] = []
        if self.room:
            name = self.room.get("name") or self.room.get("Name")
            area = self.room.get("area") or self.room.get("Area")
            exits = self.room.get("exits") or self.room.get("Exits")
            if name:
                parts.append(str(name))
            if area:
                parts.append(f"in {area}")
            if exits:
                names = exits.keys() if isinstance(exits, dict) else exits
                parts.append("exits " + ", ".join(str(name) for name in names))
        if self.trail:
            parts.append(f"{len(self.trail)} steps from your breadcrumb")
        return "; ".join(parts) if parts else "no location info"


# Default lines that mean "you didn't move" — halt a safe-walk when one appears.
DEFAULT_BLOCKED_PATTERNS = (
    r"can'?t go that way",
    r"cannot go that way",
    r"there is no exit",
    r"no exit (in )?that (way|direction)",
    r"the door is closed",
    r"you are unable to",
)


class SafeWalk:
    """Walk a speedwalk one step at a time, halting if a step is blocked.

    Adaptive: each step advances on a confirmed room change (``on_room_change``,
    fed from GMCP) or, failing that, a per-step timeout (so a MUD without GMCP
    still progresses). A blocked-movement line (``on_line``) abandons the rest.
    Pure control logic — ``send``/``schedule``/``announce`` are injected.
    """

    def __init__(
        self,
        steps: list[str],
        *,
        send: Callable[[str], None],
        schedule: Callable[[float, Callable[[], None]], None],
        announce: Callable[[str], None],
        step_timeout: float = 0.5,
        blocked_patterns: tuple[str, ...] = DEFAULT_BLOCKED_PATTERNS,
    ) -> None:
        self._remaining = list(steps)
        self._send = send
        self._schedule = schedule
        self._announce = announce
        self._timeout = step_timeout
        self._blocked = [re.compile(pattern, re.IGNORECASE) for pattern in blocked_patterns]
        self._token = 0  # bumps each step so a stale timeout callback no-ops
        self._active = False

    def start(self) -> None:
        self._active = True
        self._advance()

    def on_room_change(self) -> None:
        """A confirmed move (GMCP room changed): send the next step now."""
        if self._active:
            self._token += 1  # invalidate the in-flight timeout
            self._advance()

    def on_line(self, text: str) -> None:
        """Halt the walk if an incoming line says the move was blocked."""
        if self._active and any(pattern.search(text) for pattern in self._blocked):
            abandoned = len(self._remaining)
            self.cancel()
            self._announce(f"path blocked, {abandoned} steps abandoned")

    def cancel(self) -> None:
        self._active = False
        self._remaining.clear()

    @property
    def active(self) -> bool:
        return self._active

    def _advance(self) -> None:
        if not self._remaining:
            self._active = False
            self._announce("arrived")
            return
        step = self._remaining.pop(0)
        self._send(step)
        self._token += 1
        token = self._token
        self._schedule(self._timeout, lambda: self._on_timeout(token))

    def _on_timeout(self, token: int) -> None:
        if self._active and token == self._token:
            self._advance()  # no room signal (e.g. GMCP-less MUD); assume the step worked
