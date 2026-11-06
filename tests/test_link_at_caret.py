"""Enter in the output box follows the link under the caret, on a line that offers several.

The window says which line the caret is on, and it used to say nothing about which word. A line
carrying one link followed anyway; a line of exits carried by an MXP world -- or a Pueblo one --
did not, and the reader was told there was nothing to follow on a line they could hear two
commands on. The review cursor's own follow key always worked, because that cursor knows its
word: it is the caret the window had not passed on.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import OPT_MXP, WILL, DataReceived, Negotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"
SECURE = ESC + "[1z"
LINE = "Exits: north and down."
TWO_LINKS = (
    SECURE
    + 'Exits: <send href="north">north</send> and <send href="down">down</send>.'
)


def _app() -> tuple[EngineApp, list[str]]:
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=sent.append,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
    )
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))  # a server has to offer MXP first
    app.on_telnet_event(DataReceived(TWO_LINKS.encode() + b"\r\n"))
    return app, sent


def _enter(app: EngineApp, column: int, line: str = LINE, from_end: int = 0) -> None:
    """What the output box sends when Enter is pressed with the caret at ``column``."""
    app.on_ws_message(
        {"type": protocol.LINK_AT, "text": line, "from_end": from_end, "column": column}
    )


def test_enter_on_the_second_of_two_links_follows_that_one():
    app, sent = _app()
    _enter(app, LINE.index("down"))
    assert sent == ["down"]


def test_enter_on_the_first_of_two_follows_that_one():
    app, sent = _app()
    _enter(app, LINE.index("north"))
    assert sent == ["north"]


def test_enter_between_two_links_nags_rather_than_guessing():
    # "and" belongs to no link: following one of them would send a command the reader had not
    # put the caret on, so nothing is sent.
    app, sent = _app()
    _enter(app, LINE.index("and"))
    assert sent == []


def test_a_column_past_the_end_of_the_line_follows_its_last_link():
    app, sent = _app()
    _enter(app, 999)
    assert sent == ["down"]


def test_a_line_start_that_cannot_be_found_follows_nothing():
    app, sent = _app()
    _enter(app, 0, line="a line that is not in the buffer")
    assert sent == []


def test_the_column_is_optional_for_a_caller_that_does_not_know_it():
    # The review cursor's follow key never sends a column, and a line with one link still acts.
    app, sent = _app()
    app.on_telnet_event(
        DataReceived(
            (SECURE + 'You see a <send href="drink fountain">fountain</send>.').encode() + b"\r\n"
        )
    )
    app.on_ws_message(
        {"type": protocol.LINK_AT, "text": "You see a fountain.", "from_end": 0}
    )
    assert sent == ["drink fountain"]
