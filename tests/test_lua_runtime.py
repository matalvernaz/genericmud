"""Tests for the native Lua runtime + sandbox."""

from __future__ import annotations

import pytest
from lupa import LuaError

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.lua_runtime import LuaPackRuntime
from tests.helpers import RecordingSink


def _runtime() -> tuple[RecordingSink, AutomationEngine, LuaPackRuntime]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    runtime = LuaPackRuntime(ScriptApi(engine, source="lua"))
    return sink, engine, runtime


def test_sandbox_blocks_dunder_attribute_escape(tmp_path):
    _, _, runtime = _runtime()
    sentinel = tmp_path / "pwned"
    # An exposed method's dunders must be unreachable: mud.send.__globals__ would otherwise
    # hand back the api module's os / __builtins__ / eval -- a full sandbox escape.
    reached = runtime.run_source(
        "local ok = pcall(function() return mud.send.__globals__ end); return ok"
    )
    assert reached is False
    # as_posix(): a Windows path's backslashes would be invalid Lua string escapes (\\U etc.)
    # and fail to COMPILE, masking what we're testing. The filter blocks it regardless.
    runtime.run_source(
        f'pcall(function() mud.send.__globals__["os"].system("touch {sentinel.as_posix()}") end)'
    )
    assert not sentinel.exists()


def test_lua_send():
    sink, _, runtime = _runtime()
    runtime.run_source('mud.send("hello")')
    assert sink.sent == ["hello"]


def test_lua_trigger_fires_with_wildcard():
    sink, engine, runtime = _runtime()
    runtime.run_source(
        'mud.trigger("You see *", function(line, wc) mud.speak("found " .. wc[1]) end)'
    )
    engine.process_line(Line("You see a dragon"))
    assert sink.spoken == [("found a dragon", "main", False)]


def test_lua_trigger_with_opts_priority():
    sink, engine, runtime = _runtime()
    # regex=true means a Python regex; use a Lua long-bracket string so the
    # backslash reaches the engine intact.
    runtime.run_source(
        'mud.trigger([[hp (\\d+)]], function(line, wc) mud.set_var("hp", wc[1]) end, {regex=true})'
    )
    engine.process_line(Line("hp 73 mana 10"))
    assert engine.get_var("hp") == "73"


def test_lua_command_expands_captures_script_vars_and_multiple_commands():
    sink, engine, runtime = _runtime()
    runtime.run_source(
        """
        mud.set_var("attack", "backstab")
        mud.alias("combo *", function(line, captures)
            mud.command({"stand", "${script:attack} ${1}", "consider ${1}"})
        end)
        """
    )
    assert engine.process_input("combo goblin") == []
    assert sink.sent == ["stand", "backstab goblin", "consider goblin"]


def test_lua_command_expands_named_capture_and_explicit_local_override():
    sink, engine, runtime = _runtime()
    runtime.run_source(
        """
        mud.alias([[hit (?P<target>.+)]], function(line, captures)
            local verb = "kick"
            mud.command(
                {"${verb} ${target}", "consider ${picked}"},
                {verb=verb, picked=captures.target}
            )
        end, {regex=true})
        """
    )
    engine.process_input("hit rat")
    assert sink.sent == ["kick rat", "consider rat"]


def test_lua_command_and_getter_read_nested_mud_variables():
    sink, engine, runtime = _runtime()
    engine.set_mud_var("Char.Vitals", {"hp": 73, "status": {"enemy": "orc"}})
    runtime.run_source(
        """
        mud.command("say ${mud:Char.Vitals.hp}")
        local vitals = mud.get_mud_var("Char.Vitals")
        mud.command("consider ${enemy}", {enemy=vitals.status.enemy})
        """
    )
    assert sink.sent == ["say 73", "consider orc"]


def test_lua_command_accepts_semicolon_and_multiline_stacks():
    sink, _, runtime = _runtime()
    runtime.run_source('mud.command([[stand;score\nlook]])')
    assert sink.sent == ["stand", "score", "look"]


def test_lua_command_expands_whole_stack_before_sending_any_part():
    sink, _, runtime = _runtime()
    with pytest.raises((LuaError, ValueError), match="unknown command variable"):
        runtime.run_source('mud.command({"stand", "kill ${missing}"})')
    assert sink.sent == []


def test_lua_command_rejects_line_breaks_introduced_by_mud_variables():
    sink, engine, runtime = _runtime()
    engine.set_mud_var("TARGET", "rat\nquit")
    with pytest.raises((LuaError, ValueError), match="line break"):
        runtime.run_source('mud.command({"stand", "kill ${mud:TARGET}"})')
    assert sink.sent == []


def test_lua_execute_self_referential_alias_is_depth_bounded():
    sink, engine, runtime = _runtime()
    runtime.run_source('mud.alias("loop", function() mud.execute("loop") end)')
    assert engine.process_input("loop") == []
    assert sink.sent == []


def test_lua_vars_roundtrip():
    sink, _, runtime = _runtime()
    runtime.run_source('mud.set_var("hp", "42"); mud.echo("hp is " .. mud.get_var("hp"))')
    assert ("hp is 42", "main") in sink.echoed


def test_lua_file_puts_a_line_in_a_channels_history_without_showing_or_saying_it():
    # MUSHclient packs do this with history_add. A world's own script only had mud.echo, which
    # files the line but shows and speaks it too, so chat it filed was heard twice.
    sink, _, runtime = _runtime()
    runtime.run_source('mud.file("gossip", "Bob gossips, hi")')
    assert sink.filed == [("Bob gossips, hi", "gossip")]
    assert sink.echoed == []
    assert sink.spoken == []


def test_lua_file_takes_out_colour_codes_and_line_breaks():
    # IRE games colour the text inside Comm.Channel.Text. The review keys read the history line
    # aloud, so they should hear the words, and one message is one line.
    sink, _, runtime = _runtime()
    runtime.run_source('mud.file("newbie", "\\27[32m(Newbie): Bob says, hi\\27[0m\\nand bye")')
    assert sink.filed == [("(Newbie): Bob says, hi and bye", "newbie")]


def test_lua_file_from_a_trigger_puts_the_matched_line_itself_in_the_channel():
    # As history_add does for MUSHclient packs: the line that matched moves to the channel
    # rather than gaining a copy, and its own spacing is kept, or the line isn't found.
    sink, engine, runtime = _runtime()
    runtime.run_source(
        'mud.trigger("^Bob", function(line) mud.file("gossip", line) end, {regex=true})'
    )
    line = Line("Bob  gossips, hi")
    engine.process_line(line)
    assert line.channel == "gossip"
    assert sink.filed == []


def test_lua_file_without_a_channel_or_text_says_so():
    # A missing GMCP field is nil, which would otherwise be filed as the word "None", or under
    # a channel called "none".
    sink, _, runtime = _runtime()
    with pytest.raises((LuaError, ValueError), match="mud.file"):
        runtime.run_source('mud.file(nil, "hi")')
    with pytest.raises((LuaError, ValueError), match="mud.file"):
        runtime.run_source('mud.file("gossip", nil)')
    assert sink.filed == []


def _spoken_after_a_failing_trigger(body: str) -> list[str]:
    sink, engine, runtime = _runtime()
    runtime.run_source(f'mud.trigger("^go", function() {body} end, {{regex=true}})')
    engine.process_line(Line("go"))
    return [text for text, *_rest in sink.spoken]


def test_a_script_error_says_what_went_wrong():
    # "A Lua script error occurred: LuaError" told a player nothing they could act on. The
    # reason is said, without the chunk name and line Lua puts in front of it.
    assert _spoken_after_a_failing_trigger("local t = nil; return t.y") == [
        "A Lua script error occurred: attempt to index a nil value (local 't')"
    ]


def test_a_script_error_from_a_genericmud_call_says_its_reason():
    assert _spoken_after_a_failing_trigger('mud.file(nil, "x")') == [
        "A Lua script error occurred: mud.file needs a channel name and the text to file"
    ]


def test_a_script_error_with_no_words_still_names_itself():
    assert _spoken_after_a_failing_trigger("error({code = 3})") == [
        "A Lua script error occurred: LuaError"
    ]


def test_an_error_reason_is_one_short_line():
    from genericmud.scripting.guard import error_reason

    nested = ValueError('[string "<python>"]:5: [string "<python>"]:1: the gate is shut')
    assert error_reason(nested) == "the gate is shut"
    traced = ValueError("bad thing\nstack traceback:\n\t[C]: in ?")
    assert error_reason(traced) == "bad thing"
    assert len(error_reason(ValueError("x" * 1000))) <= 200


def test_lua_key_binding():
    sink, engine, runtime = _runtime()
    runtime.run_source('mud.key("f2", function() mud.send("score") end)')
    assert engine.press_key("f2") is True
    assert sink.sent == ["score"]


def test_lua_timer_runs_a_guarded_callback():
    sink, _, runtime = _runtime()
    runtime.run_source('mud.timer(0.25, function() mud.command("look") end)')
    assert sink.scheduled[0][0] == 0.25
    sink.run_pending()
    assert sink.sent == ["look"]


def test_sandbox_removes_dangerous_globals():
    _, _, runtime = _runtime()
    assert runtime.run_source("return os == nil") is True
    assert runtime.run_source("return io == nil") is True
    assert runtime.run_source("return require == nil") is True
    with pytest.raises(LuaError):
        runtime.run_source("return os.time()")


def test_lua_trigger_routes_to_channel():
    # A nil callback registers a pure routing rule: it only tags the channel.
    _sink, engine, runtime = _runtime()
    runtime.run_source('mud.trigger("tells you", nil, {channel="tell"})')
    line = engine.process_line(Line("Bob tells you hi"))
    assert line.channel == "tell"


def test_lua_set_channel_policy():
    _sink, engine, runtime = _runtime()
    runtime.run_source('mud.set_channel("cosmetic", {speak=false, interrupt=true})')
    policy = engine.channels.policy("cosmetic")
    assert policy.speak is False
    assert policy.interrupt is True
    assert policy.display is True  # unspecified field falls back to the default


def test_lua_set_volume_and_mute():
    _sink, engine, runtime = _runtime()
    runtime.run_source('mud.set_volume("ambient", 0.5)')
    assert engine.sound.effective_gain("ambient") == 0.5
    runtime.run_source('mud.mute("ambient")')
    assert engine.sound.effective_gain("ambient") == 0.0
    runtime.run_source('mud.mute("ambient", false)')
    assert engine.sound.effective_gain("ambient") == 0.5


def test_alias_without_a_wildcard_is_the_whole_command():
    # The scripting guide's first example: mud.alias("sc", ...) makes sc send score. It
    # must not also take over "scan" because scan happens to start with sc.
    sink, engine, runtime = _runtime()
    runtime.run_source('mud.alias("sc", function() mud.send("score") end)')
    assert engine.process_input("scan") == ["scan"]
    assert engine.process_input("sc") == []
    assert sink.sent == ["score"]
