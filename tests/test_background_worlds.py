"""A world in a background tab must not cut off the world in front.

Every open world speaks through the one screen reader, so stopping speech from a world that
may not speak (a background tab) silenced whichever world the player was listening to: a
background world in follow mode did it on every room change, and a background soundpack did
it whenever it stopped speech on a trigger of its own.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _background_world():
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    app = EngineApp(voice, send=[].append, post=[].append, keymap=load_keymap("vipmud"))
    voice.set_muted(True)  # what the window does for every tab that may not speak
    return app, backend


def test_a_background_world_in_follow_mode_does_not_cut_off_the_world_in_front():
    app, backend = _background_world()
    app.follow_mode = True
    app._on_player_moved()
    assert backend.stops == 0


def test_the_world_in_front_still_cuts_its_own_speech_on_a_move():
    app, backend = _background_world()
    app.voice.set_muted(False)
    app.follow_mode = True
    app._on_player_moved()
    assert backend.stops == 1


def test_a_soundpack_in_a_background_world_cannot_stop_speech():
    app, backend = _background_world()
    app.sink.stop_speech()
    assert backend.stops == 0
    app.voice.set_muted(False)
    app.sink.stop_speech()
    assert backend.stops == 1
