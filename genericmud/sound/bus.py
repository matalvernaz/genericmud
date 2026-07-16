"""SoundBus: per-category SFX mixing + lifecycle (the audio analog of VoiceRouter).

Soundpack ``play()``/``music()`` calls are grouped into categories
(``sound``/``music``/``ambient``/``ui``/...). Each category carries a gain and a
mute flag; a master gain scales every category. The bus computes the effective
gain per cue, forwards play/stop to an injected backend, and tracks what is
playing so a single :meth:`flush` silences everything (the panic key).

No audio device lives here — the backend (the renderer's Web Audio today, a
native mixer later) does the playback, so the control layer is unit-testable
headless. Mute gates *future* cues; use :meth:`stop`/:meth:`flush` to cut audio
that is already playing (a looped ambience won't change gain retroactively).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

DEFAULT_CATEGORY = "sound"
MUSIC_CATEGORY = "music"


@dataclass(frozen=True)
class BusPolicy:
    gain: float = 1.0
    muted: bool = False


class SoundBackend:
    """Playback surface the bus drives; real wiring (renderer/native) overrides."""

    def play(
        self, file: str, channel: str, gain: float, pan: float, loop: bool
    ) -> None: ...
    def music(self, file: str, channel: str, gain: float) -> None: ...
    def stop(self, channel: str) -> None: ...

    def is_playing(self, channel: str) -> bool:
        """Whether a cue is still audible on this channel; backends that can't know
        (the renderer post path) answer False, matching the pre-query behaviour."""
        return False

    def adjust(self, channel: str, gain: float | None = None, pan: float | None = None) -> None:
        """Re-level/re-pan a playing cue; backends without live control ignore it."""


class SoundBus:
    def __init__(self, backend: SoundBackend | None = None, *, master: float = 1.0) -> None:
        self._backend = backend or SoundBackend()
        self._master = max(0.0, master)
        self._policies: dict[str, BusPolicy] = {}
        self._playing: set[str] = set()

    def set_backend(self, backend: SoundBackend) -> None:
        """Swap the playback backend (e.g. EngineApp injects the renderer poster)."""
        self._backend = backend

    # --- policy ---

    def policy(self, category: str) -> BusPolicy:
        return self._policies.get(category, BusPolicy())

    def set_policy(self, category: str, policy: BusPolicy) -> None:
        self._policies[category] = policy

    def set_volume(self, category: str, gain: float) -> None:
        self._policies[category] = replace(self.policy(category), gain=max(0.0, gain))

    def set_muted(self, category: str, muted: bool) -> None:
        self._policies[category] = replace(self.policy(category), muted=bool(muted))

    def set_master(self, gain: float) -> None:
        self._master = max(0.0, gain)

    @property
    def master(self) -> float:
        return self._master

    def effective_gain(self, category: str, gain: float = 1.0) -> float:
        policy = self.policy(category)
        if policy.muted:
            return 0.0
        return self._master * policy.gain * gain

    # --- playback ---

    def play(
        self,
        file: str,
        channel: str = DEFAULT_CATEGORY,
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
    ) -> None:
        self._playing.add(channel)
        self._backend.play(file, channel, self.effective_gain(channel, gain), pan, loop)

    def music(self, file: str, channel: str = MUSIC_CATEGORY) -> None:
        self._playing.add(channel)
        self._backend.music(file, channel, self.effective_gain(channel))

    def stop(self, channel: str) -> None:
        self._playing.discard(channel)
        self._backend.stop(channel)

    def is_playing(self, channel: str) -> bool:
        # The backend is the truth (a one-shot ends on its own); `_playing` only
        # records what was started, so it can't answer this.
        return self._backend.is_playing(channel)

    def adjust(self, channel: str, gain: float | None = None, pan: float | None = None) -> None:
        """Live volume/pan change on a playing cue. ``gain`` is the CUE gain; the
        master and category gains scale it exactly as at play time."""
        effective = self.effective_gain(channel, gain) if gain is not None else None
        self._backend.adjust(channel, effective, pan)

    def flush(self) -> None:
        """Stop every playing category (the sound panic key, e.g. Shift+F11)."""
        for channel in sorted(self._playing):
            self._backend.stop(channel)
        self._playing.clear()
