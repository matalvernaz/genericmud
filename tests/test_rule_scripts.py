"""A rule's "send to script": a named Lua function, or Lua of its own.

This is what makes a hand-made rule able to do more than its five built-in actions -- start a
timer, switch a group of triggers off, ask the player something -- without writing a MUSHclient
pack to say it.
"""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import SEND_TO_CHOICES, UserAlias, UserRules, UserTrigger
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.rule_scripts import RuleScripts
from tests.helpers import RecordingSink

pytest.importorskip("lupa")


def _world(tmp_path, *, trigger=None, alias=None, script_source=""):
    """A world whose rules can reach Lua, with an optional script file already loaded."""
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    scripts = []
    if script_source:
        from genericmud.scripting import user_scripts

        user_scripts.save_script(tmp_path, "main.lua", script_source)
        scripts = user_scripts.load_scripts(engine, tmp_path, source_prefix="user-script:1")
    runner = RuleScripts(engine, str(tmp_path), scripts)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    api.scripts = runner
    rules = UserRules(
        triggers=[trigger] if trigger else [],
        aliases=[alias] if alias else [],
    )
    user_rules.register_rules(api, rules)
    return sink, engine


def test_script_is_offered_again_and_is_its_own_target():
    assert "script" in SEND_TO_CHOICES
    assert SEND_TO_CHOICES.index("script") != SEND_TO_CHOICES.index("execute")


def test_a_rule_can_run_lua_of_its_own(tmp_path):
    # The simplest thing it buys: a rule doing something the five built-in actions cannot.
    sink, engine = _world(
        tmp_path,
        trigger=UserTrigger(pattern="backup", send='mud.command("look")', send_to="script"),
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]


def test_a_rule_can_call_a_function_in_the_worlds_script(tmp_path):
    source = """
    function on_backup(line, wildcards)
        mud.command("say seen: " .. line)
    end
    """
    sink, engine = _world(
        tmp_path,
        trigger=UserTrigger(pattern="backup", send="on_backup", send_to="script"),
        script_source=source,
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["say seen: Starting database backup."]


def test_the_called_function_receives_the_captures(tmp_path):
    source = """
    function on_page(line, wildcards)
        mud.command("reply " .. wildcards[1])
    end
    """
    sink, engine = _world(
        tmp_path,
        trigger=UserTrigger(pattern="* pages: *", send="on_page", send_to="script"),
        script_source=source,
    )
    engine.process_line(Line("Brady pages: hello"))
    assert sink.sent == ["reply Brady"]


def test_a_rule_can_switch_a_named_trigger_off(tmp_path):
    # The case that made this worth building: manage your own automation without a pack.
    sink, engine = _world(
        tmp_path,
        trigger=UserTrigger(
            pattern="backup", send='mud.command("look")', send_to="script", label="backup-watch"
        ),
        alias=UserAlias(
            pattern="quiet *",
            send='mud.set_enabled(wildcards[1], false)',
            send_to="script",
        ),
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]
    engine.process_input("quiet backup-watch")
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]  # off now: no second send


def test_a_broken_rule_script_says_so_once(tmp_path):
    sink, engine = _world(
        tmp_path,
        trigger=UserTrigger(pattern="backup", send="this is not lua ((", send_to="script"),
    )
    engine.process_line(Line("Starting database backup."))
    engine.process_line(Line("Starting database backup."))
    failed = [
        text for text, _channel, _interrupt in sink.spoken
        if "Automation script failed" in text
    ]
    assert len(failed) == 1  # once, not on every matching line
    assert sink.sent == []


def test_lua_can_switch_rules_by_label(tmp_path):
    sink, engine = _world(
        tmp_path,
        trigger=UserTrigger(pattern="backup", send='mud.command("look")', label="watched"),
    )
    api = ScriptApi(engine, source="test")
    assert api.set_enabled("watched", False) is True
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == []


def test_captured_text_is_never_run_as_code(tmp_path):
    # A rule whose script text names a capture: what the MUD sent must arrive as a value, not
    # as text pasted into the Lua before it is parsed. Before this, a line ending in a quote
    # and a comment could add instructions of its own.
    sink, engine = _world(
        tmp_path,
        trigger=UserTrigger(
            pattern="* says *", send='mud.command("say heard %2")', send_to="script"
        ),
    )
    engine.process_line(Line('Mallory says x") mud.command("give all to mallory") --'))
    assert all("give all" not in command for command in sink.sent)
