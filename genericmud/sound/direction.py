"""Where a sound comes from: Client.Media's pan and direction, placed as Mudlet 5.1 places them.

A MUD can say a sound comes from the east, and with the player facing north it plays in the
right ear. Compass directions turn with the way the player faces (Client.Media.Listener);
ahead, behind, left and right are already relative to the player. Ahead and behind both sit in
the middle, as do up and down, because stereo can't tell front from back or above from below.
"""

from __future__ import annotations

import math

# The abbreviations a MUD may use, and the names they stand for.
_ABBREVIATIONS = {
    "n": "north", "ne": "northeast", "e": "east", "se": "southeast",
    "s": "south", "sw": "southwest", "w": "west", "nw": "northwest", "u": "up", "d": "down",
}
# Degrees clockwise from north, and whether the direction is already relative to the player.
_BEARINGS = {
    "north": (0.0, False), "northeast": (45.0, False), "east": (90.0, False),
    "southeast": (135.0, False), "south": (180.0, False), "southwest": (225.0, False),
    "west": (270.0, False), "northwest": (315.0, False),
    "ahead": (0.0, True), "right": (90.0, True), "behind": (180.0, True), "left": (270.0, True),
}


def bearing(direction: object) -> tuple[float, bool] | None:
    """A direction's bearing in degrees clockwise from north, and whether it is relative to the
    player. None for up, down, nothing, or anything unknown: those play in the middle."""
    if direction is None or isinstance(direction, bool):
        return None
    if isinstance(direction, (int, float)):
        degrees = float(direction)
    else:
        text = str(direction).strip().lower()
        try:
            degrees = float(text)  # degrees can come as text too
        except ValueError:
            return _BEARINGS.get(_ABBREVIATIONS.get(text, text))
    if not math.isfinite(degrees):
        return None
    return degrees % 360.0, False


def facing_bearing(facing: object) -> float | None:
    """The way the player faces, from Client.Media.Listener: a compass direction or degrees.

    Up, down and the relative words aren't ways a player can turn to face, so they're refused
    (None), as Mudlet refuses them.
    """
    found = bearing(facing)
    if found is None or found[1]:
        return None
    return found[0]


def stereo_pan(pan: object, direction: object, facing: float) -> float | None:
    """Where to play a sound, from -1 (left) to 1 (right), or None to leave it in the middle.

    A pan the MUD gives outright (-100 to 100) wins. Otherwise a compass direction is turned
    by the way the player faces, a relative one isn't, and the pan is the sine of the angle
    between them: east while facing north is fully right, northeast about two thirds right.
    """
    if pan is not None and not isinstance(pan, bool):
        try:
            given = float(pan)
        except (TypeError, ValueError):
            given = math.nan
        if math.isfinite(given):
            return max(-100.0, min(100.0, given)) / 100.0
    found = bearing(direction)
    if found is None:
        return None
    degrees, relative = found
    turned = degrees if relative else degrees - facing
    return round(math.sin(math.radians(turned)) * 100.0) / 100.0
