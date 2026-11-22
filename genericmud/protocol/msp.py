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
        """``L=-1`` means loop until stopped — how servers start ambience. Any other count
        plays the cue that many times."""
        return self.repeats < 0


def parse_msp_line(
    text: str, *, allow_midline: bool = True, kept: list[bool] | None = None
) -> tuple[str, list[SoundCue]]:
    """Return (text with honoured MSP tags removed, cues in order of appearance).

    With ``allow_midline=False`` only tags that open the line are honoured, and one buried in
    prose stays as ordinary text. That is what the spec asks for, and the reason bites hardest
    here: a server relays player text verbatim, so ``!!SOUND(x)`` inside a tell both fires a
    sound and is *deleted from the spoken line* -- a sighted player sees an odd gap, a blind
    player is never told those words existed.

    It defaults to on regardless, because servers are known to append cues to real output
    lines and switching that off would take soundpack audio away wholesale. Wire it to a
    per-world setting rather than changing the default blind.

    ``kept``, when given, is filled with one flag per character of ``text``: whether it is still
    in the result. The engine uses it to carry a position in ``text`` across to the result.
    """
    cues: list[SoundCue] = []

    if allow_midline:
        # SOUND first, then MUSIC on what is left, as two passes: a MUSIC tag whose halves
        # only meet once a SOUND tag between them is gone still counts, as it always has.
        # ``survivors`` is, for each character still in ``clean``, where it was in ``text``.
        clean = text
        survivors = list(range(len(text)))
        for pattern, kind in ((_SOUND_RE, "sound"), (_MUSIC_RE, "music")):
            pieces: list[str] = []
            still: list[int] = []
            position = 0
            for match in pattern.finditer(clean):
                cue = _parse_cue(kind, match.group("body"))
                if cue is not None:
                    cues.append(cue)
                pieces.append(clean[position:match.start()])
                still.extend(survivors[position:match.start()])
                position = match.end()
            pieces.append(clean[position:])
            still.extend(survivors[position:])
            clean = "".join(pieces)
            survivors = still
        if kept is not None:
            kept[:] = [False] * len(text)
            for index in survivors:
                kept[index] = True
        return clean, cues

    rest = text
    removed = 0
    while (match := _LEADING_RE.match(rest)) is not None:
        cue = _parse_cue(match.group("kind").lower(), match.group("body"))
        if cue is not None:
            cues.append(cue)
        rest = rest[match.end() :]
        removed += match.end()
    if kept is not None:
        kept[:] = [index >= removed for index in range(len(text))]
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
        url=_unquote(params.get("U", "")),
    )


def _unquote(value: str) -> str:
    """A U= value without the double quotes the spec allows around it."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _to_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
