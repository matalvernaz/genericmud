"""Speedwalking + breadcrumb navigation — a daily blind-player utility.

Pure, UI-agnostic movement helpers: expand a compact speedwalk run (``3n2e``)
into individual directions, invert a direction, and a :class:`Navigator` that
records a breadcrumb trail as the player walks so it can retrace the way back.
Optional GMCP ``room.info`` feeds a spoken "where am I". The app drives all of
this; there is no I/O here.
"""

from __future__ import annotations

import re
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


@dataclass
class Navigator:
    """A breadcrumb trail plus the last-known room. Directions record as walked."""

    trail: list[str] = field(default_factory=list)
    room: dict | None = None

    def record(self, direction: str) -> bool:
        """Append a movement step. Returns False (and ignores) a non-direction."""
        direction = direction.strip().lower()
        if direction not in DIRECTIONS:
            return False
        self.trail.append(direction)
        return True

    def retrace(self) -> list[str]:
        """The path back to the start: the trail reversed, each step inverted."""
        return [OPPOSITE[step] for step in reversed(self.trail)]

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
