"""Integration tests for the EngineApp glue (no socket/webview/NVDA needed)."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import GA, OPT_GMCP, Command, DataReceived, Subnegotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app() -> tuple[EngineApp, RecordingBackend, list[str], list[dict]]:
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"))
    return app, backend, sent, posted


def test_incoming_line_spoken_buffered_and_posted():
    app, backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"You see a dragon\r\n"))
    assert "You see a dragon" in backend.spoken
    assert app.buffer.lines()[-1].plain_text == "You see a dragon"
    assert any(m["type"] == "line" and m["text"] == "You see a dragon" for m in posted)


def test_gagged_line_not_spoken():
    app, backend, _sent, _posted = _app()
    app.engine.add_trigger("spammy", None, gag=True)
    app.on_telnet_event(DataReceived(b"spammy tick\n"))
    assert "spammy tick" not in backend.spoken


def test_prompt_flushed_on_go_ahead():
    app, backend, _sent, _posted = _app()
    app.on_telnet_event(DataReceived(b"HP:100 MP:50>"))  # no newline yet
    assert not any("HP:100" in s for s in backend.spoken)
    app.on_telnet_event(Command(GA))
    assert any("HP:100 MP:50>" in s for s in backend.spoken)


def test_recall_key_speaks_last_line():
    app, backend, _sent, _posted = _app()
    app.on_telnet_event(DataReceived(b"first\nsecond\n"))
    backend.spoken.clear()
    app.on_ws_message({"type": "key", "key": "ctrl+1"})
    assert backend.spoken[-1] == "second"


def test_review_prev_line_key_enters_and_moves():
    app, backend, _sent, _posted = _app()
    app.on_telnet_event(DataReceived(b"l1\nl2\nl3\n"))
    backend.spoken.clear()
    app.on_ws_message({"type": "key", "key": "alt+up"})
    assert backend.spoken[-1] == "l2"


def test_input_is_sent_to_mud():
    app, _backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": "north"})
    assert sent == ["north"]


def test_gmcp_subnegotiation_posts_status():
    app, _backend, _sent, posted = _app()
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'Char.Vitals {"hp":42}'))
    assert any(m["type"] == "status" for m in posted)


def test_msp_line_emits_sound_and_strips_tag():
    app, backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"A thud !!SOUND(hit.wav V=80)\r\n"))
    sounds = [m for m in posted if m["type"] == "sound"]
    assert sounds and sounds[0]["file"] == "hit.wav"
    assert abs(sounds[0]["gain"] - 0.8) < 1e-9
    # the !!SOUND tag is stripped from what gets spoken/displayed
    assert any("A thud" in s and "!!SOUND" not in s for s in backend.spoken)


def test_ansi_stripped_from_output():
    app, backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"\x1b[1;32mGreen room\x1b[0m\r\n"))
    assert any(m["type"] == "line" and m["text"] == "Green room" for m in posted)
    assert "Green room" in backend.spoken
