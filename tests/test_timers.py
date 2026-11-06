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
    # Counted with datetimes now: 2026-06-01 is a Monday in a part of the year with no clock
    # change, so the real wait and the wall clock agree to the second.
    from datetime import datetime, time

    evening = datetime(2026, 6, 1, 5, 0)  # 05:00
    # 03:00 has gone: the next one is tomorrow's, 22 hours away.
    assert seconds_until(time(3, 0), now=evening) == 22 * 3600
    assert seconds_until(time(5, 0), now=datetime(2026, 6, 1, 3, 0)) == 2 * 3600
    # Exactly now is a day away, not zero: a timer that fires the instant it is made has
    # nothing to wait for, and would fire in a loop.
    assert seconds_until(time(1, 0), now=datetime(2026, 6, 1, 1, 0)) == 86400


def test_a_tick_that_comes_a_little_early_does_not_fire_twice():
    # asyncio can run a handle a few milliseconds before the time it asked for. Working out
    # "next 03:00 from now" then would answer "now", fire again immediately, and the player would
    # get two commands in a second.
    from datetime import datetime, time

    due = datetime(2026, 6, 1, 3, 0)
    for offset in (0.004, 0.0):
        moment = due.replace(microsecond=int(offset * 1_000_000))
        # The whole day, not the few milliseconds that are left of today.
        assert seconds_until(time(3, 0), now=moment) > 86_000, offset


def test_a_daily_timer_waits_the_real_hours_across_a_clock_change():
    # A timer set for 03:00 must be 03:00 on the day the clocks change: 23 real hours after the
    # previous one when they go forward, 25 when they go back. Skipped where the machine's zone
    # has no daylight saving, since there is nothing to count.
    from datetime import datetime, time, timedelta

    day = _a_clock_change_day()
    if day is None:
        pytest.skip("this machine's time zone has no daylight saving")
    before = datetime.combine(day - timedelta(days=1), time(3, 0))
    upcoming, wait = user_rules.next_daily_wait(time(3, 0), now=before)
    assert upcoming.date() == day
    assert upcoming.time() == time(3, 0)  # the wall clock, not 24 hours later
    assert wait in (23 * 3600, 25 * 3600)


def _a_clock_change_day():
    """Some day in the next year whose local offset differs from the day before, or None."""
    from datetime import date, timedelta

    start = date.today()
    for step in range(1, 366):
        day = start + timedelta(days=step)
        if _offset(day) != _offset(day - timedelta(days=1)):
            return day
    return None


def _offset(day):
    from datetime import datetime, time

    return datetime.combine(day, time(12)).astimezone().utcoffset()


# --- a timer that could not run says so ------------------------------------------------


def test_an_interval_timer_with_no_seconds_says_why_it_will_not_run(tmp_path):
    # A timer that saves and never fires is silent; this is the only way the player hears it.
    sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(send="drink water", every=0))
    assert scheduled == []
    assert any("Timer not started" in said for said, *_ in sink.spoken)


def test_a_daily_timer_with_a_time_nothing_can_read_says_so(tmp_path):
    sink, _engine, scheduled = _scheduled(tmp_path, UserTimer(send="backup", at="25:99"))
    assert scheduled == []
    assert any("25:99" in said for said, *_ in sink.spoken)


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
        # Cleared, not kept: "once a day" with 15 in the Every box used to fire every 15
        # seconds while listing as a daily timer.
        assert dialog.result().every == 0.0
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
    timer = engine.named_timer("timer 03:00 #1")
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


# --- the fake clock the review asked for: handles that can be cancelled -----------------


class _Handle:
    def __init__(self, clock: _Clock, delay: float, callback) -> None:
        self.clock = clock
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Clock:
    """A sink whose schedule() hands back a cancellable handle, so "armed" can be tested."""

    def __init__(self) -> None:
        self.pending: list[_Handle] = []

    def schedule(self, delay, callback, **_ignored):
        handle = _Handle(self, delay, callback)
        self.pending.append(handle)
        return handle

    def live(self) -> list[_Handle]:
        return [handle for handle in self.pending if not handle.cancelled]

    def fire(self, index: int = 0) -> None:
        live = self.live()
        handle = live[index]
        handle.cancelled = True  # it has fired
        handle.callback()


def _engine_with_clock():
    from genericmud.automation.engine import AutomationEngine

    class Sink(RecordingSink):
        def __init__(self) -> None:
            super().__init__()
            self.clock = _Clock()

        def schedule(self, delay, callback, **_ignored):
            return self.clock.schedule(delay, callback)

    sink = Sink()
    return sink, AutomationEngine(sink), sink.clock


def test_a_reload_stops_the_timers_it_replaces_instead_of_leaving_them_behind():
    # A timer switched off or deleted and saved used to be cancelled behind its own back: it
    # stayed in the list, its handle counted as armed, and after a reconnect it was back.
    sink, engine, clock = _engine_with_clock()
    api = ScriptApi(engine, source="user-rule:1")
    api.add_named_timer("drink", 10, lambda: sink.sent.append("drink water"), every=10)
    assert len(clock.live()) == 1
    engine.remove_source("user-rule:1")
    assert engine.named_timer("drink") is None
    assert clock.live() == []


def test_replacing_a_timer_by_name_stops_the_one_it_replaces():
    # mud.add_timer("tick", ...) three times is how a script restarts a timer; the first two
    # used to keep firing with nothing able to reach them.
    sink, engine, clock = _engine_with_clock()
    api = ScriptApi(engine, source="probe")
    for _ in range(3):
        api.add_named_timer("tick", 10, lambda: sink.sent.append("tick"), every=10)
    live = clock.live()
    assert len(live) == 1
    live[0].callback()
    assert sink.sent == ["tick"]


def test_switching_on_a_timer_that_is_already_on_does_not_restart_it():
    # A prompt trigger switching its group on every ten seconds would otherwise reset a
    # thirty-second timer before it ever fired.
    sink, engine, clock = _engine_with_clock()
    api = ScriptApi(engine, source="probe")
    api.add_named_timer("heal", 30, lambda: None, every=30)
    first = clock.live()[0]
    assert engine.set_timer_enabled("heal", True) is True
    assert clock.live() == [first]  # the same pending handle, not a new one


def test_a_one_shot_is_deleted_when_it_fires():
    # What MUSHclient does with a one-shot timer, and the reason there is no "finished" flag to
    # keep: a timer that has fired is not there any more, so nothing can list it as on, reach it
    # by name, or start it again from its group.
    sink, engine, clock = _engine_with_clock()
    api = ScriptApi(engine, source="probe")
    api.add_named_timer("once", 5, lambda: sink.sent.append("once"), every=None, group="combat")
    clock.fire()
    assert sink.sent == ["once"]
    assert clock.live() == []
    assert engine.named_timer("once") is None
    engine.set_group_enabled("combat", True)  # nothing left to start
    assert clock.live() == []


def test_a_daily_timer_asks_the_clock_when_it_is_armed_again():
    # Registered at 01:00 for 03:00, disconnected from 02:30 to 02:45: it should fire at 03:00,
    # not 04:45. The wait is asked for at arm time, not kept from registration.
    from datetime import datetime, time

    sink, engine, clock = _engine_with_clock()
    asked: list[datetime] = []

    def next_delay() -> float:
        asked.append(datetime.now())
        return 1234.0

    api = ScriptApi(engine, source="probe")
    api.add_named_timer("dawn", 7200, lambda: None, next_delay=next_delay, enabled=False)
    engine.set_timer_enabled("dawn", True)
    assert asked, "arming must ask the clock"
    assert clock.live()[0].delay == 1234.0
    assert time(0, 0) < datetime.now().time()


def test_a_fired_timer_does_not_leave_its_handle_being_tracked():
    # A one-second timer that ran for an hour used to leave 3,601 tracked handles behind.
    sink, engine, clock = _engine_with_clock()
    api = ScriptApi(engine, source="probe")
    api.add_named_timer("tick", 1, lambda: None, every=1)
    for _ in range(50):
        clock.fire()
    assert len(engine._timer_handles) == 1  # only the one still pending


def test_named_timers_have_the_floor_and_the_ceiling():
    from genericmud.automation.engine import MAX_NAMED_TIMERS, MIN_TIMER_DELAY

    sink, engine, clock = _engine_with_clock()
    api = ScriptApi(engine, source="probe")
    api.add_named_timer("fast", 0, lambda: None, every=0)
    assert clock.live()[0].delay >= MIN_TIMER_DELAY

    for number in range(MAX_NAMED_TIMERS + 5):
        api.add_named_timer(f"t{number}", 60, lambda: None)
    assert len(engine._named_timers) == MAX_NAMED_TIMERS


def test_a_lua_add_timer_callback_that_raises_is_reported_and_survives(tmp_path):
    pytest.importorskip("lupa")
    from genericmud.scripting.lua_runtime import LuaPackRuntime

    sink, engine, clock = _engine_with_clock()
    runtime = LuaPackRuntime(ScriptApi(engine, source="probe"))
    runtime.run_source(
        'mud.add_timer("boom", 5, function() error("boom") end, {every=5})'
    )
    clock.fire()
    # The error is the guard's business, not the crash log's: it is spoken, and the timer lives.
    assert any("Lua script error" in said for said, *_ in sink.spoken)
    assert engine.named_timer("boom") is not None
    assert clock.live(), "a repeating timer must survive a callback that raised"


def test_two_unlabelled_timers_with_one_schedule_get_their_own_names(tmp_path):
    # Two sixty-second timers taking turns are both "every 60 seconds", and sharing one name
    # left one of them unreachable and the other overwritten on every reload.
    from genericmud.packs.user_rules import timer_name

    first = UserTimer(every=60, send="look")
    second = UserTimer(every=60, send="score")
    assert timer_name(first, 0) != timer_name(second, 1)
    assert timer_name(first, 0).startswith("timer 60s")


def test_a_spent_one_shot_is_dropped_from_the_saved_rules():
    # MUSHclient's one-shot deletes itself when it fires, and its world file is written from the
    # live list, so the file loses it at the next save. This is that, for a world's rules.
    from genericmud.packs.user_rules import UserRules, drop_spent_one_shots, timer_name

    once = UserTimer(every=60, send="look", one_shot=True)
    always = UserTimer(every=60, send="score")
    rules = UserRules(timers=[once, always])
    removed = drop_spent_one_shots(rules, {timer_name(once, 0)})
    assert removed == [timer_name(once, 0)]
    assert rules.timers == [always]


def test_a_timer_that_is_not_a_one_shot_is_never_dropped():
    from genericmud.packs.user_rules import UserRules, drop_spent_one_shots, timer_name

    repeating = UserTimer(every=60, send="score")
    rules = UserRules(timers=[repeating])
    assert drop_spent_one_shots(rules, {timer_name(repeating, 0)}) == []
    assert rules.timers == [repeating]


def test_the_engine_remembers_a_spent_one_shot_and_forgets_it_on_reload(tmp_path):
    sink, engine, clock = _engine_with_clock()
    api = ScriptApi(engine, source="user", base_dir=str(tmp_path))
    api.add_named_timer("once", 5, lambda: None, every=None)
    clock.fire()
    assert engine.spent_one_shots == {"once": "user"}
    engine.remove_source("user")  # the manager has saved by now
    assert engine.spent_one_shots == {}
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
