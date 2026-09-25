"""Speech that reads MUD data and script values (issue #4)."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.automation.variables import format_value
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import UserAlias, UserKey, UserRules, UserTrigger, register_rules
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import REVIEW_CHANNEL
from tests.helpers import RecordingSink


def _register(rules: UserRules, tmp_path) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    register_rules(ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path)), rules)
    return sink, engine


# --- speech that reads variables ---


def test_hotkey_reads_a_mud_value_on_the_reply_channel(tmp_path):
    rules = UserRules(keys=[UserKey(key="f2", speak="${mud:Char.Vitals.hp} health")])
    sink, engine = _register(rules, tmp_path)
    engine.set_mud_var("Char.Vitals", {"hp": 90})
    assert engine.press_key("f2")
    # Answers the keypress straight away: not queued behind MUD output, not throttled by
    # the flood governor, and still heard with self-voice off.
    assert sink.spoken == [("90 health", REVIEW_CHANNEL, True)]


def test_a_missing_value_is_said_every_time_not_skipped(tmp_path):
    rules = UserRules(keys=[UserKey(key="f2", speak="HP ${mud:Char.Vitals.hp}")])
    sink, engine = _register(rules, tmp_path)
    engine.press_key("f2")
    engine.press_key("f2")
    assert [text for text, _channel, _interrupt in sink.spoken] == [
        "HP no value for Char.Vitals.hp",
        "HP no value for Char.Vitals.hp",
    ]


def test_an_alias_can_read_a_value_without_sending_anything(tmp_path):
    rules = UserRules(aliases=[UserAlias(pattern="hp", speak="${mud:HEALTH} of ${mud:MAX}")])
    sink, engine = _register(rules, tmp_path)
    engine.set_mud_var("HEALTH", 50)
    engine.set_mud_var("MAX", 80)
    assert engine.process_input("hp") == []
    assert sink.sent == []
    assert sink.spoken == [("50 of 80", REVIEW_CHANNEL, True)]


def test_trigger_speech_fills_captures_and_script_values(tmp_path):
    rules = UserRules(triggers=[UserTrigger(
        pattern="* attacks you", speak="${1} again, target ${script:target}, %1",
    )])
    sink, engine = _register(rules, tmp_path)
    engine.set_var("target", "orc")
    engine.process_line(Line("goblin attacks you"))
    assert sink.spoken[-1] == ("goblin again, target orc, goblin", "main", False)


def test_mud_text_containing_a_template_is_read_literally(tmp_path):
    rules = UserRules(triggers=[UserTrigger(pattern="says *", speak="%1")])
    sink, engine = _register(rules, tmp_path)
    engine.set_var("secret", "should-not-expand")
    engine.process_line(Line("Bob says ${script:secret}"))
    assert sink.spoken[-1][0] == "${script:secret}"


def test_command_expansion_still_refuses_a_missing_value():
    api = ScriptApi(AutomationEngine(RecordingSink()))
    try:
        api.expand_command("kill ${script:nobody}")
    except ValueError as error:
        assert "unknown command variable: script:nobody" in str(error)
    else:
        raise AssertionError("a command with a missing value must not be sent")
    assert api.expand_speech("hi ${nobody}") == "hi no value for nobody"


def test_values_read_the_way_commands_would_put_them():
    assert format_value({"hp": 1}) == '{"hp":1}'
    assert format_value(True) == "true" and format_value(None) == ""
