"""The automation editors and the Automation Manager, as a keyboard and a screen reader meet them.

Shortcut letters, the label a screen reader reads for each field, the order Tab visits the fields
in, and what the manager says and saves when its buttons are pressed. The dialogs are built for
real under xvfb, as in test_wx_dialogs.py; none is shown modally.
"""

from __future__ import annotations

import re
import sys

import pytest

wx = pytest.importorskip("wx")

_MNEMONIC = re.compile(r"&([^&])")


@pytest.fixture(scope="module")
def frame():
    if sys.platform == "darwin":
        pytest.skip("wx needs a framework-build Python on macOS")
    try:
        app = wx.GetApp() or wx.App(False)
    except BaseException as error:  # noqa: BLE001 - wx raises SystemExit with no display
        pytest.skip(f"wx can't start here: {error}")
    parent = wx.Frame(None)
    yield parent
    parent.Destroy()
    del app


def _no_variables():
    return [], False


def _tab_stops(window) -> list[tuple[wx.Window, str, wx.StaticText | None]]:
    """Each control Tab reaches, in order, with the text that names it and the label before it.

    A checkbox, button or radio box names itself; any other control is named by the StaticText
    created straight before it, which is the only naming that works on every platform.
    """
    stops = []
    before = None
    for child in window.GetChildren():
        if isinstance(child, wx.StaticText):
            before = child
            continue
        if not (child.AcceptsFocusFromKeyboard() or isinstance(child, wx.RadioBox)):
            before = None
            continue
        own = (
            child.GetLabel()
            if isinstance(child, (wx.CheckBox, wx.Button, wx.RadioBox))
            else ""
        )
        stops.append((child, own or (before.GetLabel() if before else ""), before))
        before = None
    return stops


def _shortcut_clashes(window, *, enabled_only: bool = False) -> dict[str, list[str]]:
    """Letters two controls answer to. OK and Cancel are left out: on Windows they have none."""
    letters: dict[str, list[str]] = {}
    for child, label, _before in _tab_stops(window):
        if child.GetId() in (wx.ID_OK, wx.ID_CANCEL):
            continue
        if enabled_only and not child.IsEnabled():
            continue
        found = _MNEMONIC.search(label.replace("&&", ""))
        if found:
            letters.setdefault(found.group(1).lower(), []).append(label)
    return {letter: labels for letter, labels in letters.items() if len(labels) > 1}


# --- the MUD data rule editor -------------------------------------------------------------


def test_no_two_controls_in_the_mud_data_editor_share_a_shortcut(frame, tmp_path):
    # "&Enabled" and "Choose from the values the MUD has s&ent" both answered to Alt+E, so on
    # Windows Alt+E moved between the two instead of reaching either.
    from genericmud.packs.user_rules import UserWatch
    from genericmud.ui.rule_editors import DataRuleEditorDialog

    dialog = DataRuleEditorDialog(frame, tmp_path, UserWatch(), variables=_no_variables)
    try:
        assert _shortcut_clashes(dialog) == {}
        labels = [label for _child, label, _before in _tab_stops(dialog)]
        assert "Choose f&rom the values the MUD has sent..." in labels
    finally:
        dialog.Destroy()


# --- the timer editor ----------------------------------------------------------------------


def _stop(stops, control):
    """The (control, name, label before) entry for one control."""
    return next(entry for entry in stops if entry[0] is control)


def test_each_timer_field_has_its_label_straight_before_it(frame):
    # The interval had an empty label before it, so a screen reader named it only on Windows
    # (through SetName) and no letter reached it; the offset and the time of day had their
    # labels one control too early, with an empty one between label and field.
    from genericmud.packs.user_rules import UserTimer
    from genericmud.ui.rule_editors import TimerEditorDialog

    dialog = TimerEditorDialog(frame, UserTimer(send="look"))
    try:
        stops = _tab_stops(dialog)
        every, offset, at = (
            _stop(stops, dialog._every), _stop(stops, dialog._offset), _stop(stops, dialog._at)
        )
        assert every[1] == "E&very this many seconds:"
        assert offset[1] == "Offset in seconds, be&fore the interval is up:"
        assert at[1] == "At this &time each day (HH:MM:SS):"
        assert _shortcut_clashes(dialog) == {}
        # A label goes grey with its field, so it reads as unavailable along with it.
        for daily in (True, False):
            dialog._kind.SetSelection(1 if daily else 0)
            dialog._on_kind()
            assert dialog._every.IsEnabled() is (not daily)
            assert every[2].IsEnabled() is (not daily)
            assert dialog._at.IsEnabled() is daily
            assert at[2].IsEnabled() is daily
            assert dialog._offset.IsEnabled() and offset[2].IsEnabled()
    finally:
        dialog.Destroy()


def _press_ok(dialog, monkeypatch) -> tuple[list[int], list[str], list[str]]:
    """Press OK; what closed the dialog, what was said, and which fields took the focus."""
    closed: list[int] = []
    told: list[str] = []
    focused: list[str] = []
    monkeypatch.setattr(wx, "MessageBox", lambda text, *_args: told.append(text))
    for name in ("_every", "_offset", "_at", "_label"):
        widget = getattr(dialog, name)
        monkeypatch.setattr(widget, "SetFocus", lambda name=name: focused.append(name))
    dialog.EndModal = closed.append
    dialog._on_ok(None)
    return closed, told, focused


@pytest.mark.parametrize(
    ("daily", "every", "offset", "words"),
    [
        (False, "60", "abc", "'abc' is not one"),
        (False, "60", "-5", "can't be less than 0"),
        (False, "60", "90", "has to be less than the interval, which is 60 seconds"),
        (False, "60", "60", "has to be less than the interval"),
        (True, "", "10", "once a day takes no offset"),
    ],
)
def test_the_timer_editor_refuses_an_offset_it_would_not_honour(
    frame, monkeypatch, daily, every, offset, words
):
    # Words and a negative number used to save as no offset, and a daily timer kept an offset
    # it never used, all without a word; past the interval, the manager refused it later with
    # nothing to say which field was wrong.
    from genericmud.packs.user_rules import UserTimer
    from genericmud.ui.rule_editors import TimerEditorDialog

    dialog = TimerEditorDialog(frame, UserTimer(send="look"))
    try:
        dialog._kind.SetSelection(1 if daily else 0)
        dialog._on_kind()
        dialog._every.SetValue(every)
        dialog._at.SetValue("03:00")
        dialog._offset.SetValue(offset)
        closed, told, focused = _press_ok(dialog, monkeypatch)
        assert closed == []  # still open, so it can be put right
        assert len(told) == 1 and words in told[0], told
        assert focused == ["_offset"]
        assert dialog._offset.GetValue() == offset  # what was typed is still there
    finally:
        dialog.Destroy()


@pytest.mark.parametrize(
    ("daily", "offset", "saved"),
    [(False, "30", 30.0), (False, "", 0.0), (False, "0", 0.0), (True, "", 0.0), (True, "0", 0.0)],
)
def test_the_timer_editor_takes_an_offset_it_can_use(frame, monkeypatch, daily, offset, saved):
    from genericmud.packs.user_rules import UserTimer
    from genericmud.ui.rule_editors import TimerEditorDialog

    dialog = TimerEditorDialog(frame, UserTimer(send="look"))
    try:
        dialog._kind.SetSelection(1 if daily else 0)
        dialog._on_kind()
        dialog._every.SetValue("60")
        dialog._at.SetValue("03:00")
        dialog._offset.SetValue(offset)
        closed, told, _focused = _press_ok(dialog, monkeypatch)
        assert closed == [wx.ID_OK] and told == []
        assert dialog.result().offset == saved
    finally:
        dialog.Destroy()
