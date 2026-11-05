"""Alt+PageDown / Alt+PageUp: jump from MXP link to MXP link through the output (from Mudlet).

Driven the way the window drives it: key messages into the engine, links arriving as MXP.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import OPT_MXP, WILL, DataReceived, Negotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

SECURE = "\x1b[1z"


def _app() -> tuple[EngineApp, RecordingBackend, list[str]]:
    backend = RecordingBackend()
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0), send=sent.append, post=lambda _m: None,
        keymap=load_keymap("vipmud"),
    )
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    for line in (
        SECURE + 'A fountain gurgles: <send "drink fountain" hint="Drink">fountain</send>',
        "Nothing here.",
        SECURE + 'Exits: <send "north">north</send> and <send "south">south</send>',
    ):
        app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))
    return app, backend, sent


def _key(app: EngineApp, combo: str) -> None:
    app.on_ws_message({"type": "key", "key": combo})


def test_the_keys_are_alt_pagedown_and_alt_pageup():
    keymap = load_keymap("vipmud")
    assert keymap["alt+pagedown"] == "review:next_link"
    assert keymap["alt+pageup"] == "review:prev_link"


def test_back_through_the_links_newest_first_then_no_more():
    app, backend, _sent = _app()
    _key(app, "alt+pageup")  # outside review: the newest line's last link comes first
    assert backend.spoken[-1] == "south: Exits: north and south"
    _key(app, "alt+pageup")
    assert backend.spoken[-1] == "north: Exits: north and south"
    _key(app, "alt+pageup")  # past the line with no links; the caption follows the words
    assert backend.spoken[-2:] == ["fountain: A fountain gurgles: fountain", "link: Drink"]
    _key(app, "alt+pageup")
    assert backend.spoken[-1] == "no more links"


def test_the_follow_key_acts_on_the_link_jumped_to():
    app, _backend, sent = _app()
    _key(app, "alt+pageup")
    _key(app, "alt+pageup")  # north, on a line with two links
    _key(app, "alt+enter")
    assert sent == ["north"]


def test_on_from_the_first_link_skips_lines_without_any():
    app, backend, sent = _app()
    for _ in range(3):
        _key(app, "alt+pageup")  # back to the fountain
    _key(app, "alt+pagedown")
    assert backend.spoken[-1] == "north: Exits: north and south"
    _key(app, "alt+pagedown")
    _key(app, "alt+pagedown")
    assert backend.spoken[-1] == "no more links"
    _key(app, "alt+enter")  # still on south, where the last jump landed
    assert sent == ["south"]


def test_a_soundpacks_own_binding_keeps_the_key():
    # Erion's history browser uses Alt+PageUp/PageDown; in that world the key stays its.
    app, backend, _sent = _app()
    pressed: list[str] = []
    app.engine.add_key("alt+pageup", lambda _ctx: pressed.append("erion"))
    _key(app, "alt+pageup")
    assert pressed == ["erion"]
    assert not any(said.startswith("south") for said in backend.spoken)


def test_a_menu_says_so_when_jumped_to():
    app, backend, _sent = _app()
    menu = SECURE + '<send "buy sword|examine sword">a sword</send>'
    app.on_telnet_event(DataReceived(menu.encode() + b"\r\n"))
    _key(app, "alt+pageup")
    assert backend.spoken[-1] == "a sword (menu of 2)"
