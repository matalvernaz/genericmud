"""VoiceRouter: per-channel self-voice with interruption and a fast-output governor.

Live MUD lines flow through the governor on the ``main`` channel: a spam burst
self-voices up to the rate budget, then coalesces the rest into an "N more lines"
summary, while the full text always stays in the buffer for review. Other channels
(tells, combat, system) are ungoverned. Passthrough mode mutes the router so the
renderer's ARIA live region speaks instead.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from genericmud.voice.backends.base import VoiceBackend
from genericmud.voice.governor import TokenBucket

DEFAULT_RATE = 20  # max self-voiced lines/sec on the governed channel before coalescing
MAIN_CHANNEL = "main"


class VoiceRouter:
    def __init__(
        self,
        backend: VoiceBackend,
        *,
        rate: float = DEFAULT_RATE,
        governed_channel: str = MAIN_CHANNEL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._bucket = TokenBucket(rate, rate, clock)
        self._governed = governed_channel
        self._suppressed = 0
        self._muted = False

    def speak(self, text: str, channel: str = MAIN_CHANNEL, interrupt: bool = False) -> None:
        if self._muted:
            return
        if channel == self._governed:
            if not self._bucket.take():
                self._suppressed += 1
                return
            if self._suppressed:
                self._backend.speak(f"{self._suppressed} more lines")
                self._suppressed = 0
        if interrupt:
            self._backend.stop()
        self._backend.speak(text)

    def flush(self) -> None:
        """Stop current speech and drop the suppressed-line backlog (F11)."""
        self._backend.stop()
        self._suppressed = 0

    def set_muted(self, muted: bool) -> None:
        """Mute self-voice (passthrough mode lets the screen reader read instead)."""
        self._muted = muted
