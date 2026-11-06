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
# A cue's state, numbered as BASS numbers them, so a pack's BASS wrapper hears what it expects.
STOPPED = 0
PLAYING = 1
PAUSED = 3


@dataclass(frozen=True)
class BusPolicy:
    gain: float = 1.0
    muted: bool = False


@dataclass(frozen=True)
class CueTiming:
    """How a cue plays within its file, as Client.Media asks: where each time round starts and
    finishes (seconds into the file, 0 for the file's own start and end), and how long its volume
    takes to rise from silence at the start and fall to silence at the end (seconds, 0 for no
    fade). Fades are linear and happen every time round, as in Mudlet."""

    start: float = 0.0
    finish: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0


@dataclass(frozen=True)
class SourceInfo:
    """What a sound file holds: how long it plays, in seconds, its sample rate in Hz, and how
    many channels it has."""

    seconds: float
    rate: int
    channels: int


class SoundBackend:
    """Playback surface the bus drives; real wiring (renderer/native) overrides."""

    def play(
        self, file: str, channel: str, gain: float, pan: float, loop: bool, times: int = 1,
        timing: CueTiming | None = None,
    ) -> None:
        """Play ``file``; ``loop`` repeats it until stopped, else it plays ``times`` times.
        A backend that can't follow ``timing`` plays the file whole."""
    def music(
        self, file: str, channel: str, gain: float, times: int = -1,
        timing: CueTiming | None = None,
    ) -> None:
        """Play ``file`` as the music; ``times`` -1 loops it until stopped."""
    def stop(self, channel: str) -> None: ...

    def fade_away(self, channel: str, seconds: float) -> None:
        """Fade a playing cue to silence over ``seconds``, then stop it; a backend that can't
        fade stops it at once."""
        self.stop(channel)

    def is_playing(self, channel: str) -> bool:
        """Whether a cue is still audible on this channel; backends that can't know
        (the renderer post path) answer False, matching the pre-query behaviour."""
        return False

    def adjust(
        self, channel: str, gain: float | None = None, pan: float | None = None,
        frequency: float | None = None,
    ) -> None:
        """Re-level, re-pan or re-pitch a playing cue (``frequency`` is the rate it plays at, in
        Hz, 0 for its own); backends without live control ignore it."""

    def slide(
        self, channel: str, gain: float | None = None, pan: float | None = None,
        frequency: float | None = None, seconds: float = 0.0,
    ) -> None:
        """Move a playing cue's volume, pan or pitch to a new value over ``seconds``; a backend
        that can't glide sets it at once."""
        self.adjust(channel, gain, pan, frequency=frequency)

    def pause(self, channel: str) -> None:
        """Hold a playing cue where it is, for :meth:`resume`; backends that can't, don't."""

    def resume(self, channel: str) -> None:
        """Carry on a paused cue from where it was held."""

    def state(self, channel: str) -> int:
        """STOPPED, PLAYING or PAUSED."""
        return PLAYING if self.is_playing(channel) else STOPPED

    def position(self, channel: str) -> float:
        """How far into its file the cue is, in seconds; 0 when the backend can't tell."""
        return 0.0

    def seek(self, channel: str, seconds: float) -> None:
        """Move a cue to ``seconds`` into its file; backends that can't, don't."""

    def set_looping(self, channel: str, loop: bool) -> None:
        """Have a playing cue go round again when it ends, or stop going round."""

    def source_info(self, file: str) -> SourceInfo | None:
        """What ``file`` holds, without playing it; None when the backend can't tell."""
        return None

    def add_effect(
        self, channel: str, kind: int, settings: dict[str, float] | None = None,
        priority: int = 0,
    ) -> int:
        """Put one of BASS's DirectX 8 effects on a playing cue (see bass_effects); answers a
        handle for it, or 0 when the backend can't. Only BASS has effects."""
        return 0

    def set_effect(self, channel: str, effect: int, settings: dict[str, float]) -> bool:
        """Change an effect's settings; False when it isn't there or a value is refused."""
        return False

    def effect_settings(self, channel: str, effect: int) -> dict[str, float] | None:
        return None

    def remove_effect(self, channel: str, effect: int) -> None:
        """Take an effect off its cue."""

    def reset_effects(self, channel: str, effect: int = 0) -> None:
        """Clear what an effect has built up, or every effect's on the cue for 0."""


class SoundBus:
    def __init__(self, backend: SoundBackend | None = None, *, master: float = 1.0) -> None:
        self._backend = backend or SoundBackend()
        self._master = max(0.0, master)
        self._policies: dict[str, BusPolicy] = {}
        self._playing: set[str] = set()
        # How many times all sound was cut, so a cue still waiting on its file can tell.
        self.flushes = 0

    def set_backend(self, backend: SoundBackend) -> None:
        """Swap playback backends; renderer_backend.py posts sound requests to the web window."""
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
        times: int = 1,
        timing: CueTiming | None = None,
    ) -> None:
        """Play a cue; ``loop`` repeats it until stopped, otherwise it plays ``times`` times
        (a MUD's MSP L= or Client.Media loops). ``timing`` trims and fades it."""
        self._playing.add(channel)
        effective = self.effective_gain(channel, gain)
        if timing is None:  # backends written before timing existed still play cues
            self._backend.play(file, channel, effective, pan, loop, times=max(1, times))
        else:
            self._backend.play(
                file, channel, effective, pan, loop, times=max(1, times), timing=timing
            )

    def music(
        self, file: str, channel: str = MUSIC_CATEGORY, gain: float = 1.0, times: int = -1,
        timing: CueTiming | None = None,
    ) -> None:
        """Play music: until stopped (-1, what soundpacks' background music wants), or the
        number of times a MUD asked for."""
        self._playing.add(channel)
        effective = self.effective_gain(channel, gain)
        if timing is None:
            self._backend.music(file, channel, effective, times=times)
        else:
            self._backend.music(file, channel, effective, times=times, timing=timing)

    def stop(self, channel: str) -> None:
        self._playing.discard(channel)
        self._backend.stop(channel)

    def fade_away(self, channel: str, seconds: float) -> None:
        """Fade a playing cue out over ``seconds`` and stop it (Client.Media's fadeaway). It
        stays among the channels flush() stops: a fade can take seconds, and the stop-all key
        must end it too."""
        own_fade = getattr(type(self._backend), "fade_away", None)
        if own_fade is None or own_fade is SoundBackend.fade_away:
            self.stop(channel)  # a backend that can't fade stops the cue at once
        else:
            self._backend.fade_away(channel, seconds)

    def is_playing(self, channel: str) -> bool:
        # The backend is the truth (a one-shot ends on its own); `_playing` only
        # records what was started, so it can't answer this.
        return self._backend.is_playing(channel)

    def adjust(
        self, channel: str, gain: float | None = None, pan: float | None = None,
        frequency: float | None = None,
    ) -> None:
        """Live volume/pan/pitch change on a playing cue. ``gain`` is the CUE gain; the
        master and category gains scale it exactly as at play time."""
        effective = self.effective_gain(channel, gain) if gain is not None else None
        if frequency is None:
            self._backend.adjust(channel, effective, pan)
        else:
            self._backend.adjust(channel, effective, pan, frequency=frequency)

    def slide(
        self, channel: str, gain: float | None = None, pan: float | None = None,
        frequency: float | None = None, seconds: float = 0.0,
    ) -> None:
        """Glide a playing cue's volume, pan or pitch over ``seconds``. ``gain`` is the CUE gain,
        scaled like adjust's; below 0 it fades the cue away and stops it, as BASS does."""
        if gain is not None and gain < 0:
            self.fade_away(channel, seconds)
            gain = None
            if pan is None and frequency is None:
                return
        effective = self.effective_gain(channel, gain) if gain is not None else None
        self._backend.slide(channel, effective, pan, frequency=frequency, seconds=seconds)

    def pause(self, channel: str) -> None:
        self._backend.pause(channel)

    def resume(self, channel: str) -> None:
        self._playing.add(channel)
        self._backend.resume(channel)

    def state(self, channel: str) -> int:
        """STOPPED, PLAYING or PAUSED, as BASS numbers them."""
        return self._backend.state(channel)

    def position(self, channel: str) -> float:
        return self._backend.position(channel)

    def seek(self, channel: str, seconds: float) -> None:
        self._backend.seek(channel, max(0.0, seconds))

    def set_looping(self, channel: str, loop: bool) -> None:
        self._backend.set_looping(channel, loop)

    def source_info(self, file: str) -> SourceInfo | None:
        return self._backend.source_info(file)

    def add_effect(
        self, channel: str, kind: int, settings: dict[str, float] | None = None,
        priority: int = 0,
    ) -> int:
        return self._backend.add_effect(channel, kind, settings, priority)

    def set_effect(self, channel: str, effect: int, settings: dict[str, float]) -> bool:
        return self._backend.set_effect(channel, effect, settings)

    def effect_settings(self, channel: str, effect: int) -> dict[str, float] | None:
        return self._backend.effect_settings(channel, effect)

    def remove_effect(self, channel: str, effect: int) -> None:
        self._backend.remove_effect(channel, effect)

    def reset_effects(self, channel: str, effect: int = 0) -> None:
        self._backend.reset_effects(channel, effect)

    def flush(self) -> None:
        """Stop every playing category (the sound panic key, e.g. Shift+F11)."""
        self.flushes += 1
        for channel in sorted(self._playing):
            self._backend.stop(channel)
        self._playing.clear()
