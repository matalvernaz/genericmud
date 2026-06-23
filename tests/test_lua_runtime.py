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


def test_lua_vars_roundtrip():
    sink, _, runtime = _runtime()
    runtime.run_source('mud.set_var("hp", "42"); mud.echo("hp is " .. mud.get_var("hp"))')
    assert ("hp is 42", "main") in sink.echoed


def test_lua_key_binding():
    sink, engine, runtime = _runtime()
    runtime.run_source('mud.key("f2", function() mud.send("score") end)')
    assert engine.press_key("f2") is True
    assert sink.sent == ["score"]


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
