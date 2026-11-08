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


# --- the trigger editor ---------------------------------------------------------------------


def _sizer_windows(sizer) -> list[wx.Window]:
    windows = []
    for item in sizer.GetChildren():
        if item.IsWindow():
            windows.append(item.GetWindow())
        elif item.IsSizer():
            windows.extend(_sizer_windows(item.GetSizer()))
    return windows


def test_the_trigger_editor_asks_the_common_questions_first(frame, tmp_path):
    # Label, Group, Order and Keep checking sat between "How to match" and the commands, so
    # every edit tabbed past four fields most triggers never use before reaching what the rule
    # does. They come after the line handling now, and before the sound fields.
    from genericmud.packs.user_rules import UserTrigger
    from genericmud.ui.rule_editors import TriggerEditorDialog

    dialog = TriggerEditorDialog(frame, tmp_path, UserTrigger(), variables=_no_variables)
    try:
        stops = _tab_stops(dialog)
        tab_order = [child for child, _label, _before in stops]
        expected = [
            dialog._enabled, dialog._pattern, dialog._match,
            dialog._send, dialog._send_to, dialog._variable, dialog._speak, dialog._interrupt,
            dialog._gag, dialog._omit_log, dialog._channel,
            dialog._label, dialog._group, dialog._order, dialog._keep_evaluating,
            dialog._stop_channel, dialog._sound, dialog._volume, dialog._pan, dialog._loop,
        ]
        places = [
            next(index for index, child in enumerate(tab_order) if child is control)
            for control in expected
        ]
        assert places == sorted(places)
        # Each field still has its own label straight before it.
        assert _stop(stops, dialog._label)[1] == (
            "&Label (a script turns it off and on by this name):"
        )
        assert _stop(stops, dialog._group)[1] == (
            "&Group (rules sharing a name switch together):"
        )
        assert _stop(stops, dialog._order)[1] == "&Order (lower is tried first):"
        assert _stop(stops, dialog._stop_channel)[1] == (
            "S&top a looping sound on this channel first (optional):"
        )
        # And the screen shows them in the order Tab visits them.
        on_screen = [window for window in _sizer_windows(dialog.GetSizer())
                     if any(window is child for child in tab_order)]
        assert [id(window) for window in on_screen] == [id(child) for child in tab_order]
    finally:
        dialog.Destroy()


# --- the manager's list --------------------------------------------------------------------


def _rule(kind: str, **fields):
    from genericmud.packs.user_rules import UserAlias, UserKey, UserTimer, UserTrigger, UserWatch

    return {
        "trigger": UserTrigger, "alias": UserAlias, "key": UserKey, "timer": UserTimer,
        "data": UserWatch,
    }[kind](**fields)


@pytest.mark.parametrize(
    ("kind", "fields", "line"),
    [
        ("trigger", dict(pattern="backup", match="contains", send="look"),
         "Trigger: backup (contains): send look"),
        ("trigger", dict(pattern="* pages you", send="page %1 = ", send_to="command"),
         "Trigger: * pages you: put page %1 = in the command box"),
        ("trigger", dict(pattern="backup", match="contains", send="Backup started",
                         send_to="output"),
         "Trigger: backup (contains): note Backup started"),
        ("trigger", dict(pattern="backup", match="contains", send="Backup started",
                         send_to="print"),
         "Trigger: backup (contains): print Backup started"),
        ("trigger", dict(pattern="backup", match="contains", send="Backup started",
                         send_to="log"),
         "Trigger: backup (contains): log Backup started"),
        ("trigger", dict(pattern="backup", match="contains", send="sh goblin",
                         send_to="execute"),
         "Trigger: backup (contains): run sh goblin as if typed"),
        ("trigger", dict(pattern="backup", match="contains", send="on_backup",
                         send_to="script"),
         "Trigger: backup (contains): run script on_backup"),
        ("trigger", dict(pattern="* pages you", send="%1", send_to="variable",
                         variable="pager"),
         "Trigger: * pages you: store %1 in variable pager"),
        # 0.25.0 read "sends", and an alias's line is a sentence: the pattern, then what it does.
        ("alias", dict(pattern="sh *", send="shout %1"), "Alias: sh * sends shout %1"),
        ("alias", dict(pattern="heal", send="do_heal", send_to="script"),
         "Alias: heal runs script do_heal"),
        ("alias", dict(pattern="pg *", send="page %1 = ", send_to="command"),
         "Alias: pg * puts page %1 = in the command box"),
        ("alias", dict(pattern="rem *", send="%1", send_to="variable", variable="kept"),
         "Alias: rem * stores %1 in variable kept"),
        ("alias", dict(pattern="hi", speak="hello"), "Alias: hi sends nothing"),
        ("key", dict(key="f5", send="kill"), "Hotkey: f5: send kill"),
        ("key", dict(key="f6", send="Ready", send_to="output"), "Hotkey: f6: note Ready"),
        ("key", dict(key="f7", send="on_f7", send_to="script"),
         "Hotkey: f7: run script on_f7"),
        ("data", dict(name="Char.Vitals.hp", send="hp now ${value}", send_to="output"),
         "MUD data: Char.Vitals.hp, when it changes: note hp now ${value}"),
        ("data", dict(name="Char.Vitals.hp", send="${value}", send_to="variable",
                      variable="hp"),
         "MUD data: Char.Vitals.hp, when it changes: store ${value} in variable hp"),
        ("timer", dict(every=30, send="look"), "Timer: every 30 seconds: look"),
        ("timer", dict(every=60, send="check_buffs", send_to="script"),
         "Timer: every 60 seconds: run script check_buffs"),
        ("timer", dict(every=600, send="Drink some water", send_to="output"),
         "Timer: every 600 seconds: note Drink some water"),
        ("timer", dict(every=600, send="Still here", send_to="log"),
         "Timer: every 600 seconds: log Still here"),
    ],
)
def test_the_list_says_what_each_destination_does_with_the_text(kind, fields, line):
    # "send on_backup" for a rule that runs a script, "send Backup started" for a note: only
    # the MUD is sent anything, and the list is where a player checks what a rule does.
    from genericmud.ui.automation_manager import _rule_summary

    assert _rule_summary(kind, _rule(kind, **fields)) == line


def test_every_destination_has_words_of_its_own():
    from genericmud.packs.user_rules import SEND_TO_CHOICES
    from genericmud.ui.automation_manager import _DESTINATION_WORDS

    assert set(_DESTINATION_WORDS) == set(SEND_TO_CHOICES)
    actions = [as_action for as_action, _as_sentence in _DESTINATION_WORDS.values()]
    assert len(set(actions)) == len(actions)  # no two destinations read the same
