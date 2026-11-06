"""MUSHclient's own vocabulary is there for a world's own scripts, beside ``mud.*``.

Your world has one scripting space (``genericmud.scripting.world_runtime``), and this is what
having it means: a script can call ``Send``/``Note``/``GetVariable``/``SetVariable`` the way a
MUSHclient script does, or ``mud.*`` the way this client documents, and a rule's "send to script"
reaches both -- the same functions, the same variables, the same state the world's files loaded
into.

The dialect is MUSHclient's Lua 5.1 for the same reason: pasted MUSHclient code assumes it.
"""

from __future__ import annotations

import pytest
from lupa.lua51 import LuaError  # the dialect a world's own scripts run in: MUSHclient's 5.1

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import UserRules, UserTrigger
from genericmud.scripting import user_scripts
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.rule_scripts import RuleScripts
from genericmud.scripting.world_runtime import world_runtime
from tests.helpers import RecordingSink


def _world(tmp_path, source: str = ""):
    """A world with one loaded script file (optional) and a recording sink."""
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    loaded = []
    if source:
        user_scripts.save_script(tmp_path, "main.lua", source)
        loaded = user_scripts.load_scripts(engine, tmp_path, source_prefix="user-script:1")
    return sink, engine, loaded


def test_a_world_script_can_send_with_mushclients_send(tmp_path):
    sink, engine, _loaded = _world(
        tmp_path, 'mud.alias("hi", function() Send("say hello") end)'
    )
    engine.process_input("hi")
    assert sink.sent == ["say hello"]


def test_a_note_is_shown_and_spoken(tmp_path):
    # MushReader reads a note out as if the MUD had sent it; genericMud stands in for it.
    sink, engine, _loaded = _world(
        tmp_path, 'mud.alias("status", function() Note("all is well") end)'
    )
    engine.process_input("status")
    assert ("all is well", "main") in sink.echoed
    assert ("all is well", "main", False) in sink.spoken


def test_print_reaches_the_output_window(tmp_path):
    # Lua's own print goes to a console nobody can see; MUSHclient points it at the output.
    sink, engine, _loaded = _world(
        tmp_path, 'mud.alias("both", function() print("first", "second") end)'
    )
    engine.process_input("both")
    assert ("first second", "main") in sink.echoed


def test_mushclient_variables_and_mud_variables_are_the_same_variables(tmp_path):
    # One space, so a value set the MUSHclient way reads back the mud.* way and the reverse.
    source = """
    mud.alias("set *", function(line, captures) SetVariable("target", captures[1]) end)
    mud.alias("mudway", function() Send("say " .. mud.get_var("target")) end)
    mud.alias("mushway", function() Send("say " .. GetVariable("target")) end)
    """
    sink, engine, _loaded = _world(tmp_path, source)
    engine.process_input("set goblin")
    engine.process_input("mudway")
    engine.process_input("mushway")
    assert sink.sent == ["say goblin", "say goblin"]


def test_a_rule_can_call_a_mushclient_function(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    runner = RuleScripts(engine, str(tmp_path))
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    api.scripts = runner
    user_rules.register_rules(
        api,
        UserRules(
            triggers=[
                UserTrigger(
                    pattern="backup", send='Note("backup started")', send_to="script"
                )
            ]
        ),
    )
    engine.process_line(Line("Starting database backup."))
    assert ("backup started", "main") in sink.echoed


def test_a_rule_and_the_worlds_files_are_one_state(tmp_path):
    # The rule's code calls a function a file defined, and that function uses MUSHclient's Send.
    sink, engine, _loaded = _world(
        tmp_path, 'function shout(text) Send("say " .. text) end'
    )
    runner = RuleScripts(engine, str(tmp_path), _loaded)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    api.scripts = runner
    user_rules.register_rules(
        api,
        UserRules(
            triggers=[
                UserTrigger(pattern="backup", send='shout("backup started")', send_to="script")
            ]
        ),
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["say backup started"]


def test_the_worlds_scripts_run_in_lua_5_1(tmp_path):
    # MUSHclient embeds 5.1, so pasted MUSHclient code is written for it.
    _sink, _engine, loaded = _world(tmp_path, "started = true")
    assert loaded[0].runtime.run_source("return _VERSION") == "Lua 5.1"


def test_table_unpack_works_whichever_name_a_script_uses(tmp_path):
    # 5.1 calls it `unpack`, everything since calls it `table.unpack`; both work here.
    _sink, _engine, loaded = _world(tmp_path, "started = true")
    runtime = loaded[0].runtime
    assert runtime.run_source("return select('#', unpack({1, 2}))") == 2
    assert runtime.run_source("return select('#', table.unpack({1, 2}))") == 2


def test_a_pack_does_not_get_the_mud_table():
    # A pack was written in MUSHclient's dialect; `mud` means nothing to it, and must not appear.
    pack = MushclientPack(ScriptApi(AutomationEngine(), source="m"))
    assert pack.run_source("return mud") is None
    assert pack.run_source("return type(world)") == "table"


def test_the_worlds_own_space_leaves_the_command_box_alone(tmp_path):
    # MushReader's commands are aliases; they belong to a pack whose author assumed them.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    world_runtime(engine, tmp_path, source_prefix="user-script:1")
    assert engine.process_input("tts_interrupt hello") == ["tts_interrupt hello"]
    assert sink.spoken == []


def test_the_editor_accepts_a_script_written_for_mushclient(tmp_path):
    # Saving has to validate against the state the script will run in, not a stand-in.
    user_scripts.validate_script(tmp_path, 'Note("hello"); Send("look")')


def test_doafter_runs_later_on_the_worlds_timers(tmp_path):
    sink, engine, _loaded = _world(
        tmp_path, 'mud.alias("later", function() DoAfter(0.5, "look") end)'
    )
    engine.process_input("later")
    assert sink.sent == []  # not yet: that is the point of it
    assert [delay for delay, _callback in sink.scheduled] == [0.5]
    sink.run_pending()
    assert sink.sent == ["look"]


def test_a_misspelt_mushclient_name_says_so(tmp_path):
    # A foreign pack gets a do-nothing stand-in for APIs we lack, so that it still loads. Your
    # own script must not: a capitalised typo that quietly did nothing would never be found.
    _sink, _engine, loaded = _world(tmp_path, "started = true")
    with pytest.raises(LuaError, match="AaddTrigger"):
        loaded[0].runtime.run_source("AaddTrigger()")


def test_the_worlds_own_space_keeps_its_promise_of_no_disk_or_network(tmp_path):
    # docs/scripting.md says a world's scripts reach neither, and gaining MUSHclient's
    # vocabulary must not quietly hand them the libraries packs carry (socket, lfs, sqlite).
    _sink, _engine, loaded = _world(tmp_path, "started = true")
    runtime = loaded[0].runtime
    for name in ("io", "os", "socket", "lfs", "sqlite3", "ltn12", "json"):
        assert runtime.run_source(f"return {name}") is None, name
