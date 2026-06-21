"""Speedwalk expansion, breadcrumb retrace, where-am-I, and app wiring."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.navigation import Navigator, expand_speedwalk, invert
from genericmud.protocol.telnet import OPT_GMCP, Subnegotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def test_expand_speedwalk_counts_and_compounds():
    assert expand_speedwalk("3n2e") == ["n", "n", "n", "e", "e"]
    assert expand_speedwalk("ne") == ["ne"]  # two-char dir, not n + e
    assert expand_speedwalk("2sw") == ["sw", "sw"]
    assert expand_speedwalk("3N") == ["n", "n", "n"]  # case-insensitive
    assert expand_speedwalk("n") == ["n"]


def test_expand_speedwalk_rejects_non_speedwalk():
    assert expand_speedwalk("") == []
    assert expand_speedwalk("3nx") == []  # trailing junk
    assert expand_speedwalk("say hello") == []  # 's' then non-dir text


def test_invert():
    assert invert("n") == "s"
    assert invert("ne") == "sw"
    assert invert("u") == "d"
    assert invert("kick") is None


def test_navigator_records_only_directions():
    nav = Navigator()
    assert nav.record("n") is True
    assert nav.record("flee") is False
    assert nav.trail == ["n"]


def test_navigator_retrace_is_reversed_and_inverted():
    nav = Navigator()
    for direction in ("n", "n", "e"):
        nav.record(direction)
    assert nav.retrace() == ["w", "s", "s"]
    nav.clear()
    assert nav.trail == []


def test_navigator_where_from_room_and_trail():
    nav = Navigator()
    nav.update_room({"name": "Town Square", "area": "Midgaard", "exits": {"n": 1, "e": 2}})
    nav.record("n")
    where = nav.where()
    assert "Town Square" in where
    assert "Midgaard" in where
    assert "n" in where and "e" in where
    assert "1 steps from your breadcrumb" in where


def test_navigator_where_empty():
    assert Navigator().where() == "no location info"


# --- app wiring ---


def _app():
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"))
    return app, backend, sent, posted


def test_speedwalk_input_expands_sends_and_records():
    app, _backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": ".3n2e"})
    assert sent == ["n", "n", "n", "e", "e"]
    assert app.nav.trail == ["n", "n", "n", "e", "e"]


def test_manual_direction_is_recorded_other_input_is_not():
    app, _backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": "n"})
    app.on_ws_message({"type": "input", "text": "say hi"})
    assert sent == ["n", "say hi"]
    assert app.nav.trail == ["n"]  # only the bare direction joined the trail


def test_retrace_key_sends_inverted_path_and_clears():
    app, backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": ".ne"})  # walk northeast
    sent.clear()
    app.on_ws_message({"type": "key", "key": "alt+r"})  # nav:retrace
    assert sent == ["sw"]
    assert app.nav.trail == []
    assert any("retracing" in spoken for spoken in backend.spoken)


def test_where_key_reports_gmcp_room():
    app, backend, _sent, _posted = _app()
    payload = b'room.info {"name": "Town Square", "area": "Midgaard", "exits": {"n": 1}}'
    app.on_telnet_event(Subnegotiation(OPT_GMCP, payload))
    app.on_ws_message({"type": "key", "key": "alt+w"})  # nav:where
    assert any("Town Square" in spoken for spoken in backend.spoken)
