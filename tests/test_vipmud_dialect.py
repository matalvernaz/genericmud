"""Golden tests for the VIPMud .set interpreter."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.vipmud_dialect import VipMudPack, tokenize_statements
from tests.helpers import RecordingSink


def _load(source: str) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vipmud")).load_source(source)
    return sink, engine


def test_key_say_with_var_substitution():
    sink, engine = _load("#VAR hp {42}\n#KEY f2 {#say {@hp hp}}")
    assert engine.get_var("hp") == "42"
    engine.press_key("f2")
    assert sink.spoken == [("42 hp", "main", False)]


def test_trigger_wildcard_and_say():
    sink, engine = _load("#TR {You see *} {#say {found %1}}")
    engine.process_line(Line("You see a dragon"))
    assert sink.spoken == [("found a dragon", "main", False)]


def test_trigger_play_sound_with_volume():
    sink, engine = _load("#TRIGGER {* hits you} {#play {hit.wav} 80}")
    engine.process_line(Line("A goblin hits you"))
    assert sink.played and sink.played[0]["file"] == "hit.wav"
    assert abs(sink.played[0]["gain"] - 0.8) < 1e-9


def test_alias_with_wildcard_sends():
    sink, engine = _load("#ALIAS {gt *} {tell group %1}")
    assert engine.process_input("gt hello team") == []
    assert sink.sent == ["tell group hello team"]


def test_bare_line_in_body_sends():
    sink, engine = _load("#KEY f3 {kill orc}")
    engine.press_key("f3")
    assert sink.sent == ["kill orc"]


def test_tokenizer_preserves_nested_braces():
    statements = tokenize_statements("#KEY f2 {#say {@hp hp, @mp mp}}")
    command, args = statements[0]
    assert command == "KEY"
    assert args[0].text == "f2"
    assert args[1].text == "#say {@hp hp, @mp mp}"
