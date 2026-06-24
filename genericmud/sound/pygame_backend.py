"""Native audio backend: plays SoundBus cues through pygame.mixer.

The bus computes effective gain; this backend maps each category to a pygame
mixer channel, applies stereo pan, loops looped cues, and routes the ``music``
category to the streaming music channel. File paths arriving here are already
absolute (ScriptApi resolved them against the pack dir), so no base directory is
needed. Decoded sounds are cached per path.

pygame is an optional dependency (the ``audio`` extra; the bundled Windows build
ships it). The web path posts protocol messages instead and needs none of this.
Build with :func:`make_pygame_backend`, which inits the mixer and returns ``None``
when pygame is absent or there is no audio device — callers fall back to the
post backend.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genericmud.session.diaglog import DiagnosticLog

MUSIC_CATEGORY = "music"
_DEFAULT_CHANNELS = 32  # the mixer is process-global; give concurrent sessions room

# Process-wide cursor so a category in session A and one in session B don't land on
# the same pygame channel (which would let B's sound cut A's). Distinctness is what
# matters, not the absolute index.
_next_channel = 0


def _allocate_channel_index(count: int) -> int:
    global _next_channel
    index = _next_channel % count
    _next_channel += 1
    return index


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def stereo_volume(gain: float, pan: float) -> tuple[float, float]:
    """Map gain + pan (-1 left .. 0 center .. 1 right) to (left, right), each 0..1."""
    pan = max(-1.0, min(1.0, pan))
    return _clamp(gain * min(1.0, 1.0 - pan)), _clamp(gain * min(1.0, 1.0 + pan))


class PygameSoundBackend:
    """A SoundBus backend over an injected ``pygame.mixer`` (or a compatible stub)."""

    def __init__(
        self,
        mixer,
        on_error: Callable[[str], None] | None = None,
        diag: DiagnosticLog | None = None,
    ) -> None:
        self._mixer = mixer
        self._on_error = on_error
        self._diag = diag  # separate from on_error: trace every attempt, not deduped
        self._sounds: dict[str, object] = {}  # path -> Sound (decode cache)
        self._channels: dict[str, object] = {}  # category -> Channel
        self._warned: set[str] = set()  # paths already reported, so a missing cue warns once

    def play(self, file: str, channel: str, gain: float, pan: float, loop: bool) -> None:
        sound = self._sound(file)
        if sound is None:  # missing/undecodable file: skip the cue, don't crash the line
            self._trace(file, "SKIP", gain=gain)
            return
        try:
            mixer_channel = self._channel(channel)
            mixer_channel.play(sound, loops=-1 if loop else 0)
            mixer_channel.set_volume(*stereo_volume(gain, pan))
        except Exception as exc:  # noqa: BLE001 - a mixer fault must not crash the line; record it
            self._trace(file, "EXC", exc=f"{type(exc).__name__}: {exc}")
            return
        self._trace(file, "OK", gain=gain)

    def music(self, file: str, channel: str, gain: float) -> None:
        try:
            self._mixer.music.load(file)
        except Exception:  # noqa: BLE001 - a missing/bad music file must not crash the line
            self._warn(file)
            self._trace(file, "SKIP", kind="music")
            return
        self._mixer.music.set_volume(_clamp(gain))
        self._mixer.music.play(loops=-1)  # background music loops until stopped
        self._trace(file, "OK", kind="music", gain=gain)

    def _trace(self, file: str, result: str, **fields: object) -> None:
        if self._diag is not None:
            self._diag.event("backend.play", file=file, result=result, **fields)

    def _warn(self, file: str) -> None:
        """Report a cue we couldn't play, once per path (a flood would fire every line)."""
        if self._on_error is None or file in self._warned:
            return
        self._warned.add(file)
        self._on_error(
            f"sound not played: {file} -- file missing or unsupported format; "
            "set the world's Sounds folder if your sounds are elsewhere"
        )

    def stop(self, channel: str) -> None:
        if channel == MUSIC_CATEGORY:
            self._mixer.music.stop()
        existing = self._channels.get(channel)
        if existing is not None:
            existing.stop()

    def _channel(self, category: str):
        channel = self._channels.get(category)
        if channel is None:
            channel = self._mixer.Channel(_allocate_channel_index(self._mixer.get_num_channels()))
            self._channels[category] = channel
        return channel

    def _sound(self, file: str):
        sound = self._sounds.get(file)
        if sound is None:
            try:
                sound = self._mixer.Sound(file)
            except Exception:  # noqa: BLE001 - missing/undecodable file: caller skips the cue
                self._warn(file)
                return None
            self._sounds[file] = sound
        return sound


def make_pygame_backend(
    on_error: Callable[[str], None] | None = None,
    diag: DiagnosticLog | None = None,
) -> PygameSoundBackend | None:
    """Init the pygame mixer and wrap it, or None if pygame/audio is unavailable.

    ``on_error`` receives a human-readable reason when the backend can't be built (so the
    caller can tell the user sound is off, rather than silently dropping every cue) and is
    forwarded to the backend for per-file failures. ``diag`` records the selection result so
    a build that silently fell back to the no-op poster is visible after the fact.
    """

    def select(result: str, reason: str) -> None:
        if diag is not None:
            diag.event("backend.select", result=result, reason=reason)

    try:
        import pygame
    except ImportError:
        if on_error is not None:
            on_error("sound is off: pygame is not installed in this build")
        select("none", "no-pygame")
        return None
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.set_num_channels(_DEFAULT_CHANNELS)
    except pygame.error:
        if on_error is not None:
            on_error("sound is off: no audio device is available")
        select("none", "no-audio-device")
        return None  # no audio device (headless server, locked device, etc.)
    select("pygame", "ok")
    return PygameSoundBackend(pygame.mixer, on_error=on_error, diag=diag)
