"""Native audio backend: plays SoundBus cues through BASS, the library soundpacks are made for.

The same contract as the pygame backend: one cue at a time on each logical channel (a new cue
there replaces the one playing), one music stream, live volume and pan changes, and a file
that's missing or unreadable reported once. BASS mixes any number of streams at once, so
unlike pygame there's no pool of channels to share out and no cue is ever cut off for another.

It also does what pygame couldn't: a cue's pitch changes live (VIPMud's #PC Frequency), a
cue starts and finishes where a MUD says within its file and fades in, out and away
(Client.Media), each done the way Mudlet does it, and a cue takes BASS's effects (the echo,
reverb, flanger and gargle ChatMUD asks Chatpack for; see bass_effects).

Build it with :func:`make_sound_backend`, which uses BASS when the build has it and an audio
device opens, and pygame otherwise.
"""

from __future__ import annotations

import ctypes
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from genericmud.config.audio_routes import AudioOutput
from genericmud.sound import bass_effects, bass_library
from genericmud.sound.bass_library import (
    ACTIVE_STOPPED,
    ATTRIB_FREQ,
    ATTRIB_PAN,
    ATTRIB_VOL,
    POS_BYTE,
    POS_END,
    POS_LOOP,
    SAMPLE_LOOP,
    STREAM_AUTOFREE,
    SYNC_END,
    SYNC_FREE,
    SYNC_MIXTIME,
    SYNC_POS,
)
from genericmud.sound.bus import CueTiming, SoundBackend, SourceInfo
from genericmud.sound.output_devices import AudioRouteUnavailable, output_context

if TYPE_CHECKING:
    from genericmud.session.diaglog import DiagnosticLog

MUSIC_CATEGORY = "music"
_SLIDE_TO_STOP = -1.0  # sliding BASS's volume to -1 fades to silence and stops the stream

# What each of BASS's syncs does, by the number BASS hands back with it, for every session. A
# cue's actions leave as BASS frees its stream, from whichever thread frees it.
_actions: dict[int, Callable[[int], None]] = {}
_action_numbers = itertools.count(1)  # from 1: BASS hands back 0 as nothing


def _run_action(_sync: int, channel: int, _data: int, number: object) -> None:
    """Run the action filed under ``number``, if it's still filed."""
    action = _actions.get(number)
    if action is not None:
        action(channel)


# The one callback BASS holds, for every sync of every cue. ctypes frees a callback nothing
# refers to, and BASS calling one that's been freed would crash genericMud; this one lasts as
# long as genericMud, so whatever thread BASS calls from and whenever, it's there.
_dispatch = bass_library.SyncProc(_run_action)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class _Effect:
    """An effect on a cue. A gargle made from BASS_FX's volume envelope keeps its own settings
    and the envelope BASS reads."""

    kind: int
    envelope: bass_effects.GargleEnvelope | None = None
    settings: dict[str, float] = field(default_factory=dict)


@dataclass
class _Cue:
    """A playing stream and what its callbacks need, which adjust() and fade_away() change."""

    stream: int
    gain: float
    pan: float
    fade_in: float = 0.0  # seconds, each time round
    end: int = 0  # byte position it ends at each time round
    fading_away: bool = False
    effects: dict[int, _Effect] = field(default_factory=dict)  # by BASS's handle for each
    actions: list[int] = field(default_factory=list)  # the numbers its syncs' actions are under
    freed: bool = False  # BASS has freed the stream, so its handle mustn't be used again


class BassSoundBackend(SoundBackend):
    """A SoundBus backend over BASS's functions (the loaded library, or a stand-in in tests)."""

    def __init__(
        self,
        bass,
        on_error: Callable[[str], None] | None = None,
        diag: DiagnosticLog | None = None,
        *, route_devices: bool = False,
    ) -> None:
        self._bass = bass
        self._route_devices = route_devices
        self._on_error = on_error
        self._diag = diag  # separate from on_error: every attempt is traced, not just the first
        self._cues: dict[str, _Cue] = {}  # logical channel -> its cue
        self._music: _Cue | None = None
        self._music_channel: str | None = None  # the logical channel the music was asked on
        self._warned: set[str] = set()  # files already reported, so a missing cue warns once

    def play(
        self, file: str, channel: str, gain: float, pan: float, loop: bool, times: int = 1,
        timing: CueTiming | None = None,
    ) -> None:
        self._play(file, channel, gain, pan, loop, times, timing)

    def play_on_device(
        self, output: AudioOutput, file: str, channel: str, gain: float, pan: float,
        loop: bool, times: int = 1, timing: CueTiming | None = None,
    ) -> None:
        if not self._route_devices:
            raise AudioRouteUnavailable("Audio device routing is unavailable")
        self._play(file, channel, gain, pan, loop, times, timing, output)

    def _play(
        self, file: str, channel: str, gain: float, pan: float, loop: bool,
        times: int, timing: CueTiming | None, output: AudioOutput | None = None,
    ) -> None:
        self._free(self._cues.pop(channel, None))
        repeats = 1 if loop else max(1, times)
        cue = self._open(file, gain, pan, loop=loop, repeats=repeats, timing=timing, output=output)
        if cue is None:
            self._trace(file, "SKIP", gain=gain)
            return
        self._cues[channel] = cue
        started = self._start(file, cue, gain=gain)
        if output is not None and not started:
            code = self._bass.BASS_ErrorGetCode()
            self._free(self._cues.pop(channel, None))
            raise AudioRouteUnavailable(
                f"Cannot play on audio output {output.name} (BASS error {code})"
            )

    def music(
        self, file: str, channel: str, gain: float, times: int = -1,
        timing: CueTiming | None = None,
    ) -> None:
        self._free(self._music)
        self._music = None
        loop = times < 0  # -1 loops until stopped (soundpacks' background music)
        repeats = 1 if loop else max(1, times)
        cue = self._open(file, gain, 0.0, loop=loop, repeats=repeats, timing=timing)
        if cue is None:
            self._trace(file, "SKIP", kind="music")
            return
        self._music = cue
        self._music_channel = channel  # so stop() and adjust() on this channel reach it
        self._start(file, cue, kind="music", gain=gain)

    def stop(self, channel: str) -> None:
        # The music may have been asked for under a channel other than "music"; stop it when
        # either name is asked for, or it plays forever.
        if channel == MUSIC_CATEGORY or channel == self._music_channel:
            self._free(self._music)
            self._music = None
            if channel == self._music_channel:
                self._music_channel = None
        self._free(self._cues.pop(channel, None))

    def fade_away(self, channel: str, seconds: float) -> None:
        """Fade a cue to silence over ``seconds``, or what's left of it if that's less, then
        stop it: Client.Media's fadeaway, as Mudlet does it."""
        cue = self._cue_for(channel)
        if cue is None:
            return
        bass = self._bass
        stream = cue.stream
        position = bass.BASS_ChannelGetPosition(stream, POS_BYTE)
        left = bass.BASS_ChannelBytes2Seconds(stream, max(0, cue.end - position))
        duration = min(seconds, left)
        if duration <= 0 or bass.BASS_ChannelIsActive(stream) == ACTIVE_STOPPED:
            self.stop(channel)
            return
        cue.fading_away = True  # its callbacks leave the volume alone from now on
        bass.BASS_ChannelFlags(stream, 0, SAMPLE_LOOP)  # and it doesn't go round again
        bass.BASS_ChannelSlideAttribute(stream, ATTRIB_VOL, _SLIDE_TO_STOP, _milliseconds(duration))

    def is_playing(self, channel: str) -> bool:
        """Whether a cue is still playing on this logical channel. Soundpacks decide on it
        (Erion asks whether the old ambience still plays before starting the next)."""
        cue = self._cue_for(channel)
        if cue is None:
            return False
        return self._bass.BASS_ChannelIsActive(cue.stream) != ACTIVE_STOPPED

    def adjust(
        self, channel: str, gain: float | None = None, pan: float | None = None,
        frequency: float | None = None,
    ) -> None:
        """Change a playing cue's volume, pan or pitch in place (None leaves that one alone).
        ``frequency`` is the rate it plays at in Hz, as VIPMud's #PC Frequency gives it; 0 puts
        back the file's own."""
        cue = self._cue_for(channel)
        if cue is None:
            return
        bass = self._bass
        if gain is not None and not cue.fading_away:  # a fade on its way out keeps going
            cue.gain = gain
            bass.BASS_ChannelSetAttribute(cue.stream, ATTRIB_VOL, _clamp(gain, 0.0, 1.0))
        if pan is not None:
            cue.pan = pan
            bass.BASS_ChannelSetAttribute(cue.stream, ATTRIB_PAN, _clamp(pan, -1.0, 1.0))
        if frequency is not None and frequency >= 0:
            bass.BASS_ChannelSetAttribute(cue.stream, ATTRIB_FREQ, frequency)

    def slide(
        self, channel: str, gain: float | None = None, pan: float | None = None,
        frequency: float | None = None, seconds: float = 0.0,
    ) -> None:
        """Glide a cue's volume, pan or pitch to a new value over ``seconds``, as BASS's own
        slides do (the packs' fades)."""
        cue = self._cue_for(channel)
        if cue is None:
            return
        bass = self._bass
        duration = _milliseconds(seconds)
        if gain is not None and not cue.fading_away:
            cue.gain = gain
            level = _clamp(gain, 0.0, 1.0)
            bass.BASS_ChannelSlideAttribute(cue.stream, ATTRIB_VOL, level, duration)
        if pan is not None:
            cue.pan = pan
            balance = _clamp(pan, -1.0, 1.0)
            bass.BASS_ChannelSlideAttribute(cue.stream, ATTRIB_PAN, balance, duration)
        if frequency is not None and frequency >= 0:
            bass.BASS_ChannelSlideAttribute(cue.stream, ATTRIB_FREQ, frequency, duration)

    def pause(self, channel: str) -> None:
        cue = self._cue_for(channel)
        if cue is not None:
            self._bass.BASS_ChannelPause(cue.stream)

    def resume(self, channel: str) -> None:
        cue = self._cue_for(channel)
        if cue is not None:
            self._bass.BASS_ChannelPlay(cue.stream, False)

    def state(self, channel: str) -> int:
        """BASS's own answer: 0 stopped, 1 playing, 2 stalled, 3 paused."""
        cue = self._cue_for(channel)
        if cue is None:
            return ACTIVE_STOPPED
        return self._bass.BASS_ChannelIsActive(cue.stream)

    def position(self, channel: str) -> float:
        cue = self._cue_for(channel)
        if cue is None:
            return 0.0
        bass = self._bass
        played = bass.BASS_ChannelGetPosition(cue.stream, POS_BYTE)
        return bass.BASS_ChannelBytes2Seconds(cue.stream, played)

    def seek(self, channel: str, seconds: float) -> None:
        cue = self._cue_for(channel)
        if cue is not None:
            bass = self._bass
            bass.BASS_ChannelSetPosition(cue.stream, self._bytes(cue.stream, seconds), POS_BYTE)

    def set_looping(self, channel: str, loop: bool) -> None:
        cue = self._cue_for(channel)
        if cue is not None:
            self._bass.BASS_ChannelFlags(cue.stream, SAMPLE_LOOP if loop else 0, SAMPLE_LOOP)

    def add_effect(
        self, channel: str, kind: int, settings: dict[str, float] | None = None,
        priority: int = 0,
    ) -> int:
        """Put one of BASS's DirectX 8 effects on the cue playing on ``channel`` (``kind`` is
        BASS's number for it, as in bass_effects), with ``settings`` by BASS's names; settings
        BASS refuses leave it at its own. Answers BASS's handle for the effect, or 0 when
        nothing plays there or BASS can't have that effect here. Gargle, which BASS lacks off
        Windows, is made from BASS_FX's volume envelope there."""
        cue = self._cue_for(channel)
        if cue is None or kind not in bass_effects.SETTINGS:
            return 0
        bass = self._bass
        handle = bass.BASS_ChannelSetFX(cue.stream, kind, priority)
        if handle:
            cue.effects[handle] = _Effect(kind=kind)
        elif kind == bass_effects.DX8_GARGLE:
            handle = self._envelope_gargle(cue, priority)
        if not handle:
            self._trace_effect(kind, "MISSING", error=bass.BASS_ErrorGetCode())
            return 0
        if settings:
            self.set_effect(channel, handle, settings)
        return handle

    def set_effect(self, channel: str, effect: int, settings: dict[str, float]) -> bool:
        """Change the settings named, by BASS's names, of an effect on the cue playing on
        ``channel``. False when it isn't there, or BASS refuses a value, which leaves every
        setting as it was."""
        cue = self._cue_for(channel)
        found = cue.effects.get(effect) if cue is not None else None
        if found is None:
            return False
        bass = self._bass
        if found.envelope is not None:
            return self._set_envelope_gargle(cue, effect, found, settings)
        structure = bass_effects.STRUCTURES[found.kind]()
        if not bass.BASS_FXGetParameters(effect, ctypes.byref(structure)):
            return False
        bass_effects.write_settings(structure, found.kind, settings)
        return bool(bass.BASS_FXSetParameters(effect, ctypes.byref(structure)))

    def effect_settings(self, channel: str, effect: int) -> dict[str, float] | None:
        """An effect's settings by BASS's names, or None when it isn't there."""
        cue = self._cue_for(channel)
        found = cue.effects.get(effect) if cue is not None else None
        if found is None:
            return None
        if found.envelope is not None:
            return dict(found.settings)
        structure = bass_effects.STRUCTURES[found.kind]()
        if not self._bass.BASS_FXGetParameters(effect, ctypes.byref(structure)):
            return None
        return bass_effects.read_settings(structure, found.kind)

    def remove_effect(self, channel: str, effect: int) -> None:
        cue = self._cue_for(channel)
        if cue is not None and cue.effects.pop(effect, None) is not None:
            self._bass.BASS_ChannelRemoveFX(cue.stream, effect)

    def reset_effects(self, channel: str, effect: int = 0) -> None:
        """Clear what an effect has built up (an echo still sounding), or every effect's on the
        cue when ``effect`` is 0."""
        cue = self._cue_for(channel)
        if cue is None:
            return
        if effect == 0:
            self._bass.BASS_FXReset(cue.stream)
        elif effect in cue.effects:
            self._bass.BASS_FXReset(effect)

    def source_info(self, file: str) -> SourceInfo | None:
        """What ``file`` holds, read by opening it to decode and closing it again."""
        bass = self._bass
        path, flags = bass_library.path_argument(file)
        stream = bass.BASS_StreamCreateFile(False, path, 0, 0, flags | bass_library.STREAM_DECODE)
        if not stream:
            return None
        try:
            length = bass.BASS_ChannelGetLength(stream, POS_BYTE)
            seconds = bass.BASS_ChannelBytes2Seconds(stream, length)
            details = bass_library.ChannelInfo()
            if not bass.BASS_ChannelGetInfo(stream, ctypes.byref(details)):
                return None
            return SourceInfo(seconds=seconds, rate=details.freq, channels=details.chans)
        finally:
            bass.BASS_StreamFree(stream)

    def _envelope_gargle(self, cue: _Cue, priority: int) -> int:
        """Gargle the cue with BASS_FX's volume envelope, at DirectX's own settings; answers
        the envelope's handle, or 0 when BASS_FX isn't there either."""
        bass = self._bass
        handle = bass.BASS_ChannelSetFX(cue.stream, bass_effects.BFX_VOLUME_ENV, priority)
        if not handle:
            return 0
        found = _Effect(kind=bass_effects.DX8_GARGLE)
        if not self._set_envelope_gargle(cue, handle, found, bass_effects.GARGLE_DEFAULTS):
            bass.BASS_ChannelRemoveFX(cue.stream, handle)
            return 0
        cue.effects[handle] = found
        return handle

    def _set_envelope_gargle(
        self, cue: _Cue, handle: int, found: _Effect, settings: dict[str, float],
    ) -> bool:
        """Give a gargle made from the volume envelope new settings, refusing what DirectX
        refuses: a rate outside 1 to 1000 Hz, or a wave shape other than 0 or 1."""
        chosen = dict(found.settings or bass_effects.GARGLE_DEFAULTS)
        chosen.update({name: settings[name] for name in chosen if name in settings})
        rate = chosen["dwRateHz"]
        shape = chosen["dwWaveShape"]
        if not isinstance(rate, (int, float)) or not 1 <= rate <= 1000 or shape not in (0, 1):
            return False
        bass = self._bass
        length = bass.BASS_ChannelGetLength(cue.stream, POS_BYTE)
        seconds = bass.BASS_ChannelBytes2Seconds(cue.stream, length)
        envelope = bass_effects.gargle_envelope(rate, shape, seconds)
        if not bass.BASS_FXSetParameters(handle, ctypes.byref(envelope.settings)):
            return False
        found.envelope = envelope
        found.settings = {"dwRateHz": int(rate), "dwWaveShape": int(shape)}
        return True

    def _cue_for(self, channel: str) -> _Cue | None:
        """The cue playing (or paused) on ``channel``; None once BASS has freed it."""
        cue = self._cues.get(channel)
        if cue is None and (channel == MUSIC_CATEGORY or channel == self._music_channel):
            cue = self._music
        if cue is None or cue.freed:
            return None
        return cue

    def _open(
        self, file: str, gain: float, pan: float, *, loop: bool, repeats: int,
        timing: CueTiming | None,
        output: AudioOutput | None = None,
    ) -> _Cue | None:
        """A cue of ``file`` ready to play, or None when it can't be opened (reported)."""
        path, flags = bass_library.path_argument(file)
        looping = loop or repeats > 1
        if looping:
            flags |= SAMPLE_LOOP
        # Every cue frees itself once it stops: at its end, at the end of the pass where its
        # loop was turned off, or when a fade away stops it. Nothing plays a stopped cue again.
        flags |= STREAM_AUTOFREE
        if self._route_devices:
            try:
                with output_context(self._bass, output) as index:
                    stream = self._bass.BASS_StreamCreateFile(False, path, 0, 0, flags)
                    if output is not None and stream:
                        if self._bass.BASS_ChannelGetDevice(stream) != index:
                            # BASS can choose another initialized device if hotplug removed
                            # the selected one between validation and stream creation.
                            self._bass.BASS_StreamFree(stream)
                            raise AudioRouteUnavailable(f"Audio output changed: {output.name}")
            except AudioRouteUnavailable as error:
                if output is not None:
                    raise
                if str(error) not in self._warned:
                    self._warned.add(str(error))
                    if self._on_error is not None:
                        self._on_error(str(error))
                return None
        else:
            stream = self._bass.BASS_StreamCreateFile(False, path, 0, 0, flags)
        if not stream:
            self._trace(file, "OPEN", error=self._bass.BASS_ErrorGetCode())
            if output is not None:
                raise AudioRouteUnavailable(
                    f"Cannot open sound on {output.name} "
                    f"(BASS error {self._bass.BASS_ErrorGetCode()})"
                )
            self._warn(file)
            return None
        self._forget_finished()
        cue = _Cue(stream=stream, gain=gain, pan=pan)

        def freed(_channel: int) -> None:
            # BASS calls this as it frees the stream: our own BASS_StreamFree, or its own at the
            # end of an auto-free stream. No sync of it comes after.
            cue.freed = True
            for number in cue.actions:
                _actions.pop(number, None)

        self._sync(cue, SYNC_FREE | SYNC_MIXTIME, 0, freed)
        cue.end = self._bass.BASS_ChannelGetLength(stream, POS_BYTE)
        if repeats > 1:
            self._repeat(cue, repeats)
        if timing is not None:
            self._time(cue, timing, looping)
        return cue

    def _repeat(self, cue: _Cue, repeats: int) -> None:
        """Play ``stream`` ``repeats`` times: it loops, and the loop is switched off as it ends
        for the last time (a MUD's MSP L= or Client.Media loops)."""
        bass = self._bass
        endings = [0]

        def at_end(channel: int) -> None:
            endings[0] += 1
            if endings[0] >= repeats:
                bass.BASS_ChannelFlags(channel, 0, SAMPLE_LOOP)

        self._sync(cue, SYNC_END | SYNC_MIXTIME, 0, at_end)

    def _time(self, cue: _Cue, timing: CueTiming, looping: bool) -> None:
        """Start, finish and fade the cue each time round, the way Mudlet does: a start at or
        past the end is ignored, the fade out is measured back from the finish, and both fades
        are straight lines between silence and the cue's volume."""
        bass = self._bass
        stream = cue.stream
        length = cue.end
        start = self._bytes(stream, timing.start)
        if start >= length:
            start = 0
        finish = self._bytes(stream, timing.finish)
        if start < finish < length:
            bass.BASS_ChannelSetPosition(stream, finish, POS_END)
            cue.end = finish
        if start:
            bass.BASS_ChannelSetPosition(stream, start, POS_BYTE)
            if looping:
                bass.BASS_ChannelSetPosition(stream, start, POS_LOOP)
        cue.fade_in = max(0.0, timing.fade_in)
        if timing.fade_out > 0:
            fade_length = min(self._bytes(stream, timing.fade_out), cue.end - start)
            fade_seconds = bass.BASS_ChannelBytes2Seconds(stream, fade_length)

            def fade_out(channel: int) -> None:
                if not cue.fading_away:
                    bass.BASS_ChannelSlideAttribute(
                        channel, ATTRIB_VOL, 0.0, _milliseconds(fade_seconds)
                    )

            self._sync(cue, SYNC_POS | SYNC_MIXTIME, cue.end - fade_length, fade_out)
        if looping and (timing.fade_in > 0 or timing.fade_out > 0):

            def round_again(channel: int) -> None:
                # Each time round starts as the first did: from silence when it fades in,
                # else back at its volume after fading out.
                if not cue.fading_away:
                    self._level(channel, cue)

            self._sync(cue, SYNC_END | SYNC_MIXTIME, 0, round_again)

    def _sync(self, cue: _Cue, kind: int, position: int, action: Callable[[int], None]) -> None:
        """Have BASS run ``action`` with the cue's stream when ``kind`` happens to it, through
        the one lasting callback."""
        number = next(_action_numbers)
        _actions[number] = action
        cue.actions.append(number)
        self._bass.BASS_ChannelSetSync(cue.stream, kind, position, _dispatch, number)

    def _forget_finished(self) -> None:
        """Let go of this backend's record of cues BASS has freed."""
        for channel, cue in list(self._cues.items()):
            if cue.freed:
                del self._cues[channel]
        if self._music is not None and self._music.freed:
            self._music = None
            self._music_channel = None

    def _bytes(self, stream: int, seconds: float) -> int:
        if seconds <= 0:
            return 0
        return self._bass.BASS_ChannelSeconds2Bytes(stream, seconds)

    def _level(self, stream: int, cue: _Cue) -> None:
        """Set the cue's volume for the start of a time round: silence sliding up to its volume
        when it fades in, else its volume."""
        bass = self._bass
        gain = _clamp(cue.gain, 0.0, 1.0)
        if cue.fade_in > 0:
            bass.BASS_ChannelSetAttribute(stream, ATTRIB_VOL, 0.0)
            bass.BASS_ChannelSlideAttribute(stream, ATTRIB_VOL, gain, _milliseconds(cue.fade_in))
        else:
            bass.BASS_ChannelSetAttribute(stream, ATTRIB_VOL, gain)

    def _start(self, file: str, cue: _Cue, **fields: object) -> bool:
        bass = self._bass
        bass.BASS_ChannelSetAttribute(cue.stream, ATTRIB_PAN, _clamp(cue.pan, -1.0, 1.0))
        self._level(cue.stream, cue)
        if bass.BASS_ChannelPlay(cue.stream, False):
            self._trace(file, "OK", **fields)
            return True
        else:
            self._trace(file, "EXC", error=bass.BASS_ErrorGetCode(), **fields)
            return False

    def _free(self, cue: _Cue | None) -> None:
        """Stop and free a cue's stream, unless BASS has already (its handle is dead then)."""
        if cue is not None and not cue.freed:
            self._bass.BASS_StreamFree(cue.stream)  # its free callback marks the cue freed

    def _trace_effect(self, kind: int, result: str, **fields: object) -> None:
        if self._diag is not None:
            self._diag.event("backend.effect", kind=kind, result=result, **fields)

    def _trace(self, file: str, result: str, **fields: object) -> None:
        if self._diag is not None:
            self._diag.event("backend.play", file=file, result=result, **fields)

    def _warn(self, file: str) -> None:
        """Report a cue that couldn't play, once per file (a flood would fire every line)."""
        if self._on_error is None or file in self._warned:
            return
        self._warned.add(file)
        self._on_error(
            f"sound not played: {file} -- file missing or unsupported format; "
            "set the world's Sounds folder if your sounds are elsewhere"
        )


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def make_bass_backend(
    on_error: Callable[[str], None] | None = None,
    diag: DiagnosticLog | None = None,
) -> BassSoundBackend | None:
    """BASS as a backend, or None when this build has no BASS or no audio device opens.

    No message for the player here: the caller falls back to pygame, which speaks up if it
    can't play either. ``diag`` records why, so a build that fell back can be told apart.
    """
    try:
        bass = bass_library.load()
        bass_library.start(bass)
    except bass_library.BassUnavailable as error:
        if diag is not None:
            diag.event("backend.select", result="none", reason=f"no BASS: {error}")
        return None
    if diag is not None:
        diag.event("backend.select", result="bass", reason=str(bass.folder))
    return BassSoundBackend(bass.core, on_error=on_error, diag=diag, route_devices=True)


def make_sound_backend(
    on_error: Callable[[str], None] | None = None,
    diag: DiagnosticLog | None = None,
):
    """The native backend for this session: BASS when it can, pygame when it can't, None
    when neither can play (the caller then posts cues to the renderer)."""
    backend = make_bass_backend(on_error=on_error, diag=diag)
    if backend is not None:
        return backend
    from genericmud.sound.pygame_backend import make_pygame_backend

    return make_pygame_backend(on_error=on_error, diag=diag)
