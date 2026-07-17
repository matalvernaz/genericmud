"""The MUDBall-parity speech ergonomics: follow mode, interrupt mode, autoretype,
channel browsing keys, spell-line, and the autocomplete word feed — all at the
EngineApp layer, driven the way the UI drives them (keys and input messages)."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Line
from genericmud.protocol.telnet import OPT_GMCP, DataReceived, Subnegotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app() -> tuple[EngineApp, RecordingBackend, list[str]]:
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(voice, send=sent.append, post=lambda _m: None, keymap=load_keymap("vipmud"))
    return app, backend, sent


def _key(app: EngineApp, combo: str) -> None:
    app.on_ws_message({"type": "key", "key": combo})


# --- follow mode ---


def test_follow_mode_interrupts_on_a_direction_but_not_on_ordinary_lines():
    app, backend, sent = _app()
    _key(app, "ctrl+f")  # announced toggle
    assert any("follow mode on" in text for text in backend.spoken)
    stops = backend.stops

    app.on_ws_message({"type": "input", "text": "n"})
    assert sent == ["n"]
    assert backend.stops == stops + 1  # movement barged in

    app.on_telnet_event(DataReceived(b"A dragon roars\r\n"))
    assert backend.stops == stops + 1  # ordinary lines still queue


def test_follow_mode_off_never_interrupts_movement():
    app, backend, _sent = _app()
    stops = backend.stops
    app.on_ws_message({"type": "input", "text": "n"})
    assert backend.stops == stops


def test_follow_mode_interrupts_on_a_gmcp_room_change():
    app, backend, _sent = _app()
    app.follow_mode = True
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'room.info {"num": 1}'))
    stops = backend.stops
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'room.info {"num": 2}'))
    assert backend.stops == stops + 1
    stops = backend.stops
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'room.info {"num": 2}'))  # same room
    assert backend.stops == stops


def test_follow_mode_interrupts_once_for_a_speedwalk_burst():
    app, backend, sent = _app()
    app.follow_mode = True
    stops = backend.stops
    app.on_ws_message({"type": "input", "text": ".3n"})
    assert sent == ["n", "n", "n"]
    assert backend.stops == stops + 1


# --- interrupt mode ---


def test_interrupt_mode_makes_every_line_barge_in():
    app, backend, _sent = _app()
    _key(app, "ctrl+i")
    assert any("interrupt mode on" in text for text in backend.spoken)
    stops = backend.stops
    app.on_telnet_event(DataReceived(b"one\r\ntwo\r\n"))
    assert backend.stops == stops + 2
    _key(app, "ctrl+i")  # off again
    stops = backend.stops
    app.on_telnet_event(DataReceived(b"three\r\n"))
    assert backend.stops == stops


# --- autoretype ---


def test_autoretype_resends_the_last_input_on_empty_enter():
    app, _backend, sent = _app()
    _key(app, "ctrl+enter")
    app.on_ws_message({"type": "input", "text": "kill rat"})
    app.on_ws_message({"type": "input", "text": ""})
    assert sent == ["kill rat", "kill rat"]


def test_autoretype_off_keeps_the_blank_line_pager_behaviour():
    app, _backend, sent = _app()
    app.on_ws_message({"type": "input", "text": "kill rat"})
    app.on_ws_message({"type": "input", "text": ""})
    assert sent == ["kill rat", ""]  # pagers advance on a genuine blank line


def test_autoretype_repeats_a_whole_stacked_input():
    app, _backend, sent = _app()
    app.autoretype = True
    app.on_ws_message({"type": "input", "text": "n;look"})
    app.on_ws_message({"type": "input", "text": ""})
    assert sent == ["n", "look", "n", "look"]


# --- toggles persist through pref_sink ---


def test_keymap_toggles_report_to_the_pref_sink():
    app, _backend, _sent = _app()
    recorded: list[tuple[str, bool]] = []
    app.pref_sink = lambda attr, value: recorded.append((attr, value))
    _key(app, "ctrl+f")
    _key(app, "ctrl+i")
    _key(app, "ctrl+enter")
    _key(app, "ctrl+f")
    assert recorded == [
        ("follow_mode", True), ("interrupt_mode", True),
        ("autoretype", True), ("follow_mode", False),
    ]


# --- channel browsing keys ---


def test_channel_keys_cycle_scroll_and_recall():
    app, backend, _sent = _app()
    app.buffer.append(Line("hi all", channel="chat"))
    app.buffer.append(Line("psst", channel="tell"))
    app.buffer.append(Line("anyone?", channel="chat"))

    _key(app, "ctrl+alt+right")
    assert backend.spoken[-1] == "chat: anyone?"
    _key(app, "ctrl+alt+up")
    assert backend.spoken[-1] == "hi all"
    _key(app, "ctrl+alt+down")
    assert backend.spoken[-1] == "anyone?"
    _key(app, "ctrl+alt+right")
    _key(app, "ctrl+alt+1")
    assert backend.spoken[-1] == "psst"


def test_channel_word_navigation_keys():
    app, backend, _sent = _app()
    app.buffer.append(Line("Bob tells you hello", channel="tell"))
    _key(app, "ctrl+alt+right")
    _key(app, "ctrl+alt+shift+right")
    assert backend.spoken[-1] == "tells"


# --- spell line ---


def test_spell_line_key_spells_the_newest_line():
    app, backend, _sent = _app()
    app.on_telnet_event(DataReceived(b"ok go\r\n"))
    _key(app, "alt+shift+enter")
    assert backend.spoken[-1] == "o, k, space, g, o"


# --- autocomplete feed ---


def test_displayed_lines_feed_the_word_index_but_gagged_removed_lines_do_not():
    app, _backend, _sent = _app()
    app.on_telnet_event(DataReceived(b"a dragon arrives\r\n"))
    assert app.word_index.complete("dra") == ["dragon"]

    app.engine.add_trigger("secret", gag=True)
    app.on_telnet_event(DataReceived(b"a secret whisper\r\n"))
    assert app.word_index.complete("whis") == []  # removed line: never displayed
