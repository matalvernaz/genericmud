"""Timers: doing something on a clock rather than on a line.

MUSHclient's timer has two shapes -- every so often, or once a day at a time -- and the flags
that matter for a rule: enabled, one-shot, and what it does. What it does is the same question
every other rule answers, so a timer sends to the MUD, fills your command box, writes a note or
runs Lua, using the send-to box that triggers and aliases already use.
"""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.packs import user_rules
from genericmud.packs.user_rules import (
    UserRules,
    UserTimer,
    UserTrigger,
    load_rules,
    parse_at,
    save_rules,
    seconds_until,
)
from genericmud.scripting.api import ScriptApi
from tests.helpers import RecordingSink


def _scheduled(tmp_path, *timers: UserTimer) -> tuple[RecordingSink, AutomationEngine, list]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, UserRules(timers=list(timers)))
    return sink, engine, sink.scheduled


def _fire(sink, index: int = 0) -> None:
    """Run one scheduled callback, as the clock would."""
    _delay, callback = sink.scheduled[index]
    callback()


# --- reading a time ------------------------------------------------------------------


def test_a_time_of_day_reads_in_the_shapes_people_write():
    assert parse_at("03:00") == 3 * 3600
    assert parse_at("3:00:30") == 3 * 3600 + 30
    assert parse_at("23:59:59") == 86399
    assert parse_at(" 04:15 ") == 4 * 3600 + 15 * 60


def test_a_time_that_is_not_a_time_is_not_a_timer():
    for bad in ("", "noon", "24:00", "12:60", "12:00:60", "-1:00", "12"):
        assert parse_at(bad) is None, bad


def test_a_daily_time_waits_for_the_next_one_even_having_passed_today():
    # 03:00 has gone when it is 05:00: the next 03:00 is tomorrow's, 22 hours away.
    assert seconds_until(3 * 3600, now=5 * 3600) == 22 * 3600
    assert seconds_until(5 * 3600, now=3 * 3600) == 2 * 3600
    # Exactly now is a day away, not zero: a timer that fires the instant it is made has
    # nothing to wait for, and would fire in a loop.
    assert seconds_until(3600, now=3600) == 86400


# --- interval timers -----------------------------------------------------------------


def test_an_interval_timer_is_scheduled_for_its_interval(tmp_path):
    _sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(every=30, send="look"))
    assert [delay for delay, _callback in scheduled] == [30]


def test_firing_an_interval_timer_does_it_and_schedules_the_next(tmp_path):
    sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(every=30, send="look"))
    _fire(sink, 0)
    assert sink.sent == ["look"]
    assert len(scheduled) == 2  # the next one is queued from now


def test_a_one_shot_timer_fires_once_and_stops(tmp_path):
    sink, _engine, scheduled = _scheduled(
        tmp_path, UserTimer(every=30, send="look", one_shot=True)
    )
    _fire(sink, 0)
    assert sink.sent == ["look"]
    assert len(scheduled) == 1  # nothing queued after it


def test_a_disabled_timer_is_not_scheduled_at_all(tmp_path):
    _sink, _engine, scheduled = _scheduled(
        tmp_path, UserTimer(every=30, send="look", enabled=False)
    )
    assert scheduled == []


def test_a_timer_with_nothing_to_do_is_not_scheduled(tmp_path):
    _sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(every=30, send=""))
    assert scheduled == []


def test_a_timer_with_no_shape_is_not_scheduled(tmp_path):
    _sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(send="look"))
    assert scheduled == []


# --- offsets: firing early within the interval, so two timers take turns ----------------


def test_an_offset_makes_the_first_fire_come_early(tmp_path):
    _sink, _engine, scheduled = _scheduled(
        tmp_path, UserTimer(every=120, offset=60, send="look")
    )
    assert [delay for delay, _callback in scheduled] == [60]


def test_an_offset_leaves_the_interval_between_fires_alone(tmp_path):
    # "every 120, offset 60" fires after 60, then 120 later: it is the phase that moves.
    sink, _engine, scheduled = _scheduled(
        tmp_path, UserTimer(every=120, offset=60, send="look")
    )
    _fire(sink, 0)
    assert [delay for delay, _callback in scheduled] == [60, 120]


def test_two_timers_on_one_interval_take_turns_with_offsets(tmp_path):
    # MUSHclient's own example: the same interval twice, one of them offset, so the two
    # messages are not shown in one breath.
    _sink, _engine, scheduled = _scheduled(
        tmp_path,
        UserTimer(every=120, send="pose looks around", label="a"),
        UserTimer(every=120, offset=60, send="pose scratches", label="b"),
    )
    assert sorted(delay for delay, _callback in scheduled) == [60, 120]


def test_no_offset_is_the_whole_interval_as_before(tmp_path):
    _sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(every=30, send="look"))
    assert [delay for delay, _callback in scheduled] == [30]


def test_an_offset_is_ignored_by_a_daily_timer(tmp_path):
    # A time of day has no interval to be inside: its wait comes from the clock.
    _sink, _engine, scheduled = _scheduled(
        tmp_path, UserTimer(at="03:00", offset=60, send="look")
    )
    assert len(scheduled) == 1
    delay, _callback = scheduled[0]
    assert 0 < delay <= 86400


def test_an_offset_past_the_interval_is_refused(tmp_path):
    # It would ask for a fire before the timer starts, so it is said rather than clamped.
    with pytest.raises(ValueError, match="less than the interval"):
        _scheduled(tmp_path, UserTimer(every=60, offset=60, send="look"))


def test_a_negative_offset_is_refused(tmp_path):
    with pytest.raises(ValueError, match="negative offset"):
        _scheduled(tmp_path, UserTimer(every=60, offset=-1, send="look"))


def test_a_disabled_timer_may_have_a_offset_it_will_not_use(tmp_path):
    # Same rule as a draft with no variable name: only what will be scheduled is checked.
    _sink, _engine, scheduled = _scheduled(
        tmp_path, UserTimer(every=60, offset=90, send="look", enabled=False)
    )
    assert scheduled == []


def test_the_offset_survives_a_saved_rules_file(tmp_path):
    from genericmud.packs.user_rules import rules_path

    save_rules(tmp_path, UserRules(timers=[UserTimer(every=120, offset=45, send="look")]))
    assert "offset" in rules_path(tmp_path).read_text(encoding="utf-8")
    assert load_rules(tmp_path).timers[0].offset == 45


# --- daily timers --------------------------------------------------------------------


def test_a_daily_timer_is_scheduled_for_the_next_time_it_names(tmp_path):
    sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(at="03:00", send="look"))
    assert len(scheduled) == 1
    delay, _callback = scheduled[0]
    assert 0 < delay <= 86400


def test_firing_a_daily_timer_schedules_tomorrow(tmp_path):
    sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(at="03:00", send="look"))
    _fire(sink, 0)
    assert sink.sent == ["look"]
    delay, _callback = scheduled[1]
    assert 0 < delay <= 86400


# --- what a timer does ---------------------------------------------------------------


def test_a_timer_uses_the_send_to_target_like_any_other_rule(tmp_path):
    sink, _engine, _scheduled_list = _scheduled(
        tmp_path, UserTimer(every=10, send="look", send_to="command")
    )
    _fire(sink, 0)
    assert sink.typed == ["look"]
    assert sink.sent == []


def test_a_timer_can_store_its_text_in_a_variable(tmp_path):
    sink, engine, _scheduled_list = _scheduled(
        tmp_path, UserTimer(every=10, send="tick", send_to="variable", variable="last_run")
    )
    _fire(sink, 0)
    assert engine.get_var("last_run") == "tick"
    assert sink.sent == []


def test_a_timer_can_write_a_note(tmp_path):
    sink, _engine, scheduled = _scheduled(
        tmp_path, UserTimer(every=10, send="backup soon", send_to="output")
    )
    _fire(sink, 0)
    assert sink.echoed == [("backup soon", "main")]


# --- the file ------------------------------------------------------------------------


def test_timers_survive_a_saved_rules_file(tmp_path):
    save_rules(
        tmp_path,
        UserRules(timers=[UserTimer(label="tick", every=15, send="look", send_to="command")]),
    )
    timer = load_rules(tmp_path).timers[0]
    assert (timer.label, timer.every, timer.send, timer.send_to) == ("tick", 15, "look", "command")


def test_a_rules_file_without_timers_reads_as_none(tmp_path):
    import json

    from genericmud.packs.user_rules import rules_path

    rules_path(tmp_path).write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert load_rules(tmp_path).timers == []

# --- the manager knows about them ------------------------------------------------------


def test_the_manager_offers_timers_after_the_categories_that_were_there():
    # Ctrl+1..6 keep the meanings they had; the new category does not shift them.
    pytest.importorskip("wx")
    from genericmud.ui.automation_manager import AutomationManagerDialog

    for index, name in enumerate(("Triggers", "Aliases", "Hotkeys", "Channels", "Scripts",
                                  "MUD data", "Timers")):
        assert AutomationManagerDialog._CATEGORIES[index][1].startswith(name)


def test_a_timer_reads_out_in_the_list():
    from genericmud.ui.automation_manager import _rule_summary

    every = _rule_summary("timer", UserTimer(every=30, send="look"))
    assert every == "Timer: every 30 seconds: look"
    daily = _rule_summary("timer", UserTimer(at="03:00", send="backup", one_shot=True))
    assert daily == "Timer: each day at 03:00, once only: backup"
    named = _rule_summary("timer", UserTimer(label="tick", every=5, send="look"))
    assert named.startswith("tick: Timer:")
    # The offset is said, because it is how you tell which of two timers goes first.
    offset = _rule_summary("timer", UserTimer(every=120, offset=60, send="look"))
    assert offset == "Timer: every 120 seconds, offset 60 seconds: look"
    # A daily timer keeps its offset unused, so the list must not claim it fires early.
    daily_offset = _rule_summary("timer", UserTimer(at="03:00", offset=60, send="look"))
    assert daily_offset == "Timer: each day at 03:00: look"


def test_the_timer_editor_round_trips(tmp_path):
    pytest.importorskip("wx")
    import wx

    from genericmud.ui.rule_editors import TimerEditorDialog

    # Held in a variable: wx deletes an App that nothing refers to, taking the dialogs with it.
    app = wx.GetApp() or wx.App(False)
    assert app is not None
    frame = wx.Frame(None)
    timer = UserTimer(label="tick", every=15, send="look", send_to="command")
    dialog = TimerEditorDialog(frame, timer)
    try:
        assert dialog._label.GetValue() == "tick"
        assert dialog._every.GetValue() == "15"
        assert dialog._kind.GetSelection() == 0  # every so often
        result = dialog.result()
        assert (result.label, result.every, result.send_to) == ("tick", 15, "command")
        dialog._offset.SetValue("5")
        assert dialog.result().offset == 5
        dialog._kind.SetSelection(1)
        dialog._at.SetValue("03:30")
        assert dialog.result().at == "03:30"
        assert dialog.result().every == 15  # kept, ignored while it is a daily timer
        from genericmud.packs.user_rules import SEND_TO_CHOICES

        dialog._send_to.SetSelection(SEND_TO_CHOICES.index("variable"))
        dialog._variable.SetValue("last_fired")
        assert dialog.result().send_to == "variable"
        assert dialog.result().variable == "last_fired"
    finally:
        dialog.Destroy()
        frame.Destroy()

# --- named timers: the handle a script, a label or a group uses ------------------------


def _named(tmp_path, **kwargs):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    api.add_named_timer("tick", 10, lambda: sink.sent.append("tick"), **kwargs)
    return sink, engine, api


def test_a_named_timer_can_be_switched_off_and_on(tmp_path):
    sink, engine, _api = _named(tmp_path, every=10)
    assert engine.set_timer_enabled("tick", False) is True
    _fire(sink, 0)  # the callback still runs from the queue, but does nothing now
    assert sink.sent == []
    assert engine.set_timer_enabled("tick", True) is True
    engine.named_timer("tick")._fire()
    assert sink.sent == ["tick"]


def test_a_named_timer_can_be_deleted(tmp_path):
    _sink, engine, _api = _named(tmp_path, every=10)
    assert engine.remove_named_timer("tick") is True
    assert engine.named_timer("tick") is None
    assert engine.remove_named_timer("tick") is False  # already gone
    assert engine.set_timer_enabled("tick", False) is False


def test_an_unknown_name_says_so_rather_than_raising(tmp_path):
    _sink, engine, _api = _named(tmp_path)
    assert engine.set_timer_enabled("no-such-timer", False) is False
    assert engine.named_timer("no-such-timer") is None


def test_a_label_switches_a_timer_as_well_as_a_rule(tmp_path):
    # A name is a name: the manager and a script should not have to know which kind of rule
    # they are switching.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(
        api, UserRules(timers=[UserTimer(label="tick", every=10, send="look")])
    )
    assert engine.set_enabled("tick", False) is True
    assert engine.named_timer("tick").enabled is False


def test_a_group_switches_timers_with_the_rules_in_it(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(
        api,
        UserRules(
            triggers=[UserTrigger(pattern="backup", send="look", group="combat")],
            timers=[UserTimer(label="tick", every=10, send="say tick", group="combat")],
        ),
    )
    assert engine.set_group_enabled("combat", False) == 2  # the trigger and the timer
    assert engine.named_timer("tick").enabled is False


def test_a_daily_timer_asks_the_clock_for_its_next_wait(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, UserRules(timers=[UserTimer(at="03:00", send="look")]))
    timer = engine.named_timer("timer 03:00")
    assert timer is not None
    assert timer.next_delay is not None  # not a fixed repeat: the wait moves each day
    assert 0 < timer.next_delay() <= 86400


def test_lua_names_the_three_timer_calls_mushclient_has(tmp_path):
    pytest.importorskip("lupa")
    from genericmud.scripting.lua_runtime import LuaPackRuntime

    sink = RecordingSink()
    engine = AutomationEngine(sink)
    runtime = LuaPackRuntime(ScriptApi(engine, source="probe"))
    runtime.run_source(
        'mud.add_timer("lua-tick", 5, function() mud.command("look") end, {every=5})'
    )
    assert engine.named_timer("lua-tick") is not None
    runtime.run_source('mud.enable_timer("lua-tick", false)')
    assert engine.named_timer("lua-tick").enabled is False
    runtime.run_source('mud.delete_timer("lua-tick")')
    assert engine.named_timer("lua-tick") is None


def test_lua_takes_the_offset_too(tmp_path):
    # The same head start the editor offers: the first fire comes offset seconds sooner.
    pytest.importorskip("lupa")
    from genericmud.scripting.lua_runtime import LuaPackRuntime

    sink = RecordingSink()
    engine = AutomationEngine(sink)
    runtime = LuaPackRuntime(ScriptApi(engine, source="probe"))
    runtime.run_source(
        'mud.add_timer("lua-offset", 120, function() end, {every=120, offset=60})'
    )
    timer = engine.named_timer("lua-offset")
    assert timer is not None
    assert timer.delay == 60  # the wait before the first fire
    assert timer.every == 120  # the interval itself is untouched
