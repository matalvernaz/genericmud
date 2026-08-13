"""MSP (Mud Sound Protocol) parsing.

The cues are inline: servers embed ``!!SOUND(file ...)`` and ``!!MUSIC(file ...)``
tags in the text stream, so this module is a line parser rather than a
subnegotiation decoder. MSP is still telnet option 90 — spec-compliant servers
send ``IAC WILL MSP`` and emit nothing until the client answers ``IAC DO MSP``,
so the option has to be in ``_ACCEPT_REMOTE`` for any of this to run at all.
Parameters are ``K=V`` tokens:
V=volume(0-100), L=loops(-1 infinite, default 1), P=priority, T=type/group,
U=download URL. We strip the tags from the visible line and return the cues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_VOLUME = 100
DEFAULT_REPEATS = 1
DEFAULT_PRIORITY = 50
STOP_CUE = "off"  # !!SOUND(Off)/!!MUSIC(Off): the spec's stop request, not a filename

_SOUND_RE = re.compile(r"!!SOUND\((?P<body>[^)]*)\)")
_MUSIC_RE = re.compile(r"!!MUSIC\((?P<body>[^)]*)\)")
# A trigger only counts when it opens the line, so consecutive tags on their own line all
# fire while one buried in prose does not. See parse_msp_line for why that matters.
_LEADING_RE = re.compile(r"^\s*!!(?P<kind>SOUND|MUSIC)\((?P<body>[^)]*)\)")


@dataclass(frozen=True)
class SoundCue:
    kind: str  # "sound" (one-shot, overlaps) or "music" (single looping channel)
    file: str
    volume: int = DEFAULT_VOLUME
    repeats: int = DEFAULT_REPEATS
    priority: int = DEFAULT_PRIORITY
    type: str = ""
    url: str = ""

    @property
    def is_stop(self) -> bool:
        """``!!MUSIC(Off)`` is the only way a server ends music it started, so treating
        ``Off`` as a filename leaves the track looping for the rest of the session."""
        return self.file.casefold() == STOP_CUE

    @property
    def loops_forever(self) -> bool:
        """``L=-1`` means loop until stopped — how servers start ambience. A finite count
        above 1 has no bus equivalent (play once or loop), so it plays once."""
        return self.repeats < 0


def parse_msp_line(text: str, *, allow_midline: bool = True) -> tuple[str, list[SoundCue]]:
    """Return (text with honoured MSP tags removed, cues in order of appearance).

    With ``allow_midline=False`` only tags that open the line are honoured, and one buried in
    prose stays as ordinary text. That is what the spec asks for, and the reason bites hardest
    here: a server relays player text verbatim, so ``!!SOUND(x)`` inside a tell both fires a
    sound and is *deleted from the spoken line* -- a sighted player sees an odd gap, a blind
    player is never told those words existed.

    It defaults to on regardless, because servers are known to append cues to real output
    lines and switching that off would take soundpack audio away wholesale. Wire it to a
    per-world setting rather than changing the default blind.
    """
    cues: list[SoundCue] = []

    def take(kind: str):
        def _repl(match: re.Match[str]) -> str:
            cue = _parse_cue(kind, match.group("body"))
            if cue is not None:
                cues.append(cue)
            return ""

        return _repl

    if allow_midline:
        clean = _SOUND_RE.sub(take("sound"), text)
        clean = _MUSIC_RE.sub(take("music"), clean)
        return clean, cues

    rest = text
    while (match := _LEADING_RE.match(rest)) is not None:
        cue = _parse_cue(match.group("kind").lower(), match.group("body"))
        if cue is not None:
            cues.append(cue)
        rest = rest[match.end() :]
    return rest, cues


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
