"""Directional sound from the MUD (Mudlet 5.1's Client.Media): pan, direction, the way the
player faces, Update, Pause and play priority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from genericmud.app import EngineApp
from genericmud.protocol.telnet import OPT_GMCP, Subnegotiation
from genericmud.sound.bus import PAUSED, PLAYING, STOPPED, SoundBackend
from genericmud.sound.direction import bearing, facing_bearing, stereo_pan
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend
from tests.test_media import _library, _Net

URL = "https://x.example/"

# --- where a direction puts a sound ------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "facing", "pan"),
    [
        ("east", 0.0, 1.0),  # facing north, east is the right ear
        ("w", 0.0, -1.0),
        ("northeast", 0.0, 0.71),
        ("north", 90.0, -1.0),  # facing east, north is on the left
        ("south", 0.0, 0.0),  # behind: stereo can't tell it from ahead
        ("left", 90.0, -1.0),  # relative words ignore the facing
        ("ahead", 270.0, 0.0),
        (90, 0.0, 1.0),  # degrees, as a number
        ("135", 0.0, 0.71),  # and as text
    ],
)
def test_a_direction_becomes_a_pan(direction, facing, pan):
    assert stereo_pan(None, direction, facing) == pan


def test_up_down_and_nonsense_stay_in_the_middle():
    for direction in ("up", "d", "", None, "sideways", "nan", True):
        assert stereo_pan(None, direction, 0.0) is None


def test_a_pan_given_outright_wins_and_is_kept_in_range():
    assert stereo_pan(50, "west", 0.0) == 0.5
    assert stereo_pan("-150", None, 0.0) == -1.0


def test_a_facing_must_be_a_way_to_turn():
    assert facing_bearing("e") == 90.0
    assert facing_bearing(180) == 180.0
    assert facing_bearing("left") is None  # relative, not a compass direction
    assert facing_bearing("up") is None
    assert bearing("nw") == (315.0, False)


# --- the engine, driven by Client.Media messages ------------------------------------


class _Backend(SoundBackend):
    """Records what the bus asks of it, and keeps track of what's playing or paused."""

    def __init__(self) -> None:
        self.played: list[tuple[str, str, float | None]] = []  # (file, channel, pan)
        self.adjusted: list[tuple[str, float | None, float | None]] = []
        self.slid: list[tuple[str, float | None, float | None, float]] = []
        self.stopped: list[str] = []
        self.playing: set[str] = set()
        self.paused: set[str] = set()

    def play(self, file, channel, gain, pan, loop, times=1, timing=None):
        self.played.append((file, channel, pan))
        self.playing.add(channel)

    def music(self, file, channel, gain, times=-1, timing=None):
        self.played.append((file, channel, None))
        self.playing.add(channel)

    def stop(self, channel):
        self.stopped.append(channel)
        self.playing.discard(channel)

    def is_playing(self, channel):
        return channel in self.playing

    def adjust(self, channel, gain=None, pan=None, frequency=None):
        self.adjusted.append((channel, None if gain is None else round(gain, 2), pan))

    def slide(self, channel, gain=None, pan=None, frequency=None, seconds=0.0):
        self.slid.append((channel, None if gain is None else round(gain, 2), pan, seconds))

    def pause(self, channel):
        self.playing.discard(channel)
        self.paused.add(channel)

    def resume(self, channel):
        self.paused.discard(channel)
        self.playing.add(channel)

    def state(self, channel):
        if channel in self.paused:
            return PAUSED
        return PLAYING if channel in self.playing else STOPPED


def _app(tmp_path) -> tuple[EngineApp, _Backend]:
    backend = _Backend()
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0), post=lambda _m: None,
        sound_backend=backend, media=_library(tmp_path, _Net()),
    )
    return app, backend


def _gmcp(app: EngineApp, package: str, body: dict) -> None:
    app.on_telnet_event(Subnegotiation(OPT_GMCP, f"{package} {json.dumps(body)}".encode()))


def _play(app: EngineApp, **body) -> None:
    _gmcp(app, "Client.Media.Play", {"url": URL, **body})


def test_a_sound_from_the_east_plays_on_the_right(tmp_path):
    app, backend = _app(tmp_path)
    _play(app, name="door.wav", direction="east")
    _play(app, name="bell.wav", pan=-50)
    _play(app, name="hum.wav", direction="up")
    assert [pan for _file, _channel, pan in backend.played] == [1.0, -0.5, 0.0]


def test_turning_swings_placed_sounds_round_but_not_a_given_pan(tmp_path):
    app, backend = _app(tmp_path)
    _play(app, name="river.wav", key="river", direction="north", loops=-1)
    _play(app, name="radio.wav", key="radio", pan=40, loops=-1)
    _gmcp(app, "Client.Media.Listener", {"facing": "east"})  # now north is on the left
    assert backend.slid == [("media:river", None, -1.0, 0.25)]
    _gmcp(app, "Client.Media.Listener", {"facing": "up"})  # not a way to face: ignored
    assert len(backend.slid) == 1


def test_a_new_connection_faces_north_again(tmp_path):
    app, backend = _app(tmp_path)
    _gmcp(app, "Client.Media.Listener", {"facing": "south"})
    app.on_connect("Test")
    _play(app, name="door.wav", direction="east")
    assert backend.played[-1][2] == 1.0


def test_update_glides_a_named_sound_and_refuses_to_name_none(tmp_path):
    app, backend = _app(tmp_path)
    _play(app, name="wind.wav", key="wind", direction="west", loops=-1)
    _gmcp(app, "Client.Media.Update", {"key": "wind", "volume": 30, "duration": 2000})
    _gmcp(app, "Client.Media.Update", {"key": "wind", "direction": "east"})
    _gmcp(app, "Client.Media.Update", {"volume": 100})  # names no sound: refused
    assert backend.slid == [("media:wind", 0.3, None, 2.0)]
    assert backend.adjusted == [("media:wind", None, 1.0)]


def test_update_by_key_leaves_sounds_without_a_key_alone(tmp_path):
    app, backend = _app(tmp_path)
    _play(app, name="wind.wav", loops=-1)  # no key of its own
    _gmcp(app, "Client.Media.Update", {"name": "wind.wav", "key": "gust", "volume": 10})
    assert backend.adjusted == []


def test_a_higher_priority_sound_keeps_lower_ones_out_and_stops_them(tmp_path):
    app, backend = _app(tmp_path)
    _play(app, name="alarm.wav", key="alarm", priority=50, loops=-1)
    _play(app, name="chirp.wav", priority=50)  # equal: refused
    _play(app, name="bird.wav", priority=10)  # lower: refused
    _play(app, name="step.wav")  # no priority given: always plays
    assert [Path(file).name for file, _channel, _pan in backend.played] == ["alarm.wav", "step.wav"]
    _play(app, name="siren.wav", priority=80)  # higher: plays, and the alarm stops
    assert Path(backend.played[-1][0]).name == "siren.wav"
    assert "media:alarm" in backend.stopped


def test_pause_then_play_carries_on_where_it_stopped(tmp_path):
    app, backend = _app(tmp_path)
    _play(app, name="area.ogg", type="music", url=URL)
    _gmcp(app, "Client.Media.Pause", {})  # no fields: everything the MUD is playing
    assert backend.state("music") == PAUSED
    _play(app, name="area.ogg", type="music")
    assert backend.state("music") == PLAYING
    assert len(backend.played) == 1  # resumed, not started again


def test_the_old_bare_name_still_sets_the_default_address(tmp_path):
    app, backend = _app(tmp_path)
    _gmcp(app, "Client.Media", {"url": URL})
    _gmcp(app, "Client.Media.Play", {"name": "door.wav"})
    assert backend.played and backend.played[0][0].endswith("door.wav")
