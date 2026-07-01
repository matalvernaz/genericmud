"""A hostile pack must not DoS the client through the legitimate ScriptApi (finding G)."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine, EngineSink
from genericmud.scripting.api import (
    _MAX_ACTIVE_TIMERS,
    _MAX_VAR_VALUE_LEN,
    _MIN_TIMER_DELAY,
    ScriptApi,
)


class _RecordingSink(EngineSink):
    def __init__(self) -> None:
        self.scheduled: list[tuple[float, object]] = []

    def schedule(self, delay, callback) -> None:
        self.scheduled.append((delay, callback))


def test_add_timer_is_capped():
    api = ScriptApi(AutomationEngine())  # base sink.schedule is a no-op, so timers never "fire"
    for _ in range(_MAX_ACTIVE_TIMERS + 50):
        api.add_timer(0.0, lambda: None)
    assert api._active_timers == _MAX_ACTIVE_TIMERS  # excess refused


def test_add_timer_clamps_near_zero_delay():
    sink = _RecordingSink()
    api = ScriptApi(AutomationEngine(sink))
    api.add_timer(0.0, lambda: None)
    assert sink.scheduled[0][0] >= _MIN_TIMER_DELAY


def test_add_timer_frees_its_slot_when_it_fires():
    sink = _RecordingSink()
    api = ScriptApi(AutomationEngine(sink))
    api.add_timer(0.0, lambda: None)
    assert api._active_timers == 1
    _delay, wrapped = sink.scheduled[0]
    wrapped()  # the event loop fires the timer
    assert api._active_timers == 0  # a self-rearming heartbeat stays at steady state


def test_set_var_rejects_oversized_value():
    engine = AutomationEngine()
    api = ScriptApi(engine)
    api.set_var("x", "a" * (_MAX_VAR_VALUE_LEN + 1))
    assert engine.get_var("x") == ""  # refused
    api.set_var("y", "fine")
    assert engine.get_var("y") == "fine"
