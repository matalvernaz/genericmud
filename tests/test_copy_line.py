"""Ctrl+Shift+C: copy all or part of the line being reviewed, MUDBall's copy-from-line box.

The engine says which line and the window owns the box and the clipboard, so these drive
the engine the way the window does (key messages) and check what it hands the window. The
box itself is covered with the other native dialogs in test_wx_dialogs.py.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import DataReceived
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app() -> tuple[EngineApp, RecordingBackend, list[dict]]:
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    posted: list[dict] = []
    app = EngineApp(
        voice, send=lambda _text: None, post=posted.append, keymap=load_keymap("vipmud")
    )
    return app, backend, posted


def _key(app: EngineApp, combo: str) -> None:
    app.on_ws_message({"type": "key", "key": combo})


def _offered(posted: list[dict]) -> list[str]:
    return [message["text"] for message in posted if message.get("type") == protocol.COPY_LINE]


def test_the_key_is_ctrl_shift_c():
    assert load_keymap("vipmud")["ctrl+shift+c"] == "review:copy_line"


def test_before_review_starts_the_newest_line_is_offered():
    app, _backend, posted = _app()
    app.on_telnet_event(DataReceived(b"An old line\r\nThe newest line\r\n"))
    _key(app, "ctrl+shift+c")
    assert _offered(posted) == ["The newest line"]


def test_the_line_the_review_cursor_is_on_is_offered():
    app, _backend, posted = _app()
    app.on_telnet_event(DataReceived(b"Zugg tells you, the code is 4521\r\nA wolf howls\r\n"))
    _key(app, "alt+up")  # into review, one line back from the newest
    _key(app, "ctrl+shift+c")
    assert _offered(posted) == ["Zugg tells you, the code is 4521"]


def test_with_nothing_to_copy_it_says_so_and_opens_nothing():
    app, backend, posted = _app()
    _key(app, "ctrl+shift+c")
    assert _offered(posted) == []
    assert backend.spoken[-1] == "nothing to copy"
