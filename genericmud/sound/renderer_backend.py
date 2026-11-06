"""The web window's sound backend: post playback requests to the renderer."""

from __future__ import annotations

from genericmud.bridge import protocol
from genericmud.sound.bus import CueTiming, SoundBackend


class _PostSoundBackend(SoundBackend):
    """SoundBus backend that posts renderer protocol messages (the Web Audio path)."""

    def __init__(self, post) -> None:
        self._post = post

    def play(
        self, file: str, channel: str, gain: float, pan: float, loop: bool, times: int = 1,
        timing: CueTiming | None = None,
    ) -> None:
        # The renderer can't count, trim or fade: it plays the file whole.
        self._post(protocol.sound(file, channel, gain, pan, loop))

    def music(
        self, file: str, channel: str, gain: float, times: int = -1,
        timing: CueTiming | None = None,
    ) -> None:
        self._post(protocol.music(file, channel, gain))

    def stop(self, channel: str) -> None:
        self._post(protocol.stop_sound(channel))
