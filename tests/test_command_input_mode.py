"""The command input mode is one setting for the whole client.

It used to be read when a session was created, so switching it changed nothing that was on
screen: the menu item looked on, the box in front of the player behaved exactly as before, and
only a tooltip said the change was waiting for a new tab. Now switching converts every open tab
at once, keeping whatever was half-typed, and says what the mode means.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

wx = pytest.importorskip("wx")  # main_window and the panels import wx at module level

from genericmud.config.ui_prefs import UiPrefs  # noqa: E402 - after the importorskip
from genericmud.ui.main_window import GenericMudFrame  # noqa: E402 - ditto


@pytest.fixture(scope="module")
def frame():
    application = wx.App(False)
    window = wx.Frame(None)
    yield window
    window.Destroy()
    del application


# --- the window: one switch, every tab, and something said about it -------------------------


class _Panel:
    """A tab that records what it was asked for."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def apply_command_input_mode(self, mode: str) -> bool:
        self.asked.append(mode)
        return True


class _Book:
    def __init__(self, panels: list[_Panel]) -> None:
        self.panels = panels

    def GetPageCount(self) -> int:
        return len(self.panels)

    def GetPage(self, index: int) -> _Panel:
        return self.panels[index]


def _frame(panels: list[_Panel]) -> SimpleNamespace:
    said: list[str] = []
    saved: list[tuple[str, str]] = []
    frame = SimpleNamespace(book=_Book(panels), _prefs=UiPrefs(), announce=said.append)

    def save_pref(name: str, value: str) -> None:  # the real one also updates the prefs
        saved.append((name, value))
        setattr(frame._prefs, name, value)

    frame._save_pref = save_pref
    frame.said, frame.saved = said, saved
    return frame


def test_switching_converts_every_open_tab_and_saves_the_choice():
    panels = [_Panel(), _Panel(), _Panel()]
    frame = _frame(panels)

    GenericMudFrame._set_command_input_mode(frame, "editable")

    assert [panel.asked for panel in panels] == [["editable"], ["editable"], ["editable"]]
    assert frame.saved == [("command_input_mode", "editable")]
    assert frame._prefs.command_input_mode == "editable"


def test_switching_says_what_the_mode_means():
    frame = _frame([_Panel()])

    GenericMudFrame._set_command_input_mode(frame, "editable")
    assert "for every world" in frame.said[-1]
    # The words have to be true: Shift+Enter sends the line too, it just does not move on. A
    # sentence saying it only starts a line would teach the wrong key.
    assert "Enter sends the line you are on" in frame.said[-1]
    assert "Shift+Enter sends it without moving" in frame.said[-1]
    assert "without sending" not in frame.said[-1]

    GenericMudFrame._set_command_input_mode(frame, "standard")
    assert frame.said[-1] == "The standard command box for every world."


def test_switching_with_no_tabs_open_still_saves_the_choice():
    frame = _frame([])

    GenericMudFrame._set_command_input_mode(frame, "editable")

    assert frame.saved == [("command_input_mode", "editable")]
    assert frame.said  # it was still said, so the switch is never silent


# --- a tab: what happens to the box that is already open -----------------------------------


def _panel(frame, prefs: UiPrefs):
    from genericmud.config.worlds import World
    from genericmud.ui import session_panel

    async def no_session(_panel):
        return None

    session_panel.SessionPanel._start = no_session
    loop = asyncio.new_event_loop()
    panel = session_panel.SessionPanel(
        frame, loop, {}, World("Probe", "mud.example", 4000), prefs=prefs
    )
    # A send ends up on the loop thread; here it can run at once and be watched.
    panel._loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))
    sent: list[str] = []

    def on_message(message: dict) -> None:
        if message.get("type") == "input":
            sent.append(message["text"])

    panel.app = SimpleNamespace(on_ws_message=on_message)
    return panel, loop, sent


def _enter(panel) -> None:
    event = wx.CommandEvent(wx.wxEVT_TEXT_ENTER, panel.input.GetId())
    event.SetEventObject(panel.input)
    panel.input.GetEventHandler().ProcessEvent(event)


def test_an_open_tab_switches_mode_and_keeps_what_was_typed(frame):
    from genericmud.ui.command_buffer import CommandEntryCtrl

    prefs = UiPrefs()  # standard
    panel, loop, sent = _panel(frame, prefs)
    try:
        panel.input.SetValue("tell Ariadne meet me at the gate after the raid")
        panel.input.SetInsertionPointEnd()
        assert not isinstance(panel.input, CommandEntryCtrl)

        assert panel.apply_command_input_mode("editable") is True
        assert isinstance(panel.input, CommandEntryCtrl)
        assert panel.input.GetValue() == (
            "tell Ariadne meet me at the gate after the raid"
        )
        assert panel.input.GetName() == "Probe command"  # named again for the screen reader

        # Nothing to do the second time: switching twice is not a rebuild.
        assert panel.apply_command_input_mode("editable") is False

        # And back: only the line the caret is on comes across. The buffer keeps every command
        # it has sent, so carrying the whole document out made the next Enter send the session
        # again -- see the test below for the sequence that showed it.
        panel.input.SetValue("look\nscore")
        panel.input.SetSelection(0, 0)  # caret on the first line
        assert panel.apply_command_input_mode("standard") is True
        assert not isinstance(panel.input, CommandEntryCtrl)
        assert panel.input.GetValue() == "look"
    finally:
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))  # let the panel's own work settle
        loop.close()


def test_the_box_that_arrives_still_sends(frame):
    from genericmud.ui.command_buffer import CommandEntryCtrl

    prefs = UiPrefs()
    panel, loop, sent = _panel(frame, prefs)
    try:
        panel.apply_command_input_mode("editable")
        panel.input.SetValue("say hello")
        panel.input.SetInsertionPointEnd()
        _enter(panel)
        assert sent == ["say hello"]
        assert panel.input.GetValue() == "say hello\n"  # the buffer keeps the line and adds one

        # Switching back must not bring the sent command with it, so this presses Enter before
        # touching the box: only the empty tail line the caret was on has come across.
        panel.apply_command_input_mode("standard")
        assert panel.input.GetValue() == ""
        panel.input.WriteText("look")
        _enter(panel)
        assert sent == ["say hello", "look"]
        panel.input.SetValue("north\nscore")
        _enter(panel)
        assert sent == ["say hello", "look", "north", "score"]  # a block is one per line
        assert panel.input.GetValue() == ""
        assert not isinstance(panel.input, CommandEntryCtrl)
    finally:
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))  # let the panel's own work settle
        loop.close()


def test_a_tab_showing_a_password_field_is_left_alone_until_it_goes(frame):
    from genericmud.ui.command_buffer import CommandEntryCtrl

    # The password field owns the box while the server hides typing; converting it out from
    # under that would put a secret into the document the buffer keeps.
    prefs = UiPrefs(command_input_mode="editable")
    panel, loop, _sent = _panel(frame, prefs)
    try:
        panel.input.SetValue("a draft")
        panel._set_input_masked(True)
        assert panel.apply_command_input_mode("standard") is False
        assert panel.input.HasFlag(wx.TE_PASSWORD)
        # When the field goes, the tab catches up with the setting on its own.
        prefs.command_input_mode = "standard"
        panel._set_input_masked(False)
        assert not isinstance(panel.input, CommandEntryCtrl)
        assert not isinstance(panel._entry_input, CommandEntryCtrl)
    finally:
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))  # let the panel's own work settle
        loop.close()


def test_switching_leaves_the_caret_and_the_selection_where_they_were(frame):
    prefs = UiPrefs()
    panel, loop, _sent = _panel(frame, prefs)
    try:
        # One line, so the box's own positions and the text's agree while setting this up; the
        # conversion itself is what has to cope with the difference.
        panel.input.SetValue("score the rat")
        panel.input.SetSelection(0, len("score"))

        panel.apply_command_input_mode("editable")
        # The buffer counts in its own positions, and the same words are still selected.
        assert panel.input.selected_text() == "score"

        panel.apply_command_input_mode("standard")
        assert panel.input.GetStringSelection() == "score"
    finally:
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()


def test_the_focus_stays_in_the_box_after_it_is_replaced(frame):
    # Destroying the focused control first hands focus to whatever Windows picks next, and a
    # screen reader reads that out. The new box takes the focus before the old one goes.
    prefs = UiPrefs()
    panel, loop, _sent = _panel(frame, prefs)
    frame.Show()
    try:
        panel.input.SetFocus()
        assert panel.input.HasFocus()
        panel.apply_command_input_mode("editable")
        assert panel.input.HasFocus()
        panel.apply_command_input_mode("standard")
        assert panel.input.HasFocus()
    finally:
        frame.Hide()
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()


def test_switching_back_does_not_re_send_what_the_buffer_still_holds(frame):
    # The repro from the review, in its own order: two commands sent in the buffer, back to the
    # standard box, then look. The MUD got say hello, kill rat, say hello, kill rat, look.
    prefs = UiPrefs(command_input_mode="editable")
    panel, loop, sent = _panel(frame, prefs)
    try:
        panel.input.SetValue("say hello")
        panel.input.SetInsertionPointEnd()
        _enter(panel)
        panel.input.WriteText("kill rat")
        _enter(panel)
        assert sent == ["say hello", "kill rat"]
        assert panel.input.GetValue() == "say hello\nkill rat\n"  # the buffer keeps both

        panel.apply_command_input_mode("standard")
        assert panel.input.GetValue() == ""  # the caret was on the empty tail
        panel.input.WriteText("look")
        _enter(panel)
        assert sent == ["say hello", "kill rat", "look"]
    finally:
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()


def test_switching_back_carries_the_line_the_caret_is_on(frame):
    # A command the reader has deliberately parked on is the one they meant to work on, so that
    # line comes across alone and Enter sends it once.
    prefs = UiPrefs(command_input_mode="editable")
    panel, loop, sent = _panel(frame, prefs)
    try:
        panel.input.SetValue("say hello")
        panel.input.SetInsertionPointEnd()
        _enter(panel)
        panel.input.WriteText("kill rat")
        _enter(panel)
        assert sent == ["say hello", "kill rat"]

        panel.input.SetSelection(0, 0)  # park on the first line
        panel.apply_command_input_mode("standard")
        assert panel.input.GetValue() == "say hello"
        _enter(panel)
        assert sent == ["say hello", "kill rat", "say hello"]
    finally:
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()
