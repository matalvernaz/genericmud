"""The still-hiding hint, and hidden answers the MUD asked for.

Issue #127: a new player typing a password and its retype heard "The MUD is still hiding what
you type" between the two, although the MUD had asked for both. The hint is for a MUD that never
stops hiding, so it now counts only hidden lines nobody asked for.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.protocol import telnet as T
from genericmud.protocol.telnet import DataReceived
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

HINT = "The MUD is still hiding what you type. If it shouldn't be, press Escape."


def _hidden_app():
    backend = RecordingBackend()
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=sent.append,
        post=[].append,
        keymap={},
        schedule=lambda _delay, _callback: None,
    )
    return app, sent, backend


def _server(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode("utf-8") + b"\r\n"))


def _type(app: EngineApp, text: str) -> None:
    app.on_ws_message({"type": "input", "text": text})


def _hints(backend: RecordingBackend) -> int:
    return sum(1 for spoken in backend.spoken if spoken == HINT)


def test_a_new_characters_password_and_its_retype_get_no_hint():
    # ROM: one WILL ECHO before "Give me a password", WONT ECHO only after the retype.
    app, sent, backend = _hidden_app()
    _server(app, "Give me a password for Hero:")
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    _type(app, "s3cret")
    _server(app, "Please retype password:")
    _type(app, "s3cret")
    app.on_telnet_event(T.Negotiation(T.WONT, T.OPT_ECHO))
    assert sent == ["s3cret", "s3cret"]
    assert _hints(backend) == 0


def test_a_retype_that_did_not_match_gets_no_hint_either():
    app, _sent, backend = _hidden_app()
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    _server(app, "Give me a password for Hero:")
    _type(app, "one")
    _server(app, "Please retype password:")
    _type(app, "two")
    _server(app, "Passwords don't match.")
    _server(app, "Retype password:")
    _type(app, "one")
    _server(app, "Please retype password:")
    _type(app, "one")
    assert _hints(backend) == 0


def test_a_spanish_retype_gets_no_hint():
    app, _sent, backend = _hidden_app()
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    _server(app, "Elige una contraseña para Hero:")
    _type(app, "s3cret")
    _server(app, "Repite la contraseña:")
    _type(app, "s3cret")
    assert _hints(backend) == 0


def test_an_answer_and_then_another_line_nobody_asked_for_gets_the_hint_once():
    app, _sent, backend = _hidden_app()
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    _server(app, "Password:")
    _type(app, "s3cret")  # asked for
    _type(app, "look")  # the MUD said nothing since: it forgot to stop hiding
    _type(app, "north")
    assert _hints(backend) == 1


def test_a_mud_that_never_stops_hiding_still_gets_the_hint_after_its_prompt():
    app, _sent, backend = _hidden_app()
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    _server(app, "Password:")
    _type(app, "s3cret")
    _server(app, "Welcome back!")
    _server(app, "<100hp 50mv>")
    _type(app, "look")
    assert _hints(backend) == 1


def test_one_asking_covers_one_answer():
    # A line mentioning a password lets exactly one hidden answer through without the hint.
    app, _sent, backend = _hidden_app()
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    _server(app, "Password:")
    _type(app, "first")
    _server(app, "Password:")
    _type(app, "second")
    _type(app, "third")
    assert _hints(backend) == 1
