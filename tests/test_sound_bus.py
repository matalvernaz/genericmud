"""SoundBus: per-category gain/mute, master scaling, flush, and app wiring."""

from __future__ import annotations

from dataclasses import dataclass, field

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.sound.bus import SoundBackend, SoundBus
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


@dataclass
class _RecordingSound(SoundBackend):
    played: list = field(default_factory=list)
    musics: list = field(default_factory=list)
    stopped: list = field(default_factory=list)

    def play(self, file, channel, gain, pan, loop):
        self.played.append({"file": file, "channel": channel, "gain": gain, "loop": loop})

    def music(self, file, channel, gain):
        self.musics.append({"file": file, "channel": channel, "gain": gain})

    def stop(self, channel):
        self.stopped.append(channel)


def test_default_gain_and_category_pass_through():
    backend = _RecordingSound()
    SoundBus(backend).play("hit.wav")
    assert backend.played[0]["gain"] == 1.0
    assert backend.played[0]["channel"] == "sound"


def test_per_category_volume_and_master_compose():
    backend = _RecordingSound()
    bus = SoundBus(backend, master=0.5)
    bus.set_volume("ambient", 0.4)
    bus.play("wind.ogg", "ambient", gain=0.5)
    assert backend.played[0]["gain"] == 0.5 * 0.4 * 0.5  # master * category * per-cue


def test_mute_zeros_future_cues_only():
    backend = _RecordingSound()
    bus = SoundBus(backend)
    bus.set_muted("music", True)
    bus.music("theme.mp3", "music")
    bus.set_muted("music", False)
    bus.music("theme.mp3", "music")
    assert [m["gain"] for m in backend.musics] == [0.0, 1.0]


def test_flush_stops_everything_playing_then_is_idempotent():
    backend = _RecordingSound()
    bus = SoundBus(backend)
    bus.play("a.wav", "sound")
    bus.music("b.mp3", "music")
    bus.play("c.wav", "ambient")
    bus.flush()
    assert set(backend.stopped) == {"sound", "music", "ambient"}
    backend.stopped.clear()
    bus.flush()
    assert backend.stopped == []  # nothing tracked as playing after the first flush


def test_stop_drops_a_category_from_the_flush_set():
    backend = _RecordingSound()
    bus = SoundBus(backend)
    bus.play("a.wav", "sound")
    bus.stop("sound")
    backend.stopped.clear()
    bus.flush()
    assert backend.stopped == []


def _app() -> tuple[EngineApp, list]:
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    posted: list[dict] = []
    app = EngineApp(voice, post=posted.append, keymap=load_keymap("vipmud"))
    return app, posted


def test_sound_flush_key_posts_stops_for_playing_categories():
    app, posted = _app()
    app.sound.play("ambient.ogg", "ambient")
    posted.clear()
    app._handle_key("shift+f11")  # vipmud keymap: sound:flush
    assert any(m["type"] == protocol.STOP_SOUND and m["channel"] == "ambient" for m in posted)


def test_app_play_routes_through_bus_with_gain():
    app, posted = _app()
    app.sound.set_volume("sound", 0.25)
    app.sink.play("clang.wav", "sound")  # the path a pack's mud.play() takes in-app
    sound_msgs = [m for m in posted if m["type"] == protocol.SOUND]
    assert sound_msgs[-1]["gain"] == 0.25
