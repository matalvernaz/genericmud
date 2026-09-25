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
# The same moves typed out in full. They move the player exactly as the short forms do,
# so they belong on the trail, recorded short so a retrace sends what a speedwalk would.
_FULL_NAMES = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "up": "u", "down": "d",
}
# Two-char directions must be tried before one-char so "ne" doesn't read as "n"+"e".
_TOKEN = re.compile(r"(\d*)(ne|nw|se|sw|n|s|e|w|u|d)")
_MAX_SPEEDWALK_STEPS = 1000  # a real route is tens of steps; a typo like ".999999999n" is refused


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
        if count == 0:
            return []  # "3n0e": a zero-count leg is a typo, not a silent no-op mid-walk
        if count > _MAX_SPEEDWALK_STEPS or len(directions) + count > _MAX_SPEEDWALK_STEPS:
            return []  # absurd repeat count (a typo/paste): refuse before allocating the list
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
        direction = _FULL_NAMES.get(direction, direction)
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
        waypoints: list[str] | None = None,
        locate: Callable[[], str] | None = None,
    ) -> None:
        self._remaining = list(steps)
        self._send = send
        self._schedule = schedule
        self._announce = announce
        self._timeout = step_timeout
        self._blocked = [re.compile(pattern, re.IGNORECASE) for pattern in blocked_patterns]
        self._token = 0  # bumps each step so a stale timeout callback no-ops
        self._active = False
        # A mapped route knows which room each step should land in, so it can tell being
        # moved off the route from moving along it. A typed speedwalk has no waypoints and
        # keeps the old behaviour of trusting any room change.
        self._waypoints = list(waypoints) if waypoints else []
        self._locate = locate
        self._sent = 0

    def start(self) -> None:
        self._active = True
        self._advance()

    def on_room_change(self) -> None:
        """A confirmed move (the MUD reported a new room): send the next step now."""
        if not self._active:
            return
        if self._strayed():
            abandoned = len(self._remaining)
            self.cancel()
            self._announce(f"off the route, {abandoned} steps abandoned")
            return
        self._token += 1  # invalidate the in-flight timeout
        self._advance()

    def _strayed(self) -> bool:
        """True when the room reached isn't the one this step of the route was heading for.

        Only a mapped route can tell: a teleport, a trapdoor or being dragged would
        otherwise send the rest of the directions from a room the route never ran through.
        A room the map can't place counts as strayed — not knowing where the player is is
        not a reason to keep walking them.
        """
        if not self._waypoints or self._locate is None or not self._sent:
            return False
        index = min(self._sent, len(self._waypoints)) - 1
        return self._locate() != self._waypoints[index]

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
        self._sent += 1
        self._token += 1
        token = self._token
        self._schedule(self._timeout, lambda: self._on_timeout(token))

    def _on_timeout(self, token: int) -> None:
        if self._active and token == self._token:
            self._advance()  # no room signal (e.g. GMCP-less MUD); assume the step worked
