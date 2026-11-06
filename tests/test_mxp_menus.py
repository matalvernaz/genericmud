"""<SEND> menus: one link that offers several commands.

A menu is written as a list of commands separated by ``|``, with ``HINT=`` captioning them --
its first caption belonging to the link itself. Following one asks which command was meant,
because assuming the first is a command the player never chose and the rest were lost.
"""

from __future__ import annotations

import sys

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

MENU_TAG = (
    '<send "buy sword|examine sword|appraise sword" '
    'hint="click for options|Buy it|Look closer|What is it worth?">a sword</send>'
)


def _app():
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"))
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    return app, sent, posted


def _say(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))


def _menu_messages(posted: list[dict]) -> list[dict]:
    return [m for m in posted if m.get("type") == protocol.MENU]


# --- what the parser keeps --------------------------------------------------


def test_a_menu_keeps_every_command_with_its_caption():
    outcome = MxpParser().feed(SECURE + MENU_TAG)
    assert outcome.text == "a sword"
    assert outcome.links == [
        Link(
            "a sword",
            "buy sword",
            hint="click for options",
            choices=(
                ("buy sword", "Buy it"),
                ("examine sword", "Look closer"),
                ("appraise sword", "What is it worth?"),
            ),
        )
    ]


def test_a_menu_without_captions_still_offers_every_command():
    outcome = MxpParser().feed(SECURE + '<send "north|south|east">an exit</send>')
    assert outcome.links[0].choices == (("north", ""), ("south", ""), ("east", ""))
    assert outcome.links[0].hint == ""


def test_one_command_is_not_a_menu():
    # The single-command case is the ordinary link, and must stay exactly as it was.
    outcome = MxpParser().feed(SECURE + '<send "look" hint="Click to look">look</send>')
    assert outcome.links == [Link("look", "look", hint="Click to look")]


def test_a_menu_is_never_a_typed_command():
    # PROMPT describes one command to start; a menu is a question, so it sends nothing at all.
    outcome = MxpParser().feed(SECURE + '<send "buy|sell" PROMPT>a shop</send>')
    assert outcome.links[0].insert is False


def test_a_menu_offers_no_more_than_it_should():
    commands = "|".join(f"command {number}" for number in range(30))
    outcome = MxpParser().feed(SECURE + f'<send "{commands}">a very long menu</send>')
    assert len(outcome.links[0].choices) == 20


def test_the_spacing_round_a_menu_separator_is_not_part_of_the_command():
    outcome = MxpParser().feed(SECURE + '<send " eat | drink ">a meal</send>')
    assert [command for command, _caption in outcome.links[0].choices] == ["eat", "drink"]


# --- what following one does ------------------------------------------------


def test_following_a_menu_sends_nothing_and_asks():
    app, sent, posted = _app()
    _say(app, SECURE + MENU_TAG)
    app._activate_link()
    assert sent == []
    menus = _menu_messages(posted)
    assert len(menus) == 1
    assert menus[0]["text"] == "a sword"
    assert menus[0]["choices"] == [
        ["buy sword", "Buy it"],
        ["examine sword", "Look closer"],
        ["appraise sword", "What is it worth?"],
    ]


def test_picking_one_sends_exactly_that_command():
    app, sent, posted = _app()
    app.on_ws_message(
        {"type": protocol.MENU_CHOICE, "text": "Buy it", "command": "buy sword"}
    )
    assert sent == ["buy sword"]


def test_a_picked_command_may_not_run_a_client_command():
    # The list came from the MUD, so it goes down the link road: words for the MUD, never the
    # client's own command dispatcher.
    app, sent, _posted = _app()
    app.on_ws_message(
        {"type": protocol.MENU_CHOICE, "text": "everything is fine", "command": "/quit"}
    )
    assert sent == ["/quit"]
    assert app._closed is False  # type: ignore[attr-defined]


def test_a_menu_is_named_as_one_when_a_line_has_several_links():
    # A line carrying several links names them when the cursor is between them; the menu has to
    # say so there, or the reader only finds out by following it.
    from genericmud.app import _menu_note

    plain = Link("north", "north")
    menu = Link("a sword", "buy sword", choices=(("buy sword", ""), ("examine", "")))
    assert _menu_note(plain) == ""
    assert _menu_note(menu) == " (menu of 2)"


# --- the windows ------------------------------------------------------------


@pytest.fixture(scope="module")
def frame():
    wx = pytest.importorskip("wx")
    if sys.platform == "darwin":
        pytest.skip("wx needs a framework-build Python on macOS")
    try:
        app = wx.App(False)
    except BaseException as error:  # noqa: BLE001 - wx raises SystemExit with no display
        pytest.skip(f"wx can't start here: {error}")
    parent = wx.Frame(None)
    yield parent
    parent.Destroy()
    del app


def test_the_choose_dialog_lists_every_choice_numbered(frame):
    from genericmud.ui.output_dialogs import ChooseDialog

    dialog = ChooseDialog(
        frame, "a sword", [("buy sword", "Buy it"), ("examine sword", "")]
    )
    try:
        assert dialog._list.GetCount() == 2
        assert dialog._list.GetString(0) == "1. Buy it"
        # No caption: the command itself names the row.
        assert dialog._list.GetString(1) == "2. examine sword"
        assert dialog.chosen() == ("buy sword", "Buy it")
    finally:
        dialog.Destroy()


def test_a_number_key_picks_that_choice_and_closes(frame):
    from genericmud.ui.output_dialogs import ChooseDialog

    dialog = ChooseDialog(frame, "a sword", [("buy", "Buy"), ("sell", "Sell")])
    ended: list[int] = []
    dialog.EndModal = ended.append  # type: ignore[method-assign]
    try:
        dialog._on_char(_CharEvent(ord("2")))
        assert ended  # the dialog closed on the number
        assert dialog.chosen() == ("sell", "Sell")
    finally:
        dialog.Destroy()


def test_a_number_past_the_end_of_the_list_is_just_a_key(frame):
    from genericmud.ui.output_dialogs import ChooseDialog

    dialog = ChooseDialog(frame, "a sword", [("buy", "Buy")])
    ended: list[int] = []
    dialog.EndModal = ended.append  # type: ignore[method-assign]
    try:
        event = _CharEvent(ord("9"))
        dialog._on_char(event)
        assert ended == []
        assert event.skipped is True
    finally:
        dialog.Destroy()


class _CharEvent:
    """The two things the dialog asks of a key event, without a real keypress."""

    def __init__(self, code: int) -> None:
        self._code = code
        self.skipped = False

    def GetKeyCode(self) -> int:
        return self._code

    def Skip(self) -> None:
        self.skipped = True


# --- captions, read the way Mudlet reads them --------------------------------


def _menu(tag: str) -> Link:
    return MxpParser().feed(SECURE + tag).links[0]


def test_as_many_captions_as_commands_pair_one_to_one():
    link = _menu('<send "buy sword|examine sword" hint="Buy it|Look it over">a sword</send>')
    assert link.choices == (("buy sword", "Buy it"), ("examine sword", "Look it over"))
    assert link.hint == ""


def test_one_caption_more_than_commands_gives_the_link_its_own():
    link = _menu(
        '<send "buy sword|examine sword" hint="A shop shelf|Buy it|Look it over">a sword</send>'
    )
    assert link.choices == (("buy sword", "Buy it"), ("examine sword", "Look it over"))
    assert link.hint == "A shop shelf"


def test_a_single_caption_over_a_menu_is_the_links_own():
    link = _menu('<send "buy sword|examine sword" hint="Click for a menu">a sword</send>')
    assert link.choices == (("buy sword", ""), ("examine sword", ""))
    assert link.hint == "Click for a menu"


def test_an_empty_entry_keeps_every_later_caption_on_its_own_command():
    link = _menu('<send "buy||sell" hint="A shelf|Buy it|---|Sell it">a sword</send>')
    assert link.choices == (("buy", "Buy it"), ("sell", "Sell it"))


def test_with_ten_choices_or_more_a_number_key_is_just_a_key(frame):
    from genericmud.ui.output_dialogs import ChooseDialog

    choices = [(f"cmd {number}", "") for number in range(1, 13)]
    dialog = ChooseDialog(frame, "a big menu", choices)
    ended: list[int] = []
    dialog.EndModal = ended.append  # type: ignore[method-assign]
    try:
        event = _CharEvent(ord("1"))  # might be the start of 12
        dialog._on_char(event)
        assert ended == []
        assert event.skipped is True
    finally:
        dialog.Destroy()
