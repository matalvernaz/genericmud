"""Native pygame audio backend: pan/gain math, channel mapping, real-mixer smoke."""

from __future__ import annotations

import struct
import wave

import pytest

from genericmud.sound.bus import SoundBus
from genericmud.sound.pygame_backend import (
    PygameSoundBackend,
    make_pygame_backend,
    stereo_volume,
)


class _FakeChannel:
    def __init__(self, index: int) -> None:
        self.index = index
        self.played: list[tuple] = []
        self.volume: tuple | None = None
        self.stopped = 0

    def play(self, sound, loops):
        self.played.append((sound, loops))

    def set_volume(self, *volume):
        self.volume = volume

    def stop(self):
        self.stopped += 1


class _FakeMusic:
    def __init__(self) -> None:
        self.loaded = None
        self.volume = None
        self.played: list[int] = []
        self.stopped = 0

    def load(self, file):
        self.loaded = file

    def set_volume(self, volume):
        self.volume = volume

    def play(self, loops):
        self.played.append(loops)

    def stop(self):
        self.stopped += 1


class _FakeMixer:
    def __init__(self, num: int = 8) -> None:
        self._num = num
        self.music = _FakeMusic()
        self._channels: dict[int, _FakeChannel] = {}

    def get_num_channels(self):
        return self._num

    def Channel(self, index):  # noqa: N802 - mirrors pygame.mixer.Channel
        return self._channels.setdefault(index, _FakeChannel(index))

    def Sound(self, path):  # noqa: N802 - mirrors pygame.mixer.Sound
        return ("sound", path)


def test_stereo_volume_center_left_right():
    assert stereo_volume(1.0, 0.0) == (1.0, 1.0)
    assert stereo_volume(1.0, -1.0) == (1.0, 0.0)  # full left
    assert stereo_volume(1.0, 1.0) == (0.0, 1.0)  # full right
    assert stereo_volume(0.5, 0.0) == (0.5, 0.5)


def test_stereo_volume_clamps_gain_and_pan():
    assert stereo_volume(2.0, 0.0) == (1.0, 1.0)  # gain clamps to 1
    assert stereo_volume(1.0, 5.0) == (0.0, 1.0)  # pan clamps to +1


def test_play_applies_loop_flag_and_stereo_volume():
    backend = PygameSoundBackend(_FakeMixer())
    backend.play("/abs/hit.wav", "sound", 0.5, -1.0, loop=True)
    channel = backend._channels["sound"]
    assert channel.played == [(("sound", "/abs/hit.wav"), -1)]  # loop -> loops=-1
    assert channel.volume == (0.5, 0.0)  # gain 0.5, panned hard left


def test_distinct_categories_get_distinct_channels():
    backend = PygameSoundBackend(_FakeMixer())
    backend.play("a.wav", "sound", 1.0, 0.0, False)
    backend.play("b.wav", "ambient", 1.0, 0.0, False)
    assert backend._channels["sound"].index != backend._channels["ambient"].index


def test_decoded_sounds_are_cached():
    backend = PygameSoundBackend(_FakeMixer())
    backend.play("same.wav", "sound", 1.0, 0.0, False)
    backend.play("same.wav", "sound", 1.0, 0.0, False)
    assert len(backend._sounds) == 1


def test_music_loads_sets_volume_and_loops():
    mixer = _FakeMixer()
    PygameSoundBackend(mixer).music("/abs/theme.ogg", "music", 0.4)
    assert mixer.music.loaded == "/abs/theme.ogg"
    assert mixer.music.volume == 0.4
    assert mixer.music.played == [-1]


def test_stop_routes_music_vs_sound_channel():
    mixer = _FakeMixer()
    backend = PygameSoundBackend(mixer)
    backend.play("a.wav", "sound", 1.0, 0.0, False)
    backend.stop("music")  # music stops even with no sound channel for it
    assert mixer.music.stopped == 1
    backend.stop("sound")
    assert backend._channels["sound"].stopped == 1


def test_real_pygame_backend_smoke(tmp_path, monkeypatch):
    pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")  # no real device on CI/dev hosts
    wav = tmp_path / "s.wav"
    with wave.open(str(wav), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(b"".join(struct.pack("<h", 0) for _ in range(220)))

    backend = make_pygame_backend()
    assert backend is not None
    bus = SoundBus(backend)  # exercise the full bus -> real-mixer path
    bus.play(str(wav), "sound", 0.5, 0.2, loop=False)
    bus.music(str(wav), "music")
    bus.stop("sound")
    bus.flush()  # must not raise
