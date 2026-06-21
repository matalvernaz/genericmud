"""Interactive /alias, /trigger, /to commands — making rules without scripting."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.protocol.telnet import DataReceived
from genericmud.session.hub import SessionHub
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app(**kwargs):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(voice, send=sent.append, post=[].append, keymap={}, **kwargs)
    return app, sent, backend


def _input(app, text):
    app.on_ws_message({"type": "input", "text": text})


def test_alias_create_and_fire():
    app, sent, _ = _app()
    _input(app, "/alias k = kill")
    _input(app, "k")
    assert sent == ["kill"]


def test_alias_target_supports_command_stacking():
    app, sent, _ = _app()
    _input(app, "/alias buff = cast armor;cast shield")
    sent.clear()
    _input(app, "buff")
    assert sent == ["cast armor", "cast shield"]


def test_alias_is_exact_not_a_prefix():
    app, sent, _ = _app()
    _input(app, "/alias k = kill")
    sent.clear()
    _input(app, "k goblin")  # not exactly "k"
    assert sent == ["k goblin"]  # alias didn't fire; sent literally


def test_trigger_fires_case_insensitively():
    app, sent, _ = _app()
    _input(app, "/trigger you are hungry = eat bread")
    sent.clear()
    app.on_telnet_event(DataReceived(b"You are hungry.\r\n"))
    assert sent == ["eat bread"]


def test_unalias_removes_the_alias():
    app, sent, _ = _app()
    _input(app, "/alias k = kill")
    _input(app, "/unalias k")
    sent.clear()
    _input(app, "k")
    assert sent == ["k"]  # no longer aliased


def test_list_aliases_speaks_them():
    app, _sent, backend = _app()
    _input(app, "/alias k = kill")
    _input(app, "/aliases")
    assert any("k = kill" in spoken for spoken in backend.spoken)


def test_unknown_slash_command_passes_through_to_mud():
    app, sent, _ = _app()
    _input(app, "/who")  # not a client command
    assert sent == ["/who"]


def test_self_referential_alias_is_depth_bounded():
    app, sent, _ = _app()
    _input(app, "/alias loop = loop")
    sent.clear()
    _input(app, "loop")  # must terminate, not hang or overflow
    assert sent == []  # the alias consumes the input each time; nothing reaches the MUD


def test_to_command_sends_into_another_session():
    hub = SessionHub()
    mage, _mage_sent, _ = _app(name="Mage", hub=hub)
    healer, healer_sent, _ = _app(name="Healer", hub=hub)
    mage.on_connect("Mage")
    healer.on_connect("Healer")
    _input(mage, "/to Healer cast heal")
    assert healer_sent == ["cast heal"]
