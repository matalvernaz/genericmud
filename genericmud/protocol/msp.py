"""MSP (Mud Sound Protocol) parsing.

MSP is inline, not a telnet option: servers embed ``!!SOUND(file ...)`` and
``!!MUSIC(file ...)`` tags in the text stream. Parameters are ``K=V`` tokens:
V=volume(0-100), L=loops(-1 infinite, default 1), P=priority, T=type/group,
U=download URL. We strip the tags from the visible line and return the cues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_VOLUME = 100
DEFAULT_REPEATS = 1
DEFAULT_PRIORITY = 50

_SOUND_RE = re.compile(r"!!SOUND\((?P<body>[^)]*)\)")
_MUSIC_RE = re.compile(r"!!MUSIC\((?P<body>[^)]*)\)")


@dataclass(frozen=True)
class SoundCue:
    kind: str  # "sound" (one-shot, overlaps) or "music" (single looping channel)
    file: str
    volume: int = DEFAULT_VOLUME
    repeats: int = DEFAULT_REPEATS
    priority: int = DEFAULT_PRIORITY
    type: str = ""
    url: str = ""


def parse_msp_line(text: str) -> tuple[str, list[SoundCue]]:
    """Return (text with MSP tags removed, cues in order of appearance)."""
    cues: list[SoundCue] = []

    def take(kind: str):
        def _repl(match: re.Match[str]) -> str:
            cue = _parse_cue(kind, match.group("body"))
            if cue is not None:
                cues.append(cue)
            return ""

        return _repl

    clean = _SOUND_RE.sub(take("sound"), text)
    clean = _MUSIC_RE.sub(take("music"), clean)
    return clean, cues


def _parse_cue(kind: str, body: str) -> SoundCue | None:
    parts = body.split()
    if not parts:
        return None
    params: dict[str, str] = {}
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            params[key.upper()] = value
    return SoundCue(
        kind=kind,
        file=parts[0],
        volume=_to_int(params.get("V"), DEFAULT_VOLUME),
        repeats=_to_int(params.get("L"), DEFAULT_REPEATS),
        priority=_to_int(params.get("P"), DEFAULT_PRIORITY),
        type=params.get("T", ""),
        url=params.get("U", ""),
    )


def _to_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
