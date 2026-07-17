"""Unit tests for the dialect-agnostic AutomationEngine."""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from tests.helpers import RecordingSink


def test_trigger_fires_with_wildcards():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    captured: dict[str, list[str]] = {}

    def cb(ctx):
        captured["wc"] = ctx.wildcards
        ctx.engine.sink.send("got " + ctx.wildcards[1])

    engine.add_trigger("You see *", cb)
    engine.process_line(Line("You see a dragon"))
    assert captured["wc"] == ["You see a dragon", "a dragon"]
    assert sink.sent == ["got a dragon"]


def test_redos_trigger_times_out_and_is_disabled():
    pytest.importorskip("regex")  # the per-match timeout needs the regex module
    engine = AutomationEngine(RecordingSink())
    fired: list[int] = []
    # A catastrophic pack pattern must not hang the engine on a crafted line: the per-match
    # timeout fires, the line is treated as no-match, and the offending rule is disabled.
    engine.add_trigger(r"(a|a)+$", lambda ctx: fired.append(1), regex=True, name="redos")
    engine.process_line(Line("a" * 60 + "!"))
    assert fired == []
    assert all(not rule.enabled for rule in engine._triggers if rule.name == "redos")


def test_gag_and_gag_but_display():
    engine = AutomationEngine()
    engine.add_trigger("noise", None, gag=True)
    engine.add_trigger("spammy", None, gag_but_display=True)
    assert engine.process_line(Line("noise here")).display_when_gagged is False
    spam = engine.process_line(Line("spammy tick"))
    assert spam.gagged and spam.display_when_gagged


def test_priority_then_keep_evaluating():
    engine = AutomationEngine()
    order: list[str] = []
    engine.add_trigger("x", lambda c: order.append("low"), priority=1)
    engine.add_trigger("x", lambda c: order.append("high"), priority=10)
    engine.process_line(Line("x"))
    assert order == ["high", "low"]

    stop_order: list[str] = []
    engine2 = AutomationEngine()
    engine2.add_trigger(
        "y", lambda c: stop_order.append("first"), priority=10, keep_evaluating=False
    )
    engine2.add_trigger("y", lambda c: stop_order.append("second"), priority=1)
    engine2.process_line(Line("y"))
    assert stop_order == ["first"]


def test_alias_consumes_or_passes_through():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.add_alias(
        r"^/gather (.+)$",
        lambda c: c.engine.sink.send("gather " + c.wildcards[1]),
        regex=True,
    )
    assert engine.process_input("/gather mining") == []
    assert sink.sent == ["gather mining"]
    assert engine.process_input("north") == ["north"]


def test_key_press():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.add_key("f2", lambda c: c.engine.sink.send("score"))
    assert engine.press_key("F2") is True
    assert engine.press_key("f9") is False
    assert sink.sent == ["score"]


def test_var_and_gvar_fallback():
    engine = AutomationEngine()
    engine.set_var("hp", 42)
    assert engine.get_var("hp") == "42"
    engine.set_gvar("realm", "Erion")
    assert engine.get_var("realm") == "Erion"


class _RaisingPattern:
    """Stands in for a compiled pattern that blows the per-match timeout budget."""

    pattern = "catastrophic"

    def search(self, text, **kwargs):
        raise TimeoutError("match budget exceeded")

    def match(self, text, **kwargs):
        raise TimeoutError("match budget exceeded")


class _FakeDiag:
    def __init__(self):
        self.events = []

    def event(self, stage, **fields):
        self.events.append((stage, fields))


def test_trigger_match_timeout_disables_and_traces():
    engine = AutomationEngine(RecordingSink())
    engine.diag = _FakeDiag()
    engine.add_trigger("catastrophic", lambda ctx: None, source="evilpack")
    engine._triggers[0].pattern = _RaisingPattern()
    engine.process_line(Line("a line that makes it backtrack"))
    assert engine._triggers[0].enabled is False  # disabled so it can't hang every future line
    stages = [s for s, _ in engine.diag.events]
    assert "trigger.timeout_disabled" in stages  # and the silence left a durable trace


def test_alias_match_timeout_disables_and_traces():
    engine = AutomationEngine(RecordingSink())
    engine.diag = _FakeDiag()
    engine.add_alias("catastrophic", lambda ctx: None, source="evilpack")
    engine._aliases[0].pattern = _RaisingPattern()
    engine.process_input("some input")
    assert engine._aliases[0].enabled is False
    assert "alias.timeout_disabled" in [s for s, _ in engine.diag.events]


def test_has_key_reports_bound_macros_only():
    # The UI consults this before consuming a keypress: unbound combos must fall
    # through to the platform (menu access keys), bound ones must reach the macro.
    engine = AutomationEngine()
    engine.add_key("Alt+G", lambda _ctx: None)
    assert engine.has_key("alt+g")  # case-insensitive, as press_key is
    assert not engine.has_key("alt+f")
