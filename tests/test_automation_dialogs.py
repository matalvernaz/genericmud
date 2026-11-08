"""The automation editors and the Automation Manager, as a keyboard and a screen reader meet them.

Shortcut letters, the label a screen reader reads for each field, the order Tab visits the fields
in, and what the manager says and saves when its buttons are pressed. The dialogs are built for
real under xvfb, as in test_wx_dialogs.py; none is shown modally.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import sys
import threading
import time

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


# --- the manager's switch, with a live session ----------------------------------------------


class _WaitingLoop:
    """The session's loop, standing still: what the manager hands it waits until run().

    The real loop runs a reload a moment after the save, on its own thread. Holding it still
    is how a test sees the manager in that moment, which is when the switch used to undo itself.
    """

    def __init__(self) -> None:
        self.waiting: list = []

    def call_soon_threadsafe(self, callback, *args) -> None:
        self.waiting.append((callback, args))

    def run(self) -> None:
        while self.waiting:
            callback, args = self.waiting.pop(0)
            callback(*args)


def _live_session(tmp_path, rules, *, after_load=None):
    """A panel whose session runs ``rules`` on a real engine, reloading them as the app does."""
    import types

    from genericmud.automation.engine import AutomationEngine
    from genericmud.config.keymap import load_keymap
    from genericmud.config.worlds import World
    from genericmud.packs import user_rules
    from genericmud.packs.user_rules import load_rules, save_rules
    from genericmud.scripting.api import ScriptApi

    engine = AutomationEngine()
    save_rules(tmp_path, rules)

    def reload_user_rules() -> None:
        engine.remove_source(user_rules.SOURCE)
        user_rules.register_rules(
            ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path)),
            load_rules(tmp_path),
        )

    reload_user_rules()
    if after_load is not None:
        after_load(engine)
    loop = _WaitingLoop()
    panel = types.SimpleNamespace(
        world=World("Test", "mud.example", 4000),
        app=types.SimpleNamespace(
            user_rules_dir=lambda: tmp_path, engine=engine,
            reload_user_rules=reload_user_rules, diag=None,
        ),
        variable_snapshot=None,
        _keymap=load_keymap("vipmud"),
        _prefs=types.SimpleNamespace(numpad_compass=False),
        _loop=loop,
    )
    return panel, engine, loop


def _manager(frame, panel, kind, monkeypatch, spoken):
    from genericmud.ui import automation_manager

    kinds = [entry[0] for entry in automation_manager.AutomationManagerDialog._CATEGORIES]
    # The category last shown is remembered on the class; start each test on its own.
    monkeypatch.setattr(
        automation_manager.AutomationManagerDialog, "_last_category", kinds.index(kind)
    )
    return automation_manager.AutomationManagerDialog(frame, panel, announce=spoken.append)


def _rows(manager) -> list[str]:
    return [manager._list.GetString(index) for index in range(manager._list.GetCount())]


@pytest.mark.parametrize(
    ("kind", "collection", "fields", "word"),
    [
        pytest.param("trigger", "triggers", dict(pattern="backup", send="look", label="watched"),
                     "Trigger", id="labelled trigger"),
        pytest.param("key", "keys", dict(key="ctrl+shift+f9", send="look"), "Hotkey",
                     id="hotkey"),
        pytest.param("timer", "timers", dict(label="tick", every=30, send="look"), "Timer",
                     id="timer"),
        pytest.param("data", "watches", dict(name="Char.Vitals.hp", speak="hp ${value}"),
                     "MUD data rule", id="MUD data rule"),
    ],
)
def test_disable_in_the_manager_switches_the_rule_off_and_says_so(
    frame, tmp_path, monkeypatch, kind, collection, fields, word
):
    # The button read the engine's old state back over the one it had just switched, saved
    # that, and said "Trigger enabled." for a press of Disable: nothing changed, and the words
    # said the opposite of what was asked.
    from genericmud.packs.user_rules import UserRules, engine_rule_state, load_rules

    made = _rule(kind, **fields)
    panel, engine, loop = _live_session(tmp_path, UserRules(**{collection: [made]}))
    spoken: list[str] = []
    told: list[str] = []
    monkeypatch.setattr(wx, "MessageBox", lambda text, *_args: told.append(text))
    manager = _manager(frame, panel, kind, monkeypatch, spoken)
    try:
        manager._list.SetSelection(0)
        manager._on_list_selection(None)
        assert manager._buttons["toggle"].GetLabel() == "Disa&ble"
        manager._on_toggle(None)
        assert spoken == [f"{word} disabled."]
        assert getattr(load_rules(tmp_path), collection)[0].enabled is False
        assert _rows(manager)[0].startswith("Disabled. ")
        assert manager._buttons["toggle"].GetLabel() == "E&nable"

        # The session catches up on its own loop, and the next look agrees with the file.
        loop.run()
        assert engine_rule_state(engine, kind, made) in (False, None)  # off, or not running
        manager._refresh()
        assert _rows(manager)[0].startswith("Disabled. ")

        manager._on_toggle(None)
        assert spoken[-1] == f"{word} enabled."
        assert getattr(load_rules(tmp_path), collection)[0].enabled is True
        assert not _rows(manager)[0].startswith("Disabled.")
        loop.run()
        assert engine_rule_state(engine, kind, made) is True
        assert told == []
    finally:
        manager.Destroy()


def test_a_rule_switched_back_on_in_its_editor_reads_as_on_at_once(frame, tmp_path, monkeypatch):
    # A script switched the trigger off; the player opens it, ticks Enabled and presses OK.
    # The file said on, but the list redrawn straight after asked the engine again, before its
    # reload had run, and read "Disabled." until something else redrew it.
    from genericmud.packs.user_rules import UserRules, UserTrigger, load_rules
    from genericmud.ui import rule_editors

    panel, engine, loop = _live_session(
        tmp_path,
        UserRules(triggers=[UserTrigger(pattern="backup", send="look", label="watched")]),
        after_load=lambda engine: engine.set_enabled("watched", False),
    )

    def tick_enabled_and_press_ok(editor) -> int:
        editor._enabled.SetValue(True)
        return wx.ID_OK

    monkeypatch.setattr(rule_editors.TriggerEditorDialog, "ShowModal", tick_enabled_and_press_ok)
    spoken: list[str] = []
    manager = _manager(frame, panel, "trigger", monkeypatch, spoken)
    try:
        manager._list.SetSelection(0)
        manager._on_list_selection(None)
        assert _rows(manager) == ["Disabled. watched: Trigger: backup: send look"]  # the script
        manager._on_edit(None)
        assert spoken == ["Trigger updated."]
        assert load_rules(tmp_path).triggers[0].enabled is True
        assert _rows(manager) == ["watched: Trigger: backup: send look"]
        assert manager._buttons["toggle"].GetLabel() == "Disa&ble"
        loop.run()
        manager._refresh()
        assert _rows(manager) == ["watched: Trigger: backup: send look"]
    finally:
        manager.Destroy()


# --- "From your scripts": the rules a script registered --------------------------------------


@pytest.fixture
def session_loop():
    """A session's asyncio loop on a thread of its own, the way wx_app runs it."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop, thread
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def _on_loop(loop, action):
    """Do ``action`` on the loop's thread, where a script does its work, and wait for it."""
    done: concurrent.futures.Future = concurrent.futures.Future()

    def run() -> None:
        try:
            done.set_result(action())
        except BaseException as error:  # noqa: BLE001 - raised again in the test
            done.set_exception(error)

    loop.call_soon_threadsafe(run)
    return done.result(timeout=5)


def _pump_until(condition, seconds: float = 5.0) -> bool:
    """Let the window run its wx.CallAfter work until ``condition()``, for ``seconds`` at most."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        wx.GetApp().ProcessPendingEvents()
        time.sleep(0.01)
    return True


def _scripted_session(tmp_path, loop, make):
    """A panel whose scripts made rules with ``make(api, engine)``, on the loop's thread."""
    import types

    from genericmud.automation.engine import AutomationEngine, EngineSink
    from genericmud.config.keymap import load_keymap
    from genericmud.config.worlds import World
    from genericmud.scripting import user_scripts
    from genericmud.scripting.api import ScriptApi

    class LoopSink(EngineSink):
        def schedule(self, delay, callback):
            return loop.call_later(delay, callback)  # the session's own clock, as in the app

    engine = AutomationEngine(LoopSink())
    api = ScriptApi(
        engine, source=f"{user_scripts.SOURCE_PREFIX}combat.lua", base_dir=str(tmp_path)
    )
    _on_loop(loop, lambda: make(api, engine))
    panel = types.SimpleNamespace(
        world=World("Test", "mud.example", 4000),
        app=types.SimpleNamespace(
            user_rules_dir=lambda: tmp_path, engine=engine, reload_user_rules=lambda: None,
            diag=None,
        ),
        variable_snapshot=None,
        _keymap=load_keymap("vipmud"),
        _prefs=types.SimpleNamespace(numpad_compass=False),
        _loop=loop,
    )
    return panel, engine, api


def _two_timers(api, _engine) -> None:
    api.add_trigger("You see *", lambda _ctx: None)
    api.add_key("f2", lambda _ctx: None)
    api.add_named_timer("a", 5, lambda: None, every=5)
    api.add_named_timer("b", 7, lambda: None, every=7)


def test_enabling_a_script_timer_from_the_list_starts_it_on_the_sessions_loop(
    frame, tmp_path, monkeypatch, session_loop
):
    # Enable ran on the window's thread, so the timer was armed with a loop call made from the
    # wrong thread: the loop slept on without hearing of it, and the timer did not fire until
    # something else woke the session (in asyncio's debug mode, the call raised instead).
    loop, loop_thread = session_loop
    fired: list[float] = []

    def make(api, engine) -> None:
        api.add_named_timer("ping", 0.2, lambda: fired.append(time.monotonic()), every=0.2)
        engine.set_timer_enabled("ping", False)  # the script switched it off

    panel, engine, _api = _scripted_session(tmp_path, loop, make)
    switched_on: list[int] = []
    switch = engine.set_live_enabled

    def recording_switch(handle, enabled):
        switched_on.append(threading.get_ident())
        return switch(handle, enabled)

    monkeypatch.setattr(engine, "set_live_enabled", recording_switch)
    spoken: list[str] = []
    manager = _manager(frame, panel, "live", monkeypatch, spoken)
    try:
        assert _rows(manager) == ["Disabled. ping: Timer from a script: every 0.2 seconds"]
        manager._list.SetSelection(0)
        manager._on_list_selection(None)
        assert manager._buttons["toggle"].GetLabel() == "E&nable"
        manager._on_toggle(None)
        assert _pump_until(lambda: spoken)
        assert _pump_until(lambda: fired, seconds=3.0), "the timer never fired"
        assert switched_on == [loop_thread.ident]  # on the session's thread, never the window's
        assert spoken == ["Script rule ping: every 0.2 seconds enabled."]
        assert _rows(manager) == ["ping: Timer from a script: every 0.2 seconds"]
        assert manager._buttons["toggle"].GetLabel() == "Disa&ble"
    finally:
        manager.Destroy()
        _on_loop(loop, lambda: engine.remove_named_timer("ping"))


def test_a_row_whose_rule_a_script_dropped_switches_nothing_and_says_why(
    frame, tmp_path, monkeypatch, session_loop
):
    # The switch read the engine's list afresh and used the row number from the list on screen:
    # with timer a gone, Disable on a's row switched timer b off and said "Script rule every 7
    # seconds disabled.", which was neither the rule chosen nor what happened to it.
    loop, _thread = session_loop
    panel, engine, api = _scripted_session(tmp_path, loop, _two_timers)
    spoken: list[str] = []
    manager = _manager(frame, panel, "live", monkeypatch, spoken)
    try:
        rows = _rows(manager)
        assert rows[2] == "a: Timer from a script: every 5 seconds"
        manager._list.SetSelection(2)
        manager._on_list_selection(None)
        _on_loop(loop, lambda: api.remove_named_timer("a"))  # the script deletes it meanwhile
        manager._on_toggle(None)
        assert _pump_until(lambda: spoken)
        assert spoken == [
            "Script rule a: every 5 seconds was not switched: its script has removed it since "
            "the list was read."
        ]
        assert _on_loop(loop, lambda: engine.named_timer("b").enabled) is True
        assert _rows(manager) == [rows[0], rows[1], rows[3]]  # read again, without it
    finally:
        manager.Destroy()


def test_the_last_row_still_works_after_a_script_drops_a_rule_above_it(
    frame, tmp_path, monkeypatch, session_loop
):
    # Arrowing onto the last row raised IndexError, and so did Disable: the fresh list the
    # buttons read was a row shorter than the one on screen. Nothing was said.
    loop, _thread = session_loop
    panel, engine, api = _scripted_session(tmp_path, loop, _two_timers)
    spoken: list[str] = []
    manager = _manager(frame, panel, "live", monkeypatch, spoken)
    try:
        _on_loop(loop, lambda: api.remove_named_timer("a"))
        manager._list.SetSelection(3)
        manager._on_list_selection(None)
        assert manager._buttons["toggle"].GetLabel() == "Disa&ble"
        manager._on_toggle(None)
        assert _pump_until(lambda: spoken)
        assert spoken == ["Script rule b: every 7 seconds disabled."]
        assert _on_loop(loop, lambda: engine.named_timer("b").enabled) is False
        assert _rows(manager)[-1] == "Disabled. b: Timer from a script: every 7 seconds"
        assert manager._list.GetSelection() == 2  # still on b, which moved up a row
    finally:
        manager.Destroy()


def _key(code: int, *, control: bool = False) -> wx.KeyEvent:
    event = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
    event.SetKeyCode(code)
    event.SetControlDown(control)
    return event


def test_the_keys_that_would_change_a_script_rule_say_why_they_do_not(
    frame, tmp_path, monkeypatch, session_loop
):
    # Enter, Delete, Ctrl+N and Duplicate did nothing at all on a rule a script made.
    loop, _thread = session_loop
    panel, _engine, _api = _scripted_session(
        tmp_path, loop, lambda api, _engine: api.add_trigger("You see *", lambda _ctx: None)
    )
    spoken: list[str] = []
    manager = _manager(frame, panel, "live", monkeypatch, spoken)
    try:
        manager._list.SetSelection(0)
        manager._on_list_key(_key(wx.WXK_RETURN))
        manager._on_list_key(_key(wx.WXK_DELETE))
        manager._on_manager_key(_key(ord("N"), control=True))
        manager._on_duplicate(None)
        assert spoken == [
            "Rules from scripts can't be edited here; change the script.",
            "Rules from scripts can't be deleted here; change the script, or press Disable to "
            "switch one off.",
            "Rules from scripts can't be made here; write them in a script, under Scripts.",
            "Rules from scripts can't be duplicated here; change the script.",
        ]
        assert _rows(manager) == ["Trigger from a script: You see *"]
    finally:
        manager.Destroy()


def test_the_script_rules_list_has_a_letter_of_its_own_and_lists_what_it_says(
    frame, tmp_path, monkeypatch, session_loop
):
    # "&Script rules:" shared S with "&Show:", and the category promised mud.timer, which makes
    # a timer with no name that the list never shows.
    loop, _thread = session_loop

    def make(api, _engine) -> None:
        api.add_trigger("You see *", lambda _ctx: None)
        api.add_named_timer("tick", 30, lambda: None, every=30)
        api.add_timer(30, lambda: None)  # what mud.timer(30, fn) makes

    panel, _engine, _api = _scripted_session(tmp_path, loop, make)
    manager = _manager(frame, panel, "live", monkeypatch, [])
    try:
        assert manager._items_label.GetLabel() == "&Your script rules:"
        assert manager._list.GetName().startswith("Your script rules for this world")
        assert _shortcut_clashes(manager, enabled_only=True) == {}
        assert _rows(manager) == [
            "Trigger from a script: You see *",
            "tick: Timer from a script: every 30 seconds",
        ]
        description = manager._description.GetLabel().replace("\n", " ")
        assert "mud.add_timer" in description
        assert "A mud.timer is not listed" in description
    finally:
        manager.Destroy()


@pytest.mark.parametrize("why", ["a soundpack's question", "no answer"])
def test_the_script_rules_list_says_when_the_session_cannot_answer(
    frame, tmp_path, monkeypatch, session_loop, why
):
    # The list is read on the session's loop now; a loop that can't answer must be heard about,
    # not shown as an empty list.
    from genericmud.ui import automation_manager

    loop, _thread = session_loop
    panel, _engine, _api = _scripted_session(
        tmp_path, loop, lambda api, _engine: api.add_trigger("You see *", lambda _ctx: None)
    )
    if why == "a soundpack's question":
        panel._questions = [object()]  # the loop is waiting on a pack's dialog
    else:
        panel._loop = _WaitingLoop()  # a loop that never gets round to it
        monkeypatch.setattr(automation_manager, "_SESSION_ANSWER_TIMEOUT_S", 0.05)
    spoken: list[str] = []
    manager = _manager(frame, panel, "live", monkeypatch, spoken)
    try:
        assert spoken == [
            "The session is busy, so the rules your scripts made can't be listed now. "
            "Press Ctrl+8 to try again."
        ]
        assert _rows(manager) == []
        assert manager._buttons["toggle"].IsEnabled() is False
    finally:
        manager.Destroy()


def test_a_second_press_while_a_switch_is_on_its_way_says_so(
    frame, tmp_path, monkeypatch, session_loop
):
    loop, _thread = session_loop
    panel, engine, _api = _scripted_session(tmp_path, loop, _two_timers)
    spoken: list[str] = []
    manager = _manager(frame, panel, "live", monkeypatch, spoken)
    try:
        manager._list.SetSelection(3)
        manager._on_list_selection(None)
        manager._on_toggle(None)
        manager._on_toggle(None)  # before the answer has come back
        assert _pump_until(lambda: len(spoken) == 2)
        assert spoken == [
            "Still switching the last one: the session hasn't answered yet.",
            "Script rule b: every 7 seconds disabled.",
        ]
        assert _on_loop(loop, lambda: engine.named_timer("b").enabled) is False
    finally:
        manager.Destroy()


def test_deleting_an_unlabelled_timer_leaves_the_others_their_own_state(
    frame, tmp_path, monkeypatch
):
    # Unlabelled timers are named by their place in the file. Deleting one moves the rest up,
    # so asking the engine by place after the delete, before the reload had run, gave a timer
    # the state of the one deleted above it, and the save wrote that down.
    from genericmud.packs.user_rules import UserRules, UserTimer, load_rules

    rules = UserRules(
        timers=[UserTimer(every=60, send="drink water"), UserTimer(every=60, send="eat bread")]
    )
    panel, engine, loop = _live_session(tmp_path, rules)
    assert engine.set_timer_enabled("timer 60s #2", False) is True  # a script parked it
    spoken: list[str] = []
    monkeypatch.setattr(wx, "MessageBox", lambda *_args, **_kwargs: wx.YES)
    manager = _manager(frame, panel, "timer", monkeypatch, spoken)
    try:
        assert _rows(manager)[1].startswith("Disabled. ")
        manager._list.SetSelection(0)
        manager._on_delete(None)
        assert [timer.enabled for timer in load_rules(tmp_path).timers] == [False]
        assert _rows(manager)[0].startswith("Disabled. ")
        loop.run()  # the session reloads the saved rules
        manager._refresh()
        assert _rows(manager)[0].startswith("Disabled. ")
        assert [timer.enabled for timer in load_rules(tmp_path).timers] == [False]
    finally:
        manager.Destroy()
