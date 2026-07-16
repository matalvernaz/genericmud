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

from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genericmud.session.diaglog import DiagnosticLog

MUSIC_CATEGORY = "music"
_DEFAULT_CHANNELS = 32  # the mixer is process-global; give concurrent sessions room
# Bound the decode cache so a hostile MSP stream / noisy pack that plays thousands of unique
# filenames can't grow memory for the life of the session. LRU: least-recently-played is evicted.
_MAX_CACHED_SOUNDS = 256

# Process-wide scan cursor so concurrent sessions spread their cues across the shared
# mixer instead of all crowding channel 0. Allocation scans from here for a FREE channel
# (see _alloc_index), so distinctness -- not the absolute index -- is what matters.
_next_channel = 0


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
        self._sounds: OrderedDict[str, object] = OrderedDict()  # path -> Sound (bounded LRU cache)
        self._channels: dict[str, object] = {}  # category -> Channel (only entries still live)
        self._indices: dict[str, int] = {}  # category -> physical mixer channel index
        self._loops: dict[str, bool] = {}  # category -> is it a looping cue (protected from eviction)
        self._warned: set[str] = set()  # paths already reported, so a missing cue warns once

    def play(self, file: str, channel: str, gain: float, pan: float, loop: bool) -> None:
        sound = self._sound(file)
        if sound is None:  # missing/undecodable file: skip the cue, don't crash the line
            self._trace(file, "SKIP", gain=gain)
            return
        try:
            mixer_channel = self._channel(channel, loop)
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

    def is_playing(self, channel: str) -> bool:
        """Whether a cue is still audibly playing on this logical channel.

        Truthful per-cue status is what soundpack switching logic keys off: Erion's
        ambience/music handlers ask ``isPlaying(old)`` to decide between "replace the
        running cue" and "just start the new one" -- an always-false answer makes them
        stack a new ambience on top of the old every room change.
        """
        if channel == MUSIC_CATEGORY:
            try:
                return bool(self._mixer.music.get_busy())
            except Exception:  # noqa: BLE001 - a mixer probe fault reads as "not playing"
                return False
        existing = self._channels.get(channel)
        if existing is None:
            return False
        return self._is_busy(existing)

    def _channel(self, category: str, loop: bool = False):
        """The pygame channel for a category, allocating a free physical slot on first use.

        Reusing the SAME category returns its channel (a new cue replaces the old one on it --
        the intended per-logical-channel behaviour). A NEW category gets a channel that isn't
        currently in use, so distinct cues never collide. This matters because the MUSHclient
        ``audio`` shim mints a fresh category per cue (erion-audio-1, -2, ...): the old monotonic
        ``index % num_channels`` allocator wrapped after num_channels cues and handed a new
        footstep the same physical channel as the looping area music, cutting the music out.
        """
        existing = self._channels.get(category)
        if existing is not None:
            self._loops[category] = loop  # a re-fire on the same category may change loopiness
            return existing
        index = self._alloc_index()
        channel = self._mixer.Channel(index)
        self._channels[category] = channel
        self._indices[category] = index
        self._loops[category] = loop
        return channel

    def _alloc_index(self) -> int:
        """Pick a physical channel index not held by a still-playing cue.

        Sweeps out finished one-shots first (freeing their slot and bounding the maps), then
        scans from the process-wide cursor for a free index. Under full pressure it evicts a
        one-shot before ever stealing a looping cue, so music/ambience survive a flood of SFX.
        """
        global _next_channel
        count = self._mixer.get_num_channels()
        used = self._reap()
        for offset in range(count):
            index = (_next_channel + offset) % count
            if index not in used:
                _next_channel = (index + 1) % count
                return index
        # Every channel is busy: evict the oldest non-looping cue rather than a looping one.
        for cat in list(self._channels):
            if not self._loops.get(cat):
                index = self._indices[cat]
                self._release(cat)
                _next_channel = (index + 1) % count
                return index
        # Everything live is a loop -- an unavoidable steal; take the channel at the cursor.
        index = _next_channel % count
        _next_channel = (index + 1) % count
        return index

    def _reap(self) -> set[int]:
        """Indices still held by playing cues; drop finished one-shots so their slot frees up."""
        used: set[int] = set()
        for category, channel in list(self._channels.items()):
            if self._is_busy(channel):
                used.add(self._indices[category])
            else:
                self._release(category)
        return used

    def _release(self, category: str) -> None:
        self._channels.pop(category, None)
        self._indices.pop(category, None)
        self._loops.pop(category, None)

    @staticmethod
    def _is_busy(channel: object) -> bool:
        getter = getattr(channel, "get_busy", None)
        if getter is None:
            return True  # can't tell (a stub without get_busy): assume live, so we never yank it
        try:
            return bool(getter())
        except Exception:  # noqa: BLE001 - a mixer probe fault must not abort allocation
            return True

    def _sound(self, file: str):
        cached = self._sounds.get(file)
        if cached is not None:
            self._sounds.move_to_end(file)  # most-recently-used
            return cached
        try:
            sound = self._mixer.Sound(file)
        except Exception:  # noqa: BLE001 - missing/undecodable file: caller skips the cue
            self._warn(file)
            return None
        self._sounds[file] = sound
        while len(self._sounds) > _MAX_CACHED_SOUNDS:
            self._sounds.popitem(last=False)  # evict least-recently-used
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
