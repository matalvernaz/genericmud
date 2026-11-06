"""A link that types into the command box instead of sending: MXP's <SEND PROMPT>.

The spec's example is <SEND "tell Zugg " PROMPT>Zugg</SEND>: following it puts "tell Zugg "
in the player's own command line, so they finish the sentence. Sent as it stands it would
reach the MUD as a command they never gave, which is what this client used to do.
"""

from __future__ import annotations

import pytest

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Link
from genericmud.protocol.mxp import MxpParser
from genericmud.protocol.telnet import OPT_MXP, WILL, DataReceived, Negotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"
SECURE = ESC + "[1z"


def _app():
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"))
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    return app, sent, posted


def _say(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))


def _typed(posted: list[dict]) -> list[str]:
    return [str(m.get("text", "")) for m in posted if m.get("type") == protocol.INPUT_TEXT]


# --- what the parser records ------------------------------------------------


def test_a_prompt_link_is_marked_as_typing_rather_than_sending():
    outcome = MxpParser().feed(SECURE + '<send "tell Ariadne " PROMPT>Zugg</send>')
    assert outcome.text == "Zugg"
    assert outcome.links == [Link("Zugg", "tell Ariadne ", insert=True)]


def test_a_plain_link_is_still_sent():
    outcome = MxpParser().feed(SECURE + '<send "look">look</send>')
    assert outcome.links == [Link("look", "look", insert=False)]


def test_prompt_works_with_href_too():
    outcome = MxpParser().feed(SECURE + '<a href="whisper Bors " PROMPT>Bors</a>')
    assert outcome.links == [Link("Bors", "whisper Bors ", insert=True)]


def test_a_link_whose_command_is_the_word_prompt_is_not_a_flag():
    # "PROMPT" only qualifies a command that is already there; a link that sends the word
    # itself must still send it.
    outcome = MxpParser().feed(SECURE + '<send "prompt">prompt</send>')
    assert outcome.links == [Link("prompt", "prompt", insert=False)]


def test_the_prompt_flag_is_not_case_sensitive():
    outcome = MxpParser().feed(SECURE + '<send "tell Ariadne " prompt>Zugg</send>')
    assert outcome.links == [Link("Zugg", "tell Ariadne ", insert=True)]


# --- what following it does -------------------------------------------------


def test_following_a_prompt_link_sends_nothing_and_fills_the_command_box():
    app, sent, posted = _app()
    _say(app, SECURE + '<send "tell Ariadne " PROMPT>Zugg</send>')
    app._activate_link()
    assert sent == []  # nothing went to the MUD
    assert _typed(posted) == ["tell Ariadne "]


def test_following_a_plain_link_still_sends_it():
    app, sent, posted = _app()
    _say(app, SECURE + '<send "drink fountain">fountain</send>')
    app._activate_link()
    assert sent == ["drink fountain"]
    assert _typed(posted) == []


def test_the_web_renderer_can_ask_for_the_typing_too():
    app, sent, posted = _app()
    app.on_ws_message(
        {"type": protocol.LINK, "text": "Zugg", "command": "tell Ariadne ", "insert": True}
    )
    assert sent == []
    assert _typed(posted) == ["tell Ariadne "]


def test_the_renderer_cannot_make_a_send_link_type_by_omitting_the_flag():
    # The flag travels with the command; a message without it is a send, as before.
    app, sent, _posted = _app()
    app.on_ws_message({"type": protocol.LINK, "text": "fountain", "command": "drink fountain"})
    assert sent == ["drink fountain"]


def test_the_line_message_tells_the_renderer_which_links_type():
    app, _sent, posted = _app()
    _say(app, SECURE + '<send "buy sword" PROMPT>a sword</send>')
    message = next(m for m in posted if m.get("type") == protocol.LINE)
    assert message["links"] == [[0, 7, "buy sword", True, [], ""]]


def test_a_typed_link_through_the_output_box_types_rather_than_sends():
    app, sent, posted = _app()
    _say(app, SECURE + '<send "tell Ariadne " PROMPT>Zugg</send>')
    newest = app.buffer.lines()[-1]
    app.on_ws_message(
        {"type": protocol.LINK_AT, "text": newest.plain_text, "from_end": 0}
    )
    assert sent == []
    assert _typed(posted) == ["tell Ariadne "]


# --- the window ------------------------------------------------------------


def test_the_native_window_fills_the_command_box_and_focuses_it():
    pytest.importorskip("wx")
    from genericmud.ui import session_panel

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    box = _FakeBox()
    panel.input = box
    panel._alive = True  # the window ignores messages once it is closing
    panel._handle_message({"type": protocol.INPUT_TEXT, "text": "tell Ariadne "})
    assert box.value == "tell Ariadne "  # replaces whatever was there
    assert box.caret_at_end is True
    assert box.focused is True


class _FakeBox:
    """The bits of a wx.TextCtrl the command box uses, without needing a display."""

    def __init__(self) -> None:
        self.value = "left over typing"
        self.caret_at_end = False
        self.focused = False

    def SetValue(self, value: str) -> None:
        self.value = value

    def SetInsertionPointEnd(self) -> None:
        self.caret_at_end = True

    def SetFocus(self) -> None:
        self.focused = True


# --- what a typed link may put in the command box ----------------------------
# Whatever lands in the box goes out as the player's own typing when they press Enter, with
# genericMud's own commands allowed and the separator splitting it, so the MUD may start one
# plain command there and nothing more.


def _app_hearing():
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"))
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    return app, sent, posted, backend


def _follow(app: EngineApp) -> None:
    app.on_ws_message({"type": "key", "key": "alt+enter"})


def test_a_typed_link_cannot_hide_a_genericmud_command_behind_the_separator():
    # The attack: the player types a message after "tell Zugg " and presses Enter, and the
    # hidden middle defines a trigger that later sends coins from another world with /to.
    app, sent, posted, backend = _app_hearing()
    tag = (
        '<send "tell Zugg ;/trigger gurgles = /to bank give 1000 coins to Mallory;tell Zugg "'
        " PROMPT>Zugg</send>"
    )
    _say(app, SECURE + "Who is here: " + tag)
    _follow(app)
    assert _typed(posted) == []
    assert backend.spoken[-1] == "Link not followed: it would type more than one command."
    assert app._user_triggers == {}
    assert sent == []


def test_a_typed_link_cannot_start_a_genericmud_command():
    app, _sent, posted, backend = _app_hearing()
    _say(app, SECURE + '<send "/alias look = quit" PROMPT>look around</send>')
    _follow(app)
    assert _typed(posted) == []
    assert backend.spoken[-1] == "Link not followed: it would type a genericMud command."


def test_with_stacking_off_the_separator_is_just_a_character():
    app, _sent, posted, _backend = _app_hearing()
    app.command_separator = ""  # this world doesn't split commands, so ";" means nothing
    _say(app, SECURE + '<send "say hi ;) " PROMPT>wave</send>')
    _follow(app)
    assert _typed(posted) == ["say hi ;) "]


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("tell Zugg \x07", "what it would type has control characters in it"),
        ("say " + "x" * 300, "what it would type is too long for a command"),
    ],
)
def test_a_typed_link_must_be_a_plain_command(command, reason):
    app, _sent, posted, backend = _app_hearing()
    app._follow_link("Zugg", command, insert=True)
    assert _typed(posted) == []
    assert backend.spoken[-1] == f"Link not followed: {reason}."


def test_the_web_window_cannot_skip_the_check():
    # The browser says which link was clicked; what that link may do is still decided here.
    app, _sent, posted, _backend = _app_hearing()
    app.on_ws_message({"type": "link", "text": "Zugg", "command": "/quit", "insert": True})
    assert _typed(posted) == []


def test_a_caption_that_reads_prompt_is_not_the_flag():
    outcome = MxpParser().feed(SECURE + '<send href="look" hint=prompt>fountain</send>')
    assert outcome.links[0].insert is False


def test_a_bare_prompt_after_a_bare_href_still_is_the_flag():
    outcome = MxpParser().feed(SECURE + "<send href=look prompt>fountain</send>")
    assert outcome.links[0].insert is True
