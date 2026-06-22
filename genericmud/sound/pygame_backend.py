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

    def __init__(self, mixer) -> None:
        self._mixer = mixer
        self._sounds: dict[str, object] = {}  # path -> Sound (decode cache)
        self._channels: dict[str, object] = {}  # category -> Channel

    def play(self, file: str, channel: str, gain: float, pan: float, loop: bool) -> None:
        sound = self._sound(file)
        if sound is None:  # missing/undecodable file: skip the cue, don't crash the line
            return
        mixer_channel = self._channel(channel)
        mixer_channel.play(sound, loops=-1 if loop else 0)
        mixer_channel.set_volume(*stereo_volume(gain, pan))

    def music(self, file: str, channel: str, gain: float) -> None:
        try:
            self._mixer.music.load(file)
        except Exception:  # noqa: BLE001 - a missing/bad music file must not crash the line
            return
        self._mixer.music.set_volume(_clamp(gain))
        self._mixer.music.play(loops=-1)  # background music loops until stopped

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
                return None
            self._sounds[file] = sound
        return sound


def make_pygame_backend() -> PygameSoundBackend | None:
    """Init the pygame mixer and wrap it, or None if pygame/audio is unavailable."""
    try:
        import pygame
    except ImportError:
        return None
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.set_num_channels(_DEFAULT_CHANNELS)
    except pygame.error:
        return None  # no audio device (headless server, locked device, etc.)
    return PygameSoundBackend(pygame.mixer)
