"""VoiceRouter: per-channel self-voice with interruption and a fast-output governor.

Live MUD lines flow through the governor on the ``main`` channel. Burst capacity and
sustained rate are deliberately separate numbers: MUD output arrives in whole TCP
packets, so a room description, a ``who`` list or a help page all land inside the same
millisecond with no elapsed time to refill against. Sizing the burst to the rate made
every one of those trip the governor. The burst therefore absorbs a screenful outright
and only genuinely sustained spam coalesces, into an "N more lines" summary; the full
text always stays in the buffer for review either way. Other channels (tells, combat,
system) are ungoverned. Passthrough mode mutes the router so the renderer's ARIA live
region speaks instead.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from genericmud.voice.backends.base import VoiceBackend
from genericmud.voice.governor import TokenBucket

DEFAULT_RATE = 20  # sustained self-voiced lines/sec on the governed channel
DEFAULT_BURST = 200  # lines absorbed from a single arrival: a couple of screenfuls
MAIN_CHANNEL = "main"
REVIEW_CHANNEL = "review"  # answers to a review gesture: Alt+Up/Down, Ctrl+1-9, Alt+T/C
SYSTEM_CHANNEL = "system"  # the client talking about itself, not the MUD
# Ctrl+M exists so the MUD transcript stops and the user can read the output box with their
# screen reader's own commands. It must not silence the client's reply to a keypress: the
# spoken line IS the entire result of pressing Alt+Up, and the review path has no visual
# fallback (app._speak_review posts protocol.review, which the wx UI does not render), so
# muting it leaves keys that appear to do nothing whatsoever.
_ALWAYS_SPEAK = frozenset({REVIEW_CHANNEL, SYSTEM_CHANNEL})


def _backlog_notice(count: int) -> str:
    """Phrase the coalesced-line summary so it reads as speech, not as a counter."""
    return "1 more line" if count == 1 else f"{count} more lines"


class VoiceRouter:
    def __init__(
        self,
        backend: VoiceBackend,
        *,
        rate: float = DEFAULT_RATE,
        burst: float = DEFAULT_BURST,
        governed_channel: str = MAIN_CHANNEL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._bucket = TokenBucket(burst, rate, clock)
        self._governed = governed_channel
        self._suppressed = 0
        self._muted = False

    def speak(self, text: str, channel: str = MAIN_CHANNEL, interrupt: bool = False) -> None:
        if self._muted and channel not in _ALWAYS_SPEAK:
            return
        if channel == self._governed and not self._bucket.take():
            self._suppressed += 1
            return
        if interrupt:
            self._safe(self._backend.stop)  # barge in BEFORE the summary so it isn't truncated
        if channel == self._governed and self._suppressed:
            self._safe(self._backend.speak, _backlog_notice(self._suppressed))
            self._suppressed = 0
        self._safe(self._backend.speak, text)

    def flush(self) -> None:
        """Stop current speech and drop the suppressed-line backlog (F11)."""
        self._safe(self._backend.stop)
        self._suppressed = 0

    def interrupt(self) -> None:
        """Stop current speech but keep the suppressed-line count.

        Follow mode barges in on room movement; the "N more lines" notice still
        owes the user those unheard lines, so only the utterance is cut.
        """
        self._safe(self._backend.stop)

    def _safe(self, action: Callable[..., None], *args: str) -> None:
        """Call a backend method, swallowing any fault.

        A SAPI COM hiccup or a vanished NVDA controller raising out of ``speak``/``stop`` would
        otherwise propagate into the engine's read loop -- dropping the connection AND silencing
        every later line. For a self-voicing app whose users are blind, that cascade is the worst
        outcome, so a speech fault drops just this utterance; the next call tries again.
        """
        try:
            action(*args)
        except Exception:  # noqa: BLE001 - a speech-backend fault must never crash or mute the app
            return

    def set_muted(self, muted: bool) -> None:
        """Mute self-voice (passthrough mode lets the screen reader read instead)."""
        self._muted = muted
